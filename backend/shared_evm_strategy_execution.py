"""Single-quote, single-reservation orchestration for consented EVM BUY/SELL.

This is an INTERNAL strategy entry point, not a public API or an automatic
scheduler. A future user-started worker must supply immutable strategy IDs,
actual proposed USD allocation and the owner-selected coin settings.

One fresh on-chain quote is used for both the independent capital reservation
and the final Privy transaction. A pre-claim failure cancels only a never-sent
reservation; any SENDING/unknown outcome is NEVER retried or released.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import HTTPException

from .live_orders import valid_order_key
from .shared_evm_swap_receipts import _pair
from .shared_evm_swap_preparation import prepare_owner_evm_swap
from .shared_evm_privy_order_submit import (
    _fingerprint_prepared_quote,submit_internal_privy_evm_order,
)
from .shared_evm_coin_budget_reserve import (
    reserve_internal_evm_coin_budget,
    cancel_internal_unsubmitted_evm_reservation,
)
from .storage import _safe_did

LOG=logging.getLogger("mininex.evm_approved_signal")


async def execute_internal_evm_strategy_signal(
    *,did:str,asset:str,intent_key:str,side:str,stable:str,
    amount:str,amount_usd:str,budget_revision:int,
    client:httpx.AsyncClient,
)->dict:
    """Process a single server-originated strategy event without requoting.

    No HTTP endpoint invokes this function. Fresh Privy rules, signed
    consent, a started LIVE session, a current owner quote and a durable
    original signal reservation are rechecked before every actual send.
    """
    did=_safe_did(did)
    key=valid_order_key(intent_key)
    if (asset not in ("BNB","POL","AVAX","LINK","UNI")
            or side not in ("BUY","SELL")
            or type(amount) is not str
            or type(amount_usd) is not str
            or type(budget_revision) is not int or budget_revision<1
            or type(client) is not httpx.AsyncClient):
        raise ValueError("Untrusted internal EVM strategy signal")
    _pair(asset,stable,side)

    prepared=await prepare_owner_evm_swap(
        did,asset=asset,side=side,stable=stable,amount=amount)
    quote_sha=_fingerprint_prepared_quote(prepared)
    # This reserves ONLY the selected coin's user-configured budget.
    # It does not authorize a transfer or supply a second quote.
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
