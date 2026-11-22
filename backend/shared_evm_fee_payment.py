"""One durable owner-signed MiniNex fee transfer per verified HWM obligation.

This connects the immutable HWM fee ledger to a *real* ERC-20 wallet
transaction without expanding the Privy automated trader's grant. A wallet
holder must explicitly sign the supplied transaction with their own Privy
wallet. The server only prepares and verifies; it NEVER broadcasts fees.
"""
from __future__ import annotations

from decimal import Decimal
import logging
import re
import time

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .live_orders import valid_order_key
from .shared_evm_swap_receipts import _pair
from .shared_evm_fee_transfer_proof import (
    owner_fee_treasury,owner_fee_transfer_draft,
    fetch_owner_fee_transfer_proof,
)
from .shared_evm_policy_reference_rates import (
    fetch_evm_policy_reference_rates,_price,
)
from .privy_wallet_verification import fetch_privy_user
from .wallet_balances import verified_addresses
from .storage import _db_url,_ensure_schema,_safe_did

LOG=logging.getLogger("mininex.evm_verified_fee_payment")
_ASSETS=frozenset(("BNB","POL","AVAX","LINK","QNT","UNI","ARB"))
_HASH=re.compile(r"^0x[0-9a-f]{64}$")


def _valid_fee_source_locked(cur,*,did:str,asset:str,key:str,wallet:str)->dict:
    """Source is an actual assessed SELL, not a client price or PAPER fill."""
    cur.execute(
        """SELECT o.fee_due_usd,o.settlement_status,o.settlement_tx_hash,
                  e.fee_assessed_usd,e.source_tx_hash,
                  b.status,b.side,b.stable,b.chain_id,b.wallet_address,
                  b.tx_hash
           FROM mininex_evm_fee_obligations o
           JOIN mininex_evm_fee_events e
             ON e.privy_did=o.privy_did AND e.asset=o.asset
            AND e.execution_id=o.execution_id
           JOIN mininex_live_evm_auto_broadcasts b
             ON b.privy_did=o.privy_did AND b.asset=o.asset
            AND b.intent_key=o.execution_id
           WHERE o.privy_did=%s AND o.asset=%s AND o.execution_id=%s
           FOR UPDATE OF o,e,b""",(did,asset,key))
    row=cur.fetchone()
    if (type(row) is not dict or row.get("status")!="FINALIZED_SUCCESS"
            or row.get("side")!="SELL"
            or row.get("wallet_address")!=wallet
            or row.get("tx_hash")!=row.get("source_tx_hash")
            or row.get("chain_id")!=_pair(asset,row.get("stable"),"SELL")["chain_id"]):
        raise HTTPException(409,"No owner-verified realized 5 percent fee")
    due=Decimal(str(row.get("fee_due_usd")))
    if (not due.is_finite() or due<=0
            or due!=Decimal(str(row.get("fee_assessed_usd")))):
        raise HTTPException(409,"HWM fee and original obligation disagree")
    return row


def _saved_to_fee_draft(row:dict)->dict:
    """Reconstruct an immutable original transfer from stored chain facts."""
    if type(row) is not dict:
        raise ValueError("Original fee record missing")
    draft=owner_fee_transfer_draft(
        asset=row["asset"],stable=row["stable"],
        wallet=row["owner_wallet"],treasury=row["treasury_wallet"],
        fee_due_usd=row["fee_due_usd"],
        stable_usd=row["stable_usd"])
    if (row["chain_id"]!=draft["chain_id"]
            or row["stable_token_address"]!=draft["token_address"]
            or Decimal(str(row["token_amount_atomic"]))!=Decimal(draft["amount_atomic"])):
        raise HTTPException(409,"Immutable fee quote changed")
    return draft


def _prepare_locked(
    cur,*,did:str,asset:str,key:str,wallet:str,treasury:str,
    rate:Decimal,
)->dict:
    """One immutable transfer offer; racing workers never create two quotes."""
    source=_valid_fee_source_locked(cur,did=did,asset=asset,
                                    key=key,wallet=wallet)
    cur.execute(
        """SELECT asset,stable,chain_id,stable_token_address,
                  owner_wallet,treasury_wallet,fee_due_usd,stable_usd,
                  token_amount_atomic,state,tx_hash
           FROM mininex_evm_verified_fee_payments
           WHERE privy_did=%s AND asset=%s AND execution_id=%s FOR UPDATE""",
        (did,asset,key))
    old=cur.fetchone()
    if old is not None:
        if (old["stable"]!=source["stable"]
                or old["owner_wallet"]!=wallet
                or old["treasury_wallet"]!=treasury
                or Decimal(str(old["fee_due_usd"]))!=Decimal(str(source["fee_due_usd"]))):
            raise HTTPException(409,"Original signed fee instruction differs")
        result=_saved_to_fee_draft(old)
        result["execution_id"]=key
        result["state"]=old["state"]
        result["tx_hash"]=old["tx_hash"]
        result["paid_onchain"]=old["state"]=="PAID_VERIFIED"
        return result
    if source.get("settlement_status")!="AWAITING_VERIFIED_TRANSFER":
        raise HTTPException(409,"This performance fee is already settled")
    draft=owner_fee_transfer_draft(
        asset=asset,stable=source["stable"],wallet=wallet,
        treasury=treasury,fee_due_usd=source["fee_due_usd"],stable_usd=rate)
    cur.execute(
        """INSERT INTO mininex_evm_verified_fee_payments
           (privy_did,asset,execution_id,chain_id,stable,
            stable_token_address,owner_wallet,treasury_wallet,
            fee_due_usd,stable_usd,token_amount_atomic)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (did,asset,key,draft["chain_id"],source["stable"],
         draft["token_address"],wallet,treasury,
         source["fee_due_usd"],rate,int(draft["amount_atomic"])))
    draft["execution_id"]=key
    draft["state"]="PREPARED"
    draft["tx_hash"]=None
    return draft


async def _owner_wallet(did:str)->str:
    user=await fetch_privy_user(did)
    try:
        wallet=verified_addresses(user,did)["ETH"]
    except (ValueError,TypeError,KeyError):
        raise HTTPException(409,"Owner Privy EVM wallet unavailable") from None
    if type(wallet) is not str or not re.fullmatch(r"0x[0-9a-fA-F]{40}",wallet):
        raise HTTPException(409,"Owner Privy EVM wallet invalid")
    return wallet.lower()


def _reject_manual_payment_during_auto_send_locked(
    cur,*,did:str,asset:str,key:str,
)->None:
    """Serialize with privileged auto SEND and prevent double settlement.

    Both manual and automatic fee flows lock the SAME owner-account row.
    A committed auto fee reservation must never be shown as a new manually
    signable token transfer, even while its network hash is uncertain.
    """
    cur.execute(
        """SELECT privy_did FROM mininex_accounts
           WHERE privy_did=%s FOR UPDATE""",(did,))
    owner=cur.fetchone()
    if type(owner) is not dict or owner.get("privy_did")!=did:
        raise HTTPException(409,"Original fee owner wallet unavailable")
    cur.execute(
        """SELECT status FROM mininex_evm_auto_fee_sends
           WHERE privy_did=%s AND asset=%s AND execution_id=%s
           FOR UPDATE""",(did,asset,key))
    if cur.fetchone() is not None:
        raise HTTPException(
            409,"This fee is already assigned to an automatic wallet send")


async def prepare_owner_verified_evm_fee(
    *,did:str,asset:str,execution_id:str,
)->dict:
    """No backend send: signed transaction prepared for the owner only."""
    did=_safe_did(did)
    key=valid_order_key(execution_id)
    if asset not in _ASSETS:
        raise HTTPException(404,"No owner EVM fee for this asset")
    wallet=await _owner_wallet(did)
    # First read the immutable assessed obligation from this user's DB,
    # never trust a claimed stablecoin or fee amount from the client.
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            source=_valid_fee_source_locked(cur,did=did,asset=asset,
                                            key=key,wallet=wallet)
            stable=source["stable"]
    treasury=owner_fee_treasury(asset=asset,stable=stable)
    prices=await fetch_evm_policy_reference_rates(asset,stable)
    rate=_price(prices,stable,int(time.time()))
    try:
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _reject_manual_payment_during_auto_send_locked(
                    cur,did=did,asset=asset,key=key)
                return _prepare_locked(
                    cur,did=did,asset=asset,key=key,wallet=wallet,
                    treasury=treasury,rate=rate)
    except psycopg.Error:
        LOG.warning("Original MiniNex fee payment claim unavailable")
        raise HTTPException(503,"Cannot persist user-approved EVM fee") from None


def _paid_locked(cur,*,did:str,asset:str,key:str,
                 wallet:str,tx_hash:str,proof:dict)->dict:
    """No credit until the immutable onchain receipt and original debt match."""
    source=_valid_fee_source_locked(
        cur,did=did,asset=asset,key=key,wallet=wallet)
    cur.execute(
        """SELECT asset,stable,chain_id,stable_token_address,
                  owner_wallet,treasury_wallet,fee_due_usd,stable_usd,
                  token_amount_atomic,state,tx_hash
           FROM mininex_evm_verified_fee_payments
           WHERE privy_did=%s AND asset=%s AND execution_id=%s FOR UPDATE""",
        (did,asset,key))
    row=cur.fetchone()
    if row is None:
        raise HTTPException(409,"Original owner fee quote not prepared")
    draft=_saved_to_fee_draft(row)
    if (row["stable"]!=source["stable"]
            or Decimal(str(row["fee_due_usd"]))!=Decimal(str(source["fee_due_usd"]))
            or proof.get("mode")!="FINALIZED_OWNER_SIGNED_EVM_FEE_PAYMENT"
            or proof.get("onchain_finalized") is not True
            or proof.get("asset")!=asset
            or proof.get("chain_id")!=draft["chain_id"]
            or proof.get("tx_hash")!=tx_hash
            or proof.get("wallet_address")!=wallet
            or proof.get("treasury_wallet")!=draft["treasury_wallet"]
            or proof.get("token_address")!=draft["token_address"]
            or proof.get("amount_atomic")!=draft["amount_atomic"]):
        raise HTTPException(409,"Fee payment must match original owner transaction")
    if row["state"]=="PAID_VERIFIED":
        if (row["tx_hash"]!=tx_hash
                or source.get("settlement_status")!="PAID_VERIFIED"
                or source.get("settlement_tx_hash")!=tx_hash):
            raise HTTPException(409,"Fee has already been paid by another transaction")
        return {"state":"PAID_VERIFIED","duplicate":True,
                "asset":asset,"execution_id":key,"tx_hash":tx_hash}
    if (row["state"]!="PREPARED"
            or source.get("settlement_status")!="AWAITING_VERIFIED_TRANSFER"
            or source.get("settlement_tx_hash") is not None):
        raise HTTPException(409,"Fee payment is not awaiting proof")
    cur.execute(
        """UPDATE mininex_evm_verified_fee_payments
           SET state='PAID_VERIFIED',tx_hash=%s,paid_at=now()
           WHERE privy_did=%s AND asset=%s AND execution_id=%s
             AND state='PREPARED' AND tx_hash IS NULL
           RETURNING state""",(tx_hash,did,asset,key))
    changed=cur.fetchone()
    if type(changed) is not dict or changed.get("state")!="PAID_VERIFIED":
        raise HTTPException(409,"Concurrent fee payment state conflict")
    # Same SQL transaction as the immutable receipt insert: an obligation
    # cannot remain 'unpaid' after verified payment or vice versa.
    cur.execute(
        """UPDATE mininex_evm_fee_obligations
           SET settlement_status='PAID_VERIFIED',settlement_tx_hash=%s
           WHERE privy_did=%s AND asset=%s AND execution_id=%s
             AND settlement_status='AWAITING_VERIFIED_TRANSFER'
             AND settlement_tx_hash IS NULL RETURNING settlement_status""",
        (tx_hash,did,asset,key))
    obligation=cur.fetchone()
    if (type(obligation) is not dict
            or obligation.get("settlement_status")!="PAID_VERIFIED"):
        raise HTTPException(409,"Real payment and HWM debt not atomic")
    return {"state":"PAID_VERIFIED","duplicate":False,
            "asset":asset,"execution_id":key,"tx_hash":tx_hash,
            "fee_paid_onchain":True}


async def confirm_owner_verified_evm_fee(
    *,did:str,asset:str,execution_id:str,tx_hash:str,
)->dict:
    """Owner submits only a hash; real chain logs determine payment credit."""
    did=_safe_did(did)
    key=valid_order_key(execution_id)
    if asset not in _ASSETS or type(tx_hash) is not str or not _HASH.fullmatch(tx_hash):
        raise HTTPException(422,"Invalid original EVM fee payment identity")
    wallet=await _owner_wallet(did)
    db=_db_url();_ensure_schema(db)
    try:
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _reject_manual_payment_during_auto_send_locked(
                    cur,did=did,asset=asset,key=key)
                source=_valid_fee_source_locked(
                    cur,did=did,asset=asset,key=key,wallet=wallet)
                cur.execute(
                    """SELECT asset,stable,chain_id,stable_token_address,
                              owner_wallet,treasury_wallet,fee_due_usd,stable_usd,
                              token_amount_atomic,state,tx_hash
                       FROM mininex_evm_verified_fee_payments
                       WHERE privy_did=%s AND asset=%s AND execution_id=%s""",
                    (did,asset,key))
                row=cur.fetchone()
    except psycopg.Error:
        raise HTTPException(503,"Verified owner fee record unavailable") from None
    if row is None or row.get("owner_wallet")!=wallet or row["stable"]!=source["stable"]:
        raise HTTPException(409,"Original authorized fee quote missing")
    if row["state"]=="PAID_VERIFIED":
        if row["tx_hash"]!=tx_hash:
            raise HTTPException(409,"Fee already paid with another transaction")
        return {"asset":asset,"execution_id":key,"state":"PAID_VERIFIED",
                "tx_hash":tx_hash,"duplicate":True}
    draft=_saved_to_fee_draft(row)
    proof=await fetch_owner_fee_transfer_proof(
        asset=asset,stable=row["stable"],draft=draft,tx_hash=tx_hash)
    if proof.get("onchain_finalized") is not True:
        return {"asset":asset,"execution_id":key,"state":"AWAITING_FINALITY",
                "tx_hash":tx_hash,"fee_paid_onchain":False}
    try:
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                return _paid_locked(
                    cur,did=did,asset=asset,key=key,wallet=wallet,
                    tx_hash=tx_hash,proof=proof)
    except psycopg.Error:
        raise HTTPException(503,"Verified fee transfer persistence unavailable") from None


def get_owner_verified_evm_fee(
    *,did:str,asset:str,execution_id:str,
)->dict:
    did=_safe_did(did)
    key=valid_order_key(execution_id)
    if asset not in _ASSETS:
        raise HTTPException(404,"Unknown EVM fee asset")
    db=_db_url();_ensure_schema(db)
    try:
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """SELECT o.fee_due_usd,o.settlement_status,
                              o.settlement_tx_hash,
                              p.state,p.tx_hash,p.stable,p.token_amount_atomic
                       FROM mininex_evm_fee_obligations o
                       LEFT JOIN mininex_evm_verified_fee_payments p
                         ON p.privy_did=o.privy_did AND p.asset=o.asset
                        AND p.execution_id=o.execution_id
                       WHERE o.privy_did=%s AND o.asset=%s
                         AND o.execution_id=%s""",(did,asset,key))
                row=cur.fetchone()
    except psycopg.Error:
        raise HTTPException(503,"Verified performance fee unavailable") from None
    if row is None:
        return {"asset":asset,"execution_id":key,"fee_exists":False,
                "paid_onchain":False}
    if ((row["settlement_status"]=="PAID_VERIFIED")
            !=(row["state"]=="PAID_VERIFIED")
            or (row["settlement_status"]=="PAID_VERIFIED"
                and row["settlement_tx_hash"]!=row["tx_hash"])):
        raise HTTPException(503,"Fee payment and obligation disagree")
    return {
        "asset":asset,"execution_id":key,"fee_exists":True,
        "fee_due_usd":str(row["fee_due_usd"]),
        "payment_state":row["state"] or "AWAITING_OWNER_SIGNATURE",
        "token_symbol":row["stable"],
        "token_amount_atomic":(
            str(row["token_amount_atomic"])
            if row["token_amount_atomic"] is not None else None),
        "paid_onchain":row["state"]=="PAID_VERIFIED",
        "settlement_tx_hash":row["tx_hash"],
    }
