"""Internal-only ETH reservation transaction core (disabled in production).

The public LIVE reservation entrypoint remains hard-disabled. This module
builds the exact atomic PostgreSQL contract for a *future* privileged worker.
No route, API, scheduled job, user JWT, configured wallet or paper session
can invoke the writer. No transaction is signed or broadcast.

Only a future separately reviewed caller may use the private transaction core,
after verifying revocable Privy authority and Router-compatible policy.
"""
from __future__ import annotations

from decimal import Decimal
import logging

from fastapi import HTTPException

from .live_eth_controls import validate_spend_limits
from .live_orders import (
    _RESERVED_STATES,
    build_strategy_signal_identity,
    classify_existing_signal_binding,
    valid_notional,
)
from .storage import _safe_did

LOG = logging.getLogger("mininex.eth_reservation")


def _number(value: object, *, allow_zero: bool) -> str:
    """Exact, bounded SQL numeric; never silently round provider floats."""
    if isinstance(value, Decimal):
        value = str(value)
    if type(value) is not str:
        raise ValueError("Non-exact internal USD amount")
    return str(valid_notional(value, allow_zero=allow_zero))


def _reserve_under_eth_limits_lock(
    cur,
    *, did: str, strategy_run_id: str, signal_event_id: str,
    side: str, stable: str, proposed_usd: str, quote_digest: str,
    privy_execution_proof: object,
) -> dict:
    """Atomic future claim; caller owns ONE open PostgreSQL transaction.

    This helper MUST NOT be called until a separate Privy policy, signer,
    explicit user consent and exact calldata/nonce checker exists. The
    current production wrapper below always refuses before calling it.

    Always acquire the same account ETH limit row lock before examining or
    updating live intent/binding rows. Another worker must wait on that lock.
    Both inserts must commit together; any unique collision rolls back both.
    """
    did = _safe_did(did)
    identity = build_strategy_signal_identity(
        did, asset="ETH",
        strategy_run_id=strategy_run_id, signal_event_id=signal_event_id,
        side=side, stable=stable,
        proposed_usd=proposed_usd, quote_digest=quote_digest,
    )
    # Do NOT treat a login token, configured policy ID or paper budget as a
    # Privy proof. The complete permission protocol has not been deployed.
    if (type(privy_execution_proof) is not dict
            or privy_execution_proof.get("wallet_owner_verified") is not True
            or privy_execution_proof.get("signer_and_restricted_policy_verified") is not True
            or privy_execution_proof.get("router_calldata_policy_verified") is not True
            or privy_execution_proof.get("revocable_user_consent_verified") is not True):
        raise HTTPException(409, "Verified Privy execution authority required")

    cur.execute(
        """SELECT planned_capital_usd,max_order_usd,max_daily_turnover_usd,
                  trading_paused,revision
           FROM mininex_live_eth_limits
           WHERE privy_did=%s FOR UPDATE""",
        (did,),
    )
    controls = cur.fetchone()
    if not isinstance(controls, dict):
        raise HTTPException(503, "Locked live ETH limits unavailable")
    if controls.get("trading_paused") is not False:
        raise HTTPException(409, "LIVE ETH trading stopped")

    # A replayed signal is NEVER submitted again, even if a previous order
    # was cancelled/expired/reverted or is currently uncertain on-chain.
    cur.execute(
        """SELECT signal_digest,intent_key,payload_digest
           FROM mininex_live_signal_bindings
           WHERE privy_did=%s AND asset=%s AND signal_digest=%s""",
        (did, "ETH", identity["signal_digest"]),
    )
    existing = cur.fetchone()
    decision = classify_existing_signal_binding(existing, identity)
    if decision == "ALREADY_CLAIMED_DO_NOT_SEND":
        return {
            "claim": "DUPLICATE_NO_NEW_ORDER",
            "asset": "ETH",
            "intent_key": identity["intent_key"],
            "safe_to_send": False,
        }

    # Never paginate this read: even an old SENDING or REORG_PENDING state
    # counts against account exposure and daily turnover until reconciled.
    cur.execute(
        """SELECT
               COALESCE(SUM(reserved_notional_usd) FILTER (
                   WHERE state=ANY(%s)),0) AS pending_usd,
               COALESCE(SUM(reserved_notional_usd) FILTER (
                   WHERE state='CONFIRMED'
                     AND finalized_at >= now() - INTERVAL '24 hours'),0)
                   AS confirmed_24h_usd
           FROM mininex_live_order_intents
           WHERE privy_did=%s AND asset=%s""",
        (sorted(_RESERVED_STATES), did, "ETH"),
    )
    totals = cur.fetchone()
    if not isinstance(totals, dict):
        raise HTTPException(503, "Cannot verify existing ETH exposure")

    notional = _number(proposed_usd, allow_zero=False)
    pending = _number(totals.get("pending_usd"), allow_zero=True)
    confirmed = _number(totals.get("confirmed_24h_usd"), allow_zero=True)
    # Pending orders consume both reserved capital and rolling turnover.
    proposed = validate_spend_limits(
        budget={
            **controls,
            "privy_grant_verified": True,  # Only after guarded proof above.
        },
        order_usd=notional,
        reserved_usd=pending,
        spent_today_usd=str(Decimal(pending) + Decimal(confirmed)),
    )

    # Two writes MUST be inside the caller's SAME DB transaction, under the
    # limit row lock. No ON CONFLICT retry that silently changes the order.
    cur.execute(
        """INSERT INTO mininex_live_order_intents
               (privy_did,asset,chain_network,intent_key,
                side,stable,state,reserved_notional_usd)
           VALUES (%s,'ETH','ethereum-mainnet',%s,%s,%s,'RESERVED',%s)""",
        (did, identity["intent_key"], side, stable, proposed),
    )
    cur.execute(
        """INSERT INTO mininex_live_signal_bindings
               (privy_did,asset,signal_digest,intent_key,payload_digest)
           VALUES (%s,'ETH',%s,%s,%s)""",
        (did, identity["signal_digest"], identity["intent_key"],
         identity["payload_digest"]),
    )
    return {
        "claim": "RESERVED_NO_SEND",
        "asset": "ETH",
        "intent_key": identity["intent_key"],
        "notional_usd": str(proposed),
        "safe_to_send": False,
    }


def prepare_live_eth_reservation(*args, **kwargs) -> None:
    """Intentionally no connection/transaction at this activation stage."""
    raise HTTPException(
        503,
        "ETH signing, delegation and atomic LIVE reservations not activated",
    )
