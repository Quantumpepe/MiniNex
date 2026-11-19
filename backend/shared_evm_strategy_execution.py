"""Single-quote, single-reservation orchestration for consented EVM BUY/SELL.

This is an INTERNAL strategy entry point, not a public API or an automatic
scheduler. A future user-started worker must supply immutable strategy IDs,
immutable event IDs and the owner's selected coin settings.

One fresh on-chain quote is used for both the independent capital reservation
and the final Privy transaction. A pre-claim failure cancels only a never-sent
reservation; any SENDING/unknown outcome is NEVER retried or released.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from decimal import Decimal,ROUND_CEILING

import httpx
from fastapi import HTTPException

from .live_orders import valid_order_key
from .shared_evm_swap_receipts import _pair
from .shared_evm_swap_preparation import prepare_owner_evm_swap
from .shared_evm_auto_preflight import inspect_evm_exact_swap
from .shared_evm_policy_reference_rates import (
    _price,fetch_evm_policy_reference_rates,
)
from .live_eth_quotes import _quantity
from .shared_evm_privy_order_submit import (
    _fingerprint_prepared_quote,submit_internal_privy_evm_order,
)
from .shared_evm_coin_budget_reserve import (
    reserve_internal_evm_coin_budget,
    cancel_internal_unsubmitted_evm_reservation,
)
from .storage import _safe_did

LOG=logging.getLogger("mininex.evm_approved_signal")
_ASSETS=frozenset(("BNB","POL","AVAX","LINK","UNI","ARB"))
_SIGNAL_REF=re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")


def derive_evm_strategy_signal_key(
    *,did:str,asset:str,side:str,stable:str,
    strategy_run_id:str,signal_event_id:str,
)->str:
    """One durable order key per server-originated strategy event.

    Requoting or changing an event's trade terms cannot produce a new key
    and circumvent the unique original-signal reservation. The run/event
    identifiers are internal worker provenance, never client input.
    """
    did=_safe_did(did)
    if (type(asset) is not str or asset not in _ASSETS
            or type(strategy_run_id) is not str
            or not _SIGNAL_REF.fullmatch(strategy_run_id)
            or type(signal_event_id) is not str
            or not _SIGNAL_REF.fullmatch(signal_event_id)):
        raise ValueError("Untrusted server strategy event provenance")
    pair=_pair(asset,stable,side)
    material=json.dumps(
        [did,asset,pair["chain_id"],strategy_run_id,signal_event_id],
        separators=(",",":"),ensure_ascii=True,
    ).encode("ascii")
    return valid_order_key("sig_"+hashlib.sha256(material).hexdigest()[:60])


def verified_evm_quote_usd_notional(
    *,prepared:dict,asset:str,side:str,stable:str,amount:str,
    prices:dict,now:int,
)->str:
    """Reserve the genuine source-token value, never caller-supplied USD.

    BUY: exact stablecoin input x fresh USD reference.
    SELL: exact coin input x fresh USD reference. The quote's minimum
    output is not an honest measure of an already-held coin position.
    Rounded UP to the database's six USD decimal places.
    """
    pair=_pair(asset,stable,side)
    if (type(prepared) is not dict
            or prepared.get("asset")!=asset
            or prepared.get("side")!=side
            or prepared.get("stable")!=stable
            or prepared.get("chain_id")!=pair["chain_id"]
            or prepared.get("network")!=pair["network"]
            or prepared.get("input_amount")!=amount
            or prepared.get("source_token_address")!=pair["source_token"]
            or prepared.get("output_token_address")!=pair["output_token"]
            or type(prepared.get("wallet_address")) is not str
            or type(now) is not int or now<=0):
        raise ValueError("Verified EVM quote and event terms differ")
    tx=prepared.get("unsigned_transaction")
    if type(tx) is not dict or type(tx.get("data")) is not str:
        raise ValueError("Missing verified EVM calldata")
    data=tx["data"]
    if (not data.startswith("0x04e45aaf")
            or len(data)!=2+8+7*64):
        raise ValueError("Invalid original V3 Router02 calldata")
    try:
        input_atomic=str(int(data[10+4*64:10+5*64],16))
        min_out_atomic=str(int(data[10+5*64:10+6*64],16))
        _,requested_atomic=_quantity(amount,pair["source_decimals"])
        if input_atomic!=str(requested_atomic):
            raise ValueError("Signal quantity differs from actual DEX swap")
        inspect_evm_exact_swap(
            tx,asset=asset,wallet=prepared["wallet_address"],
            side=side,stable=stable,input_atomic=input_atomic,
            minimum_output_atomic=min_out_atomic)
        price=_price(prices,stable if side=="BUY" else asset,now)
        notional=(Decimal(input_atomic)
                  / (Decimal(10)**pair["source_decimals"])
                  *price).quantize(Decimal("0.000001"),rounding=ROUND_CEILING)
        if notional<=0 or notional>=Decimal("1e22"):
            raise ValueError("Invalid positive USD reservation")
        return format(notional,"f")
    except (KeyError,TypeError,ValueError,ArithmeticError):
        raise ValueError("EVM amount or current USD reference unverified") from None


async def execute_internal_evm_strategy_signal(
    *,did:str,asset:str,strategy_run_id:str,signal_event_id:str,
    side:str,stable:str,amount:str,budget_revision:int,
    client:httpx.AsyncClient,
)->dict:
    """Process a single server-originated strategy event without requoting.

    No HTTP endpoint invokes this function. Fresh Privy rules, signed
    consent, a started LIVE session, a current owner quote and a durable
    original signal reservation are rechecked before every actual send.
    """
    did=_safe_did(did)
    if (asset not in _ASSETS
            or side not in ("BUY","SELL")
            or type(amount) is not str
            or type(budget_revision) is not int or budget_revision<1
            or type(client) is not httpx.AsyncClient):
        raise ValueError("Untrusted internal EVM strategy signal")
    key=derive_evm_strategy_signal_key(
        did=did,asset=asset,side=side,stable=stable,
        strategy_run_id=strategy_run_id,signal_event_id=signal_event_id)

    prepared=await prepare_owner_evm_swap(
        did,asset=asset,side=side,stable=stable,amount=amount)
    quote_sha=_fingerprint_prepared_quote(prepared)
    rates=await fetch_evm_policy_reference_rates(asset,stable)
    amount_usd=verified_evm_quote_usd_notional(
        prepared=prepared,asset=asset,side=side,stable=stable,
        amount=amount,prices=rates,now=int(time.time()))
    # Reserve ONLY the owner's chosen per-coin budget, using the amount
    # derived from this exact DEX calldata and current USD market reference.
    reserve_internal_evm_coin_budget(
        did=did,asset=asset,intent_key=key,side=side,stable=stable,
        amount_usd=amount_usd,quote_sha256=quote_sha,
        expected_revision=budget_revision)
    try:
        return await submit_internal_privy_evm_order(
            did=did,asset=asset,intent_key=key,side=side,
            stable=stable,amount=amount,
            budget_revision=budget_revision,client=client,
            prepared_quote=prepared)
    except BaseException:
        # Release only if the order has NEVER been durably claimed as
        # SENDING. Cancellation and claim both lock the same owner row.
        # Cancellation failure must not trigger another Privy POST.
        try:
            cancel_internal_unsubmitted_evm_reservation(
                did=did,asset=asset,intent_key=key,
                quote_sha256=quote_sha)
        except Exception:
            LOG.exception("EVM original strategy reservation needs reconciliation")
        raise
