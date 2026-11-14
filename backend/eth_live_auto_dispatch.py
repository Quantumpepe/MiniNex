"""Trusted ETH AUTO signal -> verified quote -> atomic reserve -> real Privy send.

No public route. This is the integration point a future LIVE strategist worker
must call with a server-generated signal. It never reads PAPER fills or starts
trading merely because a PAPER session is running.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import time

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .eth_router02_live_quote import make_policy_compatible_eth_quote
from .eth_privy_policy_plan import fetch_reference_prices, _parsed_price
from .eth_auto_order_submit import submit_privy_eth_auto_order
from .eth_privy_policy_ledger import get_eth_policy_ledger_status
from .live_eth_reservations import _reserve_under_eth_limits_lock
from .live_eth_quotes import _quantity
from .privy_live_signer import get_eth_signer_status
from .storage import _db_url,_ensure_schema,_safe_did


@dataclass(frozen=True)
class EthAutoSignal:
    strategy_run_id: str
    signal_event_id: str
    side: str
    stable: str
    amount: str
    source: str="LIVE_STRATEGY"
    # IMPORTANT: an internal strategy event is a *proposal* based on real
    # positions; no PAPER-generated order/fill is accepted.
    is_paper: bool=False


def exact_swap_quote_terms(quote:dict, signal:EthAutoSignal,
                           prices:dict, *, now:int)->dict:
    """Extract exact on-chain terms and USD reservation from real quote."""
    if (type(signal) is not EthAutoSignal
            or signal.is_paper is not False
            or signal.source!="LIVE_STRATEGY"
            or signal.side not in ("BUY","SELL")
            or signal.stable not in ("USDC","USDT")
            or type(quote) is not dict
            or quote.get("mode")!="ETH_ROUTER02_POLICY_ABI_QUOTE_ONLY"
            or quote.get("wallet_identity_verified") is not True
            or quote.get("unsigned_swap_validated") is not True
            or quote.get("side")!=signal.side
            or quote.get("stable")!=signal.stable
            or quote.get("input_amount")!=signal.amount
            or quote.get("execution_enabled") is not False
            or type(quote.get("quoted_at")) is not int
            or not 0 <= now-quote["quoted_at"] <= 10
            or quote.get("quote_ttl_seconds")!=15):
        raise ValueError("Stale or untrusted LIVE ETH strategy trade quote")
    tx=quote.get("transaction")
    if type(tx) is not dict or tx.get("chainId")!=1:
        raise ValueError("No wallet-bound Router02 transaction")
    _,units=_quantity(signal.amount,6 if signal.side=="BUY" else 18)
    _,min_out=_quantity(quote.get("minimum_swap_output_amount"),
                        18 if signal.side=="BUY" else 6)
    # The user's budget is USD, not an assumed paper USDC=1 USD.
    stable_price=_parsed_price(prices,signal.stable,now)
    stable_units=(Decimal(signal.amount) if signal.side=="BUY"
        else Decimal(quote["expected_swap_output_amount"]))
    notional=(stable_units*stable_price).quantize(
        Decimal("0.000001"),rounding=ROUND_CEILING)
    if not Decimal("0")<notional<Decimal("10000000"):
        raise ValueError("LIVE ETH notional unavailable")
    raw=json.dumps(
        [tx,signal.side,signal.stable,signal.amount,
         quote["minimum_swap_output_amount"],quote["quoted_at"]],
        separators=(",",":"),sort_keys=True).encode()
    return {"tx":tx,"input_atomic":str(units),
            "minimum_output_atomic":str(min_out),
            "quote_digest":hashlib.sha256(raw).hexdigest(),
            "notional_usd":format(notional,"f"),
            "quote_expires_at":quote["quoted_at"]+15}


def _reserve_verified_live_signal(did:str, signal:EthAutoSignal,
                                  terms:dict, signer:dict, policy:dict)->dict:
    """Atomic signal/order binding; no HTTP send while PostgreSQL locked."""
    did=_safe_did(did)
    if (signer.get("signer_verified") is not True
            or signer.get("policy_reference_verified") is not True
            or policy.get("status")!="GRANT_VERIFIED"
            or policy.get("live_limits_revision_matches") is not True):
        raise HTTPException(409,"Privy ETH user grant is not verified")
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                # Reservation helper locks LIVE limits then checks duplicate
                # signal digest and outstanding reserved+confirmed exposure.
                return _reserve_under_eth_limits_lock(
                    cur,did=did,strategy_run_id=signal.strategy_run_id,
                    signal_event_id=signal.signal_event_id,
                    side=signal.side,stable=signal.stable,
                    proposed_usd=terms["notional_usd"],
                    quote_digest=terms["quote_digest"],
                    privy_execution_proof={
                        "wallet_owner_verified":True,
                        "signer_and_restricted_policy_verified":True,
                        "router_calldata_policy_verified":True,
                        "revocable_user_consent_verified":True,
                    })
    except psycopg.Error:
        raise HTTPException(503,"ETH AUTO reservation unavailable") from None


async def execute_eth_auto_signal(
    did:str,signal:EthAutoSignal, *,
    client:httpx.AsyncClient,
)->dict:
    """One real strategy event, one real wallet submit after user permission."""
    did=_safe_did(did)
    if type(signal) is not EthAutoSignal or signal.is_paper or (
        signal.source!="LIVE_STRATEGY"):
        raise HTTPException(422,"Only verified live strategy signals may trade")
    quote,prices,signer=await asyncio.gather(
        make_policy_compatible_eth_quote(
            did,side=signal.side,stable=signal.stable,
            amount=signal.amount,include_user_transaction=True),
        fetch_reference_prices(),
        get_eth_signer_status(did))
    policy=await asyncio.to_thread(get_eth_policy_ledger_status,did)
    now=int(time.time())
    try:
        terms=exact_swap_quote_terms(quote,signal,prices,now=now)
    except (ValueError,TypeError,KeyError):
        raise HTTPException(409,"ETH real quote not usable for AUTO") from None
    claim=await asyncio.to_thread(
        _reserve_verified_live_signal,did,signal,terms,signer,policy)
    if claim["claim"]=="DUPLICATE_NO_NEW_ORDER":
        return claim
    if claim.get("claim")!="RESERVED_NO_SEND":
        raise HTTPException(503,"ETH AUTO strategy order not reserved")
    if time.time()>=terms["quote_expires_at"]:
        # The durable RESERVED order remains reviewable; never sign stale quotes.
        raise HTTPException(409,"ETH quote expired before wallet signing")
    return await submit_privy_eth_auto_order(
        did=did,intent_key=claim["intent_key"],
        tx=terms["tx"],side=signal.side,stable=signal.stable,
        input_atomic=terms["input_atomic"],
        minimum_output_atomic=terms["minimum_output_atomic"],
        client=client)
