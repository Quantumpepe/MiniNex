"""Read-only MiniNex ETH ledger exposure and 24-hour budget audit.

This is NOT an on-chain balance, verified allowance, gas quote, transaction
authorizer or trade executor. It reports per-user PREPARED live risk preferences
and pending/confirmed USD order ledger consumption only. The user's Paper
history is intentionally excluded. No wallet keys or private values are used.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import logging

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .live_eth_controls import validate_limits
from .live_orders import _RESERVED_STATES
from .storage import _db_url, _ensure_schema, _prepare_rows, _safe_did

LOG = logging.getLogger("mininex.eth_spend_audit")
_PENDING = sorted(_RESERVED_STATES)


def _usd_exact(raw: object) -> Decimal:
    if isinstance(raw, Decimal):
        val = raw
    elif type(raw) is str and len(raw) <= 64:
        try:
            val = Decimal(raw)
        except InvalidOperation:
            raise HTTPException(503, "Invalid ETH USD ledger amount") from None
    else:
        raise HTTPException(503, "Invalid ETH USD ledger amount")
    if (
        not val.is_finite() or val < 0 or val >= Decimal("1000000000000")
        or val.as_tuple().exponent < -6
    ):
        raise HTTPException(503, "ETH ledger amount outside safe audit bounds")
    return val


def _count(raw: object) -> int:
    if type(raw) is not int or raw < 0 or raw > 1_000_000_000:
        raise HTTPException(503, "Invalid ETH unsettled order count")
    return raw


def assess_eth_live_ledger(limits: dict, totals: dict) -> dict:
    """Pure read-only accounting; never infer trade authorization.

    Pending unresolved orders count against BOTH capital and the full rolling
    24-hour turnover. Confirmed last-24h orders count against turnover. A
    pending order that is old or uncertain is NEVER silently released.
    """
    if type(limits) is not dict or type(totals) is not dict:
        raise HTTPException(503, "Verified ETH ledger evidence required")
    try:
        cap, order, day = validate_limits(
            str(limits["planned_capital_usd"]),
            str(limits["max_order_usd"]),
            str(limits["max_daily_turnover_usd"]),
        )
    except (KeyError, TypeError, ValueError, HTTPException):
        raise HTTPException(503, "Stored ETH LIVE risk preferences invalid") from None
    rev = limits.get("revision")
    paused = limits.get("trading_paused")
    if type(rev) is not int or rev < 0 or type(paused) is not bool:
        raise HTTPException(503, "Missing LIVE ETH stop state or revision")
    pending = _usd_exact(totals.get("pending_usd"))
    confirmed = _usd_exact(totals.get("confirmed_24h_usd"))
    pending_count = _count(totals.get("pending_count"))
    unreconciled = _count(totals.get("unreconciled_count"))
    stale = _count(totals.get("stale_pending_count"))
    if unreconciled > pending_count or stale > pending_count or (
        pending_count == 0 and pending != 0
    ):
        raise HTTPException(503, "ETH ledger pending count does not match notional")
    if pending_count > 0 and pending <= 0:
        raise HTTPException(503, "ETH ledger has unpriced pending orders")
    capital_available = max(Decimal(0), cap - pending)
    remaining_turnover = max(Decimal(0), day - pending - confirmed)
    blockers = ["LIVE_EXECUTOR_DISABLED"]
    if paused:
        blockers.append("USER_STOPPED")
    if not cap or not order or not day:
        blockers.append("LIMITS_NOT_CONFIGURED")
    if unreconciled:
        blockers.append("UNRECONCILED_LIVE_ORDERS")
    if stale:
        blockers.append("STALE_PENDING_LIVE_ORDERS")
    if pending + confirmed > day:
        blockers.append("ROLLING_TURNOVER_OVER_LIMIT")
    if pending > cap:
        blockers.append("RESERVED_CAPITAL_OVER_LIMIT")
    return {
        "mode":"ETH_LIVE_LEDGER_READ_ONLY",
        "asset":"ETH","chain_id":1,
        "limits_revision":rev,
        "trading_paused":paused,
        "planned_capital_usd":str(cap),
        "max_order_usd":str(order),
        "rolling_24h_limit_usd":str(day),
        "pending_reserved_usd":str(pending),
        "confirmed_24h_usd":str(confirmed),
        "available_capital_after_pending_usd":str(capital_available),
        "available_24h_after_pending_usd":str(remaining_turnover),
        "unresolved_order_count":pending_count,
        "reconciliation_required_count":unreconciled,
        "older_than_30m_pending_count":stale,
        "blockers":blockers,
        "paper_simulation_included":False,
        "current_wallet_balance_verified":False,
        "erc20_allowances_verified":False,
        "cumulative_spend_enforced_at_execution":False,
        "ready_for_live":False,
        "can_submit":False,
        "execution_enabled":False,
    }


def _query_eth_live_ledger(did: str) -> dict:
    url = _db_url()
    try:
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                # Same row-lock ordering a future ETH order writer must use.
                cur.execute(
                    """SELECT planned_capital_usd,max_order_usd,
                              max_daily_turnover_usd,revision,trading_paused
                       FROM mininex_live_eth_limits
                       WHERE privy_did=%s FOR UPDATE""", (did,),
                )
                limits = cur.fetchone()
                if type(limits) is not dict:
                    raise HTTPException(503, "User LIVE limits not found")
                cur.execute(
                    """SELECT
                         COALESCE(SUM(reserved_notional_usd)
                           FILTER (WHERE state=ANY(%s)),0) AS pending_usd,
                         COALESCE(SUM(reserved_notional_usd)
                           FILTER (WHERE state='CONFIRMED'
                             AND finalized_at >= now()-interval '24 hours'),0)
                             AS confirmed_24h_usd,
                         COUNT(*) FILTER (WHERE state=ANY(%s))
                             AS pending_count,
                         COUNT(*) FILTER (WHERE state IN
                           ('SENDING','RECONCILIATION_REQUIRED',
                            'REORG_PENDING')) AS unreconciled_count,
                         COUNT(*) FILTER (WHERE state=ANY(%s)
                           AND created_at < now()-interval '30 minutes')
                             AS stale_pending_count
                       FROM mininex_live_order_intents
                       WHERE privy_did=%s AND asset='ETH'""",
                    (_PENDING,did if False else _PENDING,_PENDING,did),
                )
                totals = cur.fetchone()
                return assess_eth_live_ledger(limits, totals)
    except psycopg.Error:
        LOG.warning("ETH LIVE ledger audit unavailable")
        raise HTTPException(503, "ETH LIVE ledger audit unavailable") from None


def get_eth_live_ledger_audit(did: str) -> dict:
    """Authenticated DID only, not user-supplied wallet or budget."""
    return _query_eth_live_ledger(_safe_did(did))
