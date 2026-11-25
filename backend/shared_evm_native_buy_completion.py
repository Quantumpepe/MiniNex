"""One-shot native EVM BUY completion: finalized wrapped token -> native coin.

Only BNB/POL/AVAX, only after a real owner BUY swap was independently
FINALIZED_SUCCESS and its exact wrapped-token fill persisted. The original
owner consent, current remote Privy grant, active coin session and exact
native-withdraw policy must all agree. This module does not create grants,
start a session or make a new trading decision. No retries after SENDING.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal,InvalidOperation
import hashlib
import json
import os
import re
import time

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .live_orders import valid_order_key
from .shared_evm_remote_grant_audit import (
    get_owner_evm_remote_grant_audit,_read_grant_record,
    _read_grant_evidence_locked,validate_pinned_evm_policy,_lte,
)
from .shared_evm_allowance import check_evm_source_allowance
from .shared_evm_swap_receipts import _pair
from .shared_native_evm_conversion import bound_native_evm_conversion
from .shared_evm_privy_transport import post_claimed_privy_evm_rpc
from .eth_privy_p256_signer import sign_privy_rpc_payload
from .storage import _db_url,_ensure_schema,_safe_did

_NATIVE=frozenset(("ETH","BNB","POL","AVAX"))
_HASH=re.compile(r"^0x[0-9a-f]{64}$")
_SHA=re.compile(r"^[0-9a-f]{64}$")
_REQUIRED=("owner_wallet_current","exact_remote_policy_rules_verified",
           "privy_additional_signer_scoped","backend_quorum_current",
           "signed_coin_budget_intent_in_ledger")


def original_native_step_key(*,did:str,asset:str,key:str)->str:
    """One distinct, immutable POST identity per original BUY swap."""
    did=_safe_did(did)
    key=valid_order_key(key)
    if asset not in _NATIVE:
        raise ValueError("Not an enabled native EVM coin")
    return "native_"+hashlib.sha256(json.dumps(
        [did,asset,key,"WITHDRAW"],separators=(",",":")
    ).encode("ascii")).hexdigest()[:56]


def actual_buy_fill_wei(row:object,*,asset:str,stable:str)->int:
    """Derive exact native conversion amount from trusted REAL fill table."""
    pair=_pair(asset,stable,"BUY")
    if (asset not in _NATIVE or type(row) is not dict
            or row.get("side")!="BUY"
            or row.get("stable")!=stable
            or row.get("chain_id")!=pair["chain_id"]
            or type(row.get("destination_amount")) not in (Decimal,str)):
        raise ValueError("Missing finalized wrapped-native BUY amount")
    try:
        value=Decimal(str(row["destination_amount"]))
        atomic=value*Decimal(10)**18
        if (not value.is_finite() or not 0<value<Decimal("1e22")
                or atomic!=atomic.to_integral_value()
                or not 0<atomic<Decimal(10)**45):
            raise ValueError("Non-exact real native BUY fill")
        return int(atomic)
    except (InvalidOperation,TypeError,OverflowError):
        raise ValueError("Invalid realized wrapped-native amount") from None


def _post_buy_claim_locked(
    cur,*,did:str,asset:str,key:str,record:dict,
    amount_wei:int,digest:str,central:bool=False,
    remote_verified_policy_sha256:str|None=None,
)->None:
    """Under one account lock, prove full owner trade and claim SENDING."""
    if (asset not in _NATIVE or type(record) is not dict
            or type(amount_wei) is not int or not 0<amount_wei<10**45
            or type(digest) is not str or not _SHA.fullmatch(digest)):
        raise ValueError("Invalid native EVM conversion claim")
    stable=record.get("stable")
    pair=_pair(asset,stable,"BUY")
    if central:
        from .shared_evm_central_runtime import (
            _locked_original_evm_coin,central_policy_rule,
        )
        grant=_locked_original_evm_coin(
            cur,did=did,asset=asset,stable=stable,
            revision=record["budget_revision"],
        )
        if (type(remote_verified_policy_sha256) is not str
                or grant["policy_sha256"]!=remote_verified_policy_sha256):
            raise HTTPException(409,"Original native completion Privy policy changed")
        native_rule=central_policy_rule(
            grant,asset=asset,kind="native_withdraw",
            stable=stable,now=int(time.time()),
        )
    else:
        grant=_read_grant_evidence_locked(cur,did=did,asset=asset)
        policy=validate_pinned_evm_policy(
            grant,asset=asset,now=int(time.time()),
        )
        native_rule=policy["rules"][3]
    cap=int(_lte(native_rule,"withdraw.wad"))
    if (grant.get("wallet_id")!=record.get("wallet_id")
            or grant.get("wallet_address")!=record.get("wallet_address")
            or grant.get("policy_sha256")!=record.get("policy_sha256")
            or grant.get("chain_id")!=pair["chain_id"]
            or amount_wei>cap):
        raise HTTPException(409,"Owner's approved native conversion differs")
    cur.execute(
        """SELECT status,chain_id,stable,wallet_address,budget_revision
           FROM mininex_live_evm_auto_sessions
           WHERE privy_did=%s AND asset=%s FOR UPDATE""",(did,asset))
    session=cur.fetchone()
    if (type(session) is not dict or not (session.get("status")=="ACTIVE" or
            (session.get("status")=="EXIT_PENDING" and central and asset=="ETH"))
            or session.get("chain_id")!=pair["chain_id"]
            or session.get("stable")!=stable
            or session.get("wallet_address")!=record["wallet_address"]
            or session.get("budget_revision")!=grant["budget_revision"]):
        raise HTTPException(409,"Native conversion requires user's active coin session")
    # Keep lock order consistent with the Router02 send and native SELL:
    # permission/consent -> owner session -> owner wallet nonce account.
    # Acquiring the account first while a swap locks grant/session first
    # could deadlock at the moment a real conversion and swap overlap.
    cur.execute(
        """SELECT privy_did FROM mininex_accounts
           WHERE privy_did=%s FOR UPDATE""",(did,))
    owner=cur.fetchone()
    if type(owner) is not dict or owner.get("privy_did")!=did:
        raise HTTPException(409,"Owner EVM account lock unavailable")
    cur.execute(
        """SELECT status,tx_hash,side,stable,chain_id,wallet_address
           FROM mininex_live_evm_auto_broadcasts
           WHERE privy_did=%s AND asset=%s AND intent_key=%s FOR UPDATE""",
        (did,asset,key))
    order=cur.fetchone()
    if (type(order) is not dict
            or order.get("status")!="FINALIZED_SUCCESS"
            or order.get("side")!="BUY"
            or order.get("stable")!=stable
            or order.get("chain_id")!=pair["chain_id"]
            or order.get("wallet_address")!=record["wallet_address"]
            or type(order.get("tx_hash")) is not str
            or not _HASH.fullmatch(order["tx_hash"])):
        raise HTTPException(409,"No finalized owner BUY swap to unwrap")
    cur.execute(
        """SELECT side,stable,chain_id,tx_hash,destination_amount
           FROM mininex_live_evm_verified_fills
           WHERE privy_did=%s AND asset=%s AND intent_key=%s FOR UPDATE""",
        (did,asset,key))
    fill=cur.fetchone()
    if (type(fill) is not dict
            or fill.get("tx_hash")!=order["tx_hash"]
            or actual_buy_fill_wei(fill,asset=asset,stable=stable)!=amount_wei):
        raise HTTPException(409,"Native conversion differs from confirmed BUY fill")
    cur.execute(
        """SELECT (SELECT COUNT(*) FROM mininex_live_evm_auto_broadcasts
               WHERE privy_did=%s AND chain_id=%s AND status IN
                 ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED'))
             + (SELECT COUNT(*) FROM mininex_evm_auto_fee_sends
               WHERE privy_did=%s AND chain_id=%s AND status IN
                 ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED'))
             + (SELECT COUNT(*) FROM mininex_live_evm_router_approval_sends
               WHERE privy_did=%s AND chain_id=%s AND status IN
                 ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED'))
             + (SELECT COUNT(*) FROM mininex_avax_staged_steps
               WHERE privy_did=%s AND chain_id=%s AND state IN
                 ('SENDING','SUBMITTED','UNCERTAIN'))
             + (SELECT COUNT(*) FROM mininex_across_staged_steps
               WHERE privy_did=%s AND chain_id=%s AND state IN
                 ('SENDING','SUBMITTED','UNCERTAIN'))
             + (SELECT COUNT(*) FROM mininex_ethereum_settlement_submissions s
               WHERE s.privy_did=%s AND s.source_chain_id=%s AND s.state IN
                 ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')
               AND NOT EXISTS (SELECT 1 FROM mininex_ethereum_settlement_finality f WHERE f.quote_id=s.quote_id))
             + (SELECT COUNT(*) FROM mininex_live_evm_native_sell_intents
               WHERE privy_did=%s AND chain_id=%s AND status IN
                 ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED'))
             AS pending""",
        (did,pair["chain_id"],did,pair["chain_id"],did,pair["chain_id"],did,pair["chain_id"],did,pair["chain_id"],did,pair["chain_id"],did,pair["chain_id"]))
    other=cur.fetchone()
    if type(other) is not dict or other.get("pending")!=0:
        raise HTTPException(409,"Same-network wallet transaction unresolved")
    cur.execute(
        """SELECT COUNT(*) AS pending FROM mininex_live_evm_native_auto_steps
           WHERE privy_did=%s AND chain_id=%s AND status IN
             ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')""",
        (did,pair["chain_id"]))
    pending=cur.fetchone()
    if type(pending) is not dict or pending.get("pending")!=0:
        raise HTTPException(409,"Native network wallet send unresolved")
    cur.execute(
        """INSERT INTO mininex_live_evm_native_auto_steps
           (privy_did,asset,intent_key,operation,chain_id,stable,
            wallet_address,wallet_id,amount_wei,request_digest,status)
           VALUES (%s,%s,%s,'WITHDRAW',%s,%s,%s,%s,%s,%s,'SENDING')""",
        (did,asset,key,pair["chain_id"],stable,
         record["wallet_address"],record["wallet_id"],amount_wei,digest))


def _persist_native_result_locked(
    cur,*,did:str,asset:str,key:str,digest:str,tx_hash:str|None,
)->str:
    if type(digest) is not str or not _SHA.fullmatch(digest):
        raise ValueError("Native EVM request digest missing")
    if tx_hash is not None and (type(tx_hash) is not str
                                or not _HASH.fullmatch(tx_hash)):
        raise ValueError("Unverified native transaction hash")
    state="SUBMITTED" if tx_hash else "RECONCILIATION_REQUIRED"
    cur.execute(
        """UPDATE mininex_live_evm_native_auto_steps
           SET status=%s,tx_hash=%s,updated_at=now()
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
             AND operation='WITHDRAW' AND request_digest=%s
             AND status='SENDING' RETURNING status""",
        (state,tx_hash,did,asset,key,digest))
    saved=cur.fetchone()
    if type(saved) is not dict or saved.get("status")!=state:
        raise HTTPException(409,"Native conversion original send state changed")
    return state


def _persist_native_result(**kwargs)->str:
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            return _persist_native_result_locked(cur,**kwargs)


async def submit_internal_native_evm_buy_completion(
    *,did:str,asset:str,intent_key:str,client:httpx.AsyncClient,
    central:bool=False,
)->dict:
    """One real Privy native WITHDRAW after the actual finalized BUY fill."""
    did=_safe_did(did)
    key=valid_order_key(intent_key)
    if asset not in _NATIVE or type(client) is not httpx.AsyncClient:
        raise ValueError("Unsupported internal completed native BUY")
    report=(await get_owner_evm_remote_grant_audit(
        did,asset,central=True,
    ) if central else await get_owner_evm_remote_grant_audit(did,asset))
    if type(report) is not dict or report.get("asset")!=asset or any(
        report.get(field) is not True for field in _REQUIRED
    ):
        raise HTTPException(409,"Current owner-approved native grant not verified")
    record=await asyncio.to_thread(
        _read_grant_record,did,asset,
        **({"central":True} if central else {}),
    )
    if central and (
        record["policy_sha256"]!=report.get("remote_verified_policy_sha256")
        or record["wallet_id"]!=report.get("remote_verified_wallet_id")
        or record["signer_id"]!=report.get("remote_verified_signer_id")
    ):
        raise HTTPException(409,"Original Privy native completion grant changed")
    # A separate read of the real final swap fill determines the exact
    # quantity. The claim checks the same data again under FOR UPDATE.
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT side,stable,chain_id,tx_hash,destination_amount
                   FROM mininex_live_evm_verified_fills
                   WHERE privy_did=%s AND asset=%s AND intent_key=%s""",
                (did,asset,key))
            fill=cur.fetchone()
    try:
        amount_wei=actual_buy_fill_wei(
            fill,asset=asset,stable=record["stable"])
    except (ValueError,KeyError,TypeError):
        raise HTTPException(409,"Verified native BUY output unavailable") from None
    amount=format(Decimal(amount_wei)/Decimal(10**18),"f")
    source=await check_evm_source_allowance(
        did,asset=asset,side="SELL",stable=record["stable"],amount=amount)
    if (source.get("wallet_address")!=record["wallet_address"]
            or source.get("source_funds_sufficient") is not True
            or source.get("native_gas_available") is not True):
        raise HTTPException(409,"Native conversion requires verified wallet balance")
    url,body=bound_native_evm_conversion(
        asset=asset,wallet_id=record["wallet_id"],
        wallet=record["wallet_address"],operation="WITHDRAW",
        amount_wei=amount_wei)
    digest=hashlib.sha256(json.dumps(
        [url,body],sort_keys=True,separators=(",",":")
    ).encode("ascii")).hexdigest()
    try:
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _post_buy_claim_locked(
                    cur,did=did,asset=asset,key=key,record=record,
                    amount_wei=amount_wei,digest=digest,
                    **({
                        "central":True,
                        "remote_verified_policy_sha256":
                            report["remote_verified_policy_sha256"],
                    } if central else {}))
    except psycopg.Error:
        raise HTTPException(503,"Cannot claim original native BUY completion") from None
    tx_hash=None
    native_key=original_native_step_key(did=did,asset=asset,key=key)
    try:
        signature=sign_privy_rpc_payload(
            url=url,body=body,idempotency_key=native_key)
        tx_hash=await post_claimed_privy_evm_rpc(
            url=url,body=body,
            app_secret=os.getenv("MININEX_PRIVY_APP_SECRET",""),
            authorization_signature=signature,idempotency_key=native_key,
            client=client)
    except Exception:
        await asyncio.to_thread(
            _persist_native_result,did=did,asset=asset,key=key,
            digest=digest,tx_hash=None)
        raise
    await asyncio.to_thread(
        _persist_native_result,did=did,asset=asset,key=key,
        digest=digest,tx_hash=tx_hash)
    return {"asset":asset,"intent_key":key,
            "operation":"WITHDRAW","state":"SUBMITTED",
            "tx_hash":tx_hash,"native_trade_cycle_settled":False,
            "automatic_resend_allowed":False}
