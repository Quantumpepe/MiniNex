"""Durable ETH AUTO WETH9 conversion phases using the REAL Privy signer.

Provides the native ETH leg which Router02 ERC20/WETH alone does not handle.
Each operation belongs to an explicitly started user session and has a stable
idempotency key. No arbitrary calldata, PAPER fills, user-submitted orders,
retransmission on uncertain outcomes, or private keys in API responses.
"""
from __future__ import annotations
import asyncio
from decimal import Decimal
import hashlib
import logging
import os
import re

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .eth_privy_native_rpc import send_privy_native_weth9
from .eth_weth9_receipts import check_user_weth9_receipt
from .privy_wallet_verification import fetch_privy_user
from .privy_live_signer import (
    _owned_eth_wallet, _scoped_signer, fetch_privy_wallet_metadata,
    signer_configuration,
)
from .wallet_balances import get_wallet_balances
from .eth_router02_allowance import inspect_router02_approval
from .real_eth_trade_legs import save_verified_leg,validated_finalized_leg
from .storage import _db_url,_ensure_schema,_safe_did

LOG=logging.getLogger("mininex.eth_native_auto")
_KEY=re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,95}$")
_HASH=re.compile(r"^0x[0-9a-f]{64}$")


def native_intent_key(did:str, source_key:str, operation:str)->str:
    if (type(did) is not str or type(source_key) is not str
            or not _KEY.fullmatch(source_key)
            or operation not in ("DEPOSIT","WITHDRAW")):
        raise ValueError("Native ETH operation requires a durable source")
    return "native_"+hashlib.sha256(
        (did+"|"+source_key+"|"+operation).encode("utf-8")).hexdigest()[:56]


def _atomic_wei(amount:str)->int:
    if type(amount) is not str or len(amount)>72:
        raise ValueError("Exact native ETH amount required")
    value=Decimal(amount)
    wei=value*10**18
    if (not value.is_finite() or value<=0 or value>=10**24
            or wei!=wei.to_integral_value()):
        raise ValueError("Non-exact ETH amount")
    return int(wei)


def _native_claim_locked(cur, *, did:str, source_key:str,
                         operation:str, amount_wei:int,
                         stable:str, wallet:str, wallet_id:str,
                         signer_id:str, policy_id:str, key:str)->bool:
    """Atomic ONCE-only native stage reservation before any Privy HTTP."""
    if operation not in ("DEPOSIT","WITHDRAW") or stable not in ("USDC","USDT"):
        raise ValueError("Invalid native stage")
    if type(amount_wei) is not int or not 0<amount_wei<10**45:
        raise ValueError("Invalid WETH9 amount")
    cur.execute(
        """SELECT trading_paused,revision FROM mininex_live_eth_limits
           WHERE privy_did=%s FOR UPDATE""",(did,))
    limits=cur.fetchone()
    if type(limits) is not dict or limits.get("trading_paused") is not False:
        raise HTTPException(409,"ETH AUTO is paused")
    cur.execute(
        """SELECT run_id,stable,status FROM mininex_live_eth_auto_sessions
           WHERE privy_did=%s FOR UPDATE""",(did,))
    session=cur.fetchone()
    if (type(session) is not dict or session["status"]!="ACTIVE"
            or session["stable"]!=stable):
        raise HTTPException(409,"No active ETH AUTO session")
    cur.execute(
        """SELECT status,policy_id,wallet_address,execution_signer_id,
                  limits_revision,remote_policy_sha256,candidate_sha256,
                  extract(epoch FROM policy_expires_at)::bigint AS expiry
           FROM mininex_live_eth_policy_enrollments
           WHERE privy_did=%s FOR UPDATE""",(did,))
    policy=cur.fetchone()
    import time
    if (type(policy) is not dict or policy.get("status")!="GRANT_VERIFIED"
            or policy.get("policy_id")!=policy_id
            or policy.get("execution_signer_id")!=signer_id
            or str(policy.get("wallet_address","")).lower()!=wallet.lower()
            or policy.get("limits_revision")!=limits["revision"]
            or policy.get("candidate_sha256")!=policy.get("remote_policy_sha256")
            or type(policy.get("expiry")) is not int
            or policy["expiry"]<int(time.time())+30):
        raise HTTPException(409,"Native WETH9 grant expired or revoked")
    cur.execute(
        """SELECT status,revoked_at,limits_revision
           FROM mininex_live_eth_consent_intents
           WHERE privy_did=%s FOR UPDATE""",(did,))
    consent=cur.fetchone()
    if (type(consent) is not dict or consent.get("status")!="SIGNED_INTENT"
            or consent.get("revoked_at") is not None
            or consent.get("limits_revision")!=limits["revision"]):
        raise HTTPException(409,"Native conversion no longer authorized")
    cur.execute(
        """SELECT intent_key,operation,amount_wei,status
           FROM mininex_live_eth_auto_native_steps
           WHERE privy_did=%s AND source_key=%s
             AND operation=%s FOR UPDATE""",(did,source_key,operation))
    existing=cur.fetchone()
    if existing is not None:
        if (existing["intent_key"]!=key
                or existing["operation"]!=operation
                or int(existing["amount_wei"])!=amount_wei):
            raise HTTPException(409,"Native order source changed")
        return False
    # No other ETH swap or native wallet transaction may overlap this
    # conversion. Prevents simultaneous nonces/races on one Privy wallet.
    cur.execute(
        """SELECT count(*) AS pending FROM mininex_live_order_intents
           WHERE privy_did=%s AND asset='ETH' AND state IN
             ('RESERVED','SENDING','SUBMITTED',
              'RECONCILIATION_REQUIRED','REORG_PENDING')""",(did,))
    if cur.fetchone()["pending"]!=0:
        raise HTTPException(409,"ETH swap still pending")
    cur.execute(
        """SELECT count(*) AS pending FROM mininex_live_eth_auto_native_steps
           WHERE privy_did=%s AND status IN
             ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')""",(did,))
    if cur.fetchone()["pending"]!=0:
        raise HTTPException(409,"Prior native ETH transaction unresolved")
    if operation=="WITHDRAW":
        # Only WETH received by a fully finalized confirmed MiniNex BUY
        # may be converted to native ETH without an additional trade.
        cur.execute(
            """SELECT o.stable,o.state,l.destination_amount
               FROM mininex_live_order_intents o
               JOIN mininex_live_eth_verified_legs l
                 ON l.privy_did=o.privy_did AND l.tx_hash=o.tx_hash
               WHERE o.privy_did=%s AND o.asset='ETH'
                 AND o.intent_key=%s AND o.side='BUY'
                 AND l.kind='SWAP'""",(did,source_key))
        buy=cur.fetchone()
        if (type(buy) is not dict or buy["state"]!="CONFIRMED"
                or buy["stable"]!=stable
                or _atomic_wei(str(buy["destination_amount"]))!=amount_wei):
            raise HTTPException(409,"No matched finalized WETH BUY to unwrap")
    cur.execute(
        """INSERT INTO mininex_live_eth_auto_native_steps
           (privy_did,source_key,intent_key,operation,stable,amount_wei,
            wallet_address,wallet_id,status)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'SENDING')""",
        (did,source_key,key,operation,stable,amount_wei,
         wallet.lower(),wallet_id))
    return True


def _persist_native_result(did:str,source_key:str,operation:str, *,
                           tx_hash:str|None):
    state="SUBMITTED" if tx_hash is not None else "RECONCILIATION_REQUIRED"
    if tx_hash is not None and not _HASH.fullmatch(tx_hash):
        raise ValueError("Wrong native transaction hash")
    url=_db_url();_ensure_schema(url)
    with psycopg.connect(url,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """UPDATE mininex_live_eth_auto_native_steps
                   SET status=%s,tx_hash=%s,updated_at=now()
                   WHERE privy_did=%s AND source_key=%s AND operation=%s
                     AND status='SENDING' RETURNING intent_key""",
                (state,tx_hash,did,source_key,operation))
            if cur.fetchone() is None:
                raise HTTPException(409,"Native ETH send state changed")


async def send_eth_auto_native_leg(did:str, *, source_key:str,operation:str,
                                  amount_wei:int,stable:str,
                                  client:httpx.AsyncClient)->dict:
    """ONE auto WETH9 send with persisted SENDING and user-owned Privy signer."""
    did=_safe_did(did)
    key=native_intent_key(did,source_key,operation)
    config=signer_configuration()
    if config is None:
        raise HTTPException(503,"ETH Privy signer not registered")
    user=await fetch_privy_user(did)
    owned=_owned_eth_wallet(user,did)
    if not owned["delegated"]:
        raise HTTPException(409,"Wallet signer not delegated")
    wallet=await fetch_privy_wallet_metadata(owned["wallet_id"])
    if not _scoped_signer(wallet,did=did,address=owned["address"],config=config):
        raise HTTPException(409,"Privy WETH9 policy does not match user")
    # Enforce actual owner wallet inventory before any irreversible send.
    if operation=="WITHDRAW":
        check=await inspect_router02_approval(
            did,side="SELL",stable=stable,
            amount=format(Decimal(amount_wei)/10**18,"f"))
        if (check.get("source_funds_sufficient") is not True or
                check.get("conservative_native_gas_reserve_met") is not True):
            raise HTTPException(409,"Confirmed WETH not available for unwrap")
    else:
        balances=await get_wallet_balances(did)
        native=balances.get("balances",{}).get("ETH",{})
        if (balances.get("wallet_ownership_verified") is not True
                or native.get("status")!="verified"
                or _atomic_wei(str(native["balance"]))<=amount_wei+10**16):
            raise HTTPException(409,"Insufficient native ETH including gas reserve")
    try:
        url=_db_url();_ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                created=_native_claim_locked(
                    cur,did=did,source_key=source_key,operation=operation,
                    amount_wei=amount_wei,stable=stable,
                    wallet=owned["address"],wallet_id=owned["wallet_id"],
                    signer_id=config["signer_id"],
                    policy_id=config["policy_id"],key=key)
    except psycopg.Error:
        raise HTTPException(503,"Native ETH order storage unavailable") from None
    if not created:
        return {"asset":"ETH","native_step":"ALREADY_CLAIMED_NO_RESEND",
                "source_key":source_key,"sent":False}
    try:
        tx_hash=await send_privy_native_weth9(
            wallet_id=owned["wallet_id"],wallet=owned["address"],
            operation=operation,amount_wei=amount_wei,
            intent_key=key,
            app_secret=os.environ.get("MININEX_PRIVY_APP_SECRET",""),
            client=client)
    except Exception:
        await asyncio.to_thread(_persist_native_result,
            did,source_key,operation,tx_hash=None)
        raise
    await asyncio.to_thread(_persist_native_result,
        did,source_key,operation,tx_hash=tx_hash)
    return {"asset":"ETH","native_step":operation,
            "tx_hash":tx_hash,"state":"SUBMITTED",
            "onchain_finalized":False}


def submitted_native_steps(limit:int=25)->list[dict]:
    url=_db_url();_ensure_schema(url)
    with psycopg.connect(url,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT privy_did,source_key,operation,stable,amount_wei,tx_hash
                   FROM mininex_live_eth_auto_native_steps
                   WHERE status='SUBMITTED'
                   ORDER BY updated_at ASC LIMIT %s""",(max(1,min(limit,25)),))
            return cur.fetchall()


async def finalize_native_step(row:dict)->str:
    did=_safe_did(row["privy_did"])
    check=await check_user_weth9_receipt(
        did,row["tx_hash"],operation=row["operation"])
    if check.get("state") not in ("FINALIZED_WETH9_CONVERSION","FINALIZED_REVERTED"):
        return "PENDING"
    state=check["state"]
    if check.get("transaction_finalized") is not True:
        return "PENDING"
    if state=="FINALIZED_WETH9_CONVERSION":
        leg=validated_finalized_leg(
            tx_hash=row["tx_hash"],
            side="BUY" if row["operation"]=="WITHDRAW" else "SELL",
            stable=row["stable"],
            kind="UNWRAP" if row["operation"]=="WITHDRAW" else "WRAP",
            receipt=check)
        if _atomic_wei(leg["source_amount"])!=int(row["amount_wei"]):
            raise HTTPException(409,"Native ETH on-chain amount differs from intent")
        await asyncio.to_thread(save_verified_leg,did,leg)
    final="CONFIRMED" if state=="FINALIZED_WETH9_CONVERSION" else "REVERTED"
    await asyncio.to_thread(_finalize_native_record,
                            did,row["source_key"],row["operation"],
                            row["tx_hash"],final)
    return final


def _finalize_native_record(did:str,source_key:str,operation:str,
                            tx_hash:str,final:str):
    url=_db_url();_ensure_schema(url)
    with psycopg.connect(url,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """UPDATE mininex_live_eth_auto_native_steps
                   SET status=%s,updated_at=now()
                   WHERE privy_did=%s AND source_key=%s AND operation=%s
                     AND tx_hash=%s AND status='SUBMITTED'
                   RETURNING intent_key""",
                (final,did,source_key,operation,tx_hash))
            if cur.fetchone() is None:
                raise HTTPException(409,"Native ETH receipt state mismatch")


def confirmed_buy_native_conversions(limit:int=15)->list[dict]:
    """Owner-sourced completed BUY fills still holding purchased WETH."""
    url=_db_url();_ensure_schema(url)
    with psycopg.connect(url,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT o.privy_did,o.intent_key AS source_key,o.stable,
                          l.destination_amount
                   FROM mininex_live_order_intents o
                   JOIN mininex_live_eth_verified_legs l
                     ON l.privy_did=o.privy_did AND l.tx_hash=o.tx_hash
                   JOIN mininex_live_eth_auto_sessions s
                     ON s.privy_did=o.privy_did
                   JOIN mininex_live_eth_limits cap
                     ON cap.privy_did=o.privy_did
                   LEFT JOIN mininex_live_eth_auto_native_steps n
                     ON n.privy_did=o.privy_did
                       AND n.source_key=o.intent_key AND n.operation='WITHDRAW'
                   WHERE o.asset='ETH' AND o.state='CONFIRMED'
                     AND o.side='BUY' AND l.kind='SWAP'
                     AND n.intent_key IS NULL AND s.status='ACTIVE'
                     AND cap.trading_paused=FALSE
                   ORDER BY o.finalized_at ASC LIMIT %s""",
                    (max(1,min(limit,20)),))
            return cur.fetchall()
