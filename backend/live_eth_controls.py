"""Privy-identity-bound LIVE ETH risk limits, stop switch and reservation ledger.

Not a signer. Stored budgets do NOT authorize moving user funds. No API may
unpause, register a Privy signer, or submit a transaction at this stage.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import logging
import re

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .storage import _db_url, _ensure_schema, _prepare_rows, _safe_did

LOG = logging.getLogger("mininex.live_eth_controls")
MONEY_MAX = Decimal("10000000")
MONEY_PLACES = Decimal("0.000001")
_MONEY_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$")
_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{7,95}$")


def _money(value: object, *, allow_zero: bool) -> Decimal:
    if not isinstance(value, str) or len(value) > 48 or not _MONEY_RE.fullmatch(value):
        raise HTTPException(422, "Live limits require exact non-negative decimal strings")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise HTTPException(422, "Invalid live amount") from None
    if (not result.is_finite() or result >= MONEY_MAX
            or result < 0 or (result == 0 and not allow_zero)
            or result.as_tuple().exponent < -6):
        raise HTTPException(422, "Live amount outside supported limits")
    return result


def validate_limits(capital: object, order: object, daily: object) -> tuple[Decimal, Decimal, Decimal]:
    """Inputs are USD *caps*, not balances; no exchange, approval or authorization."""
    cap = _money(capital, allow_zero=True)
    per_order = _money(order, allow_zero=True)
    per_day = _money(daily, allow_zero=True)
    if (cap == 0 or per_order == 0 or per_day == 0):
        if cap != 0 or per_order != 0 or per_day != 0:
            raise HTTPException(422, "Live limits must be all zero or all nonzero")
    elif per_order > cap or per_order > per_day:
        raise HTTPException(422, "Order cap exceeds capital or daily allowance")
    return cap, per_order, per_day


def _rev(value: object) -> int:
    if type(value) is not int or not 0 <= value < 2_000_000_000:
        raise HTTPException(422, "Invalid live limit revision")
    return value


def _snapshot(row: dict) -> dict:
    """Never turn stored limits or paper settings into a trading permission."""
    return {
        "asset": "ETH",
        "chain_id": 1,
        "mode": "LIVE_LIMITS_PREPARATION_ONLY",
        "planned_capital_usd": str(row["planned_capital_usd"]),
        "max_order_usd": str(row["max_order_usd"]),
        "max_daily_turnover_usd": str(row["max_daily_turnover_usd"]),
        "revision": row["revision"],
        "trading_paused": row["trading_paused"],
        "privy_grant_verified": False,
        "live_trading_enabled": False,
        "automated_signing_enabled": False,
        "description": "Separate LIVE risk limits; never the paper budget. No user grant or order authority.",
    }


def _locked_row(cur, did: str) -> dict:
    cur.execute(
        """SELECT planned_capital_usd,max_order_usd,max_daily_turnover_usd,
                  revision,trading_paused
           FROM mininex_live_eth_limits WHERE privy_did=%s FOR UPDATE""",
        (did,),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(503, "LIVE ETH controls unavailable")
    return row


def _invalidate_eth_user_authority_locked(cur, did: str) -> dict:
    """Local emergency hold, while the LIVE limits row is already locked.

    Persist consent revoke and policy REVOKE_REQUESTED in the *same*
    transaction as the stop/limit revision change. This is NOT remote Privy
    signer removal: a wallet owner may still need to remove their signer
    through the separately confirmed Privy action.
    """
    cur.execute(
        """UPDATE mininex_live_eth_consent_intents
           SET status='REVOKED',revoked_at=COALESCE(revoked_at,now())
           WHERE privy_did=%s AND status<>'REVOKED'
           RETURNING status""", (did,),
    )
    local = cur.fetchone()
    cur.execute(
        """UPDATE mininex_live_eth_policy_enrollments
           SET status='REVOKE_REQUESTED',updated_at=now()
           WHERE privy_did=%s AND status<>'REVOKED'
           RETURNING status""", (did,),
    )
    policy = cur.fetchone()
    return {
        "local_eth_consent_invalidated": type(local) is dict
            and local.get("status") == "REVOKED",
        "privy_remote_revocation_requested": type(policy) is dict
            and policy.get("status") == "REVOKE_REQUESTED",
        "privy_remote_signer_removed": False,
        "real_trading_enabled": False,
    }


def get_live_eth_limits(privy_did: str) -> dict:
    did = _safe_did(privy_did)
    try:
        url = _db_url()
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                row = _locked_row(cur, did)
                return _snapshot(row)
    except psycopg.Error:
        LOG.warning("LIVE ETH limits unavailable")
        raise HTTPException(503, "LIVE ETH controls unavailable") from None


def save_live_eth_limits(privy_did: str, *, capital: str, order: str,
                         daily: str, expected_revision: int) -> dict:
    """Save user risk preferences only. Every change retains the kill switch."""
    did = _safe_did(privy_did)
    cap, each, turnover = validate_limits(capital, order, daily)
    revision = _rev(expected_revision)
    try:
        url = _db_url()
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                cur.execute(
                    """UPDATE mininex_live_eth_limits
                       SET planned_capital_usd=%s,max_order_usd=%s,
                           max_daily_turnover_usd=%s,
                           trading_paused=TRUE,revision=revision+1,updated_at=now()
                       WHERE privy_did=%s AND revision=%s
                       RETURNING planned_capital_usd,max_order_usd,
                                 max_daily_turnover_usd,revision,trading_paused""",
                    (cap, each, turnover, did, revision),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(409, "LIVE limits changed; reload before saving")
                held = _invalidate_eth_user_authority_locked(cur, did)
                return {**_snapshot(row), **held}
    except psycopg.Error:
        LOG.warning("LIVE ETH limit save unavailable")
        raise HTTPException(503, "LIVE ETH limits could not be saved") from None


def stop_live_eth(privy_did: str) -> dict:
    """Always available to authenticated users; fail closed for new entries.

    This stops MiniNex's future sends, not an already broadcast transaction.
    Privy signer revocation will be integrated separately before enabling LIVE.
    """
    did = _safe_did(privy_did)
    try:
        url = _db_url()
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                row = _locked_row(cur, did)
                if not row["trading_paused"]:
                    cur.execute(
                        """UPDATE mininex_live_eth_limits
                           SET trading_paused=TRUE,revision=revision+1,updated_at=now()
                           WHERE privy_did=%s
                           RETURNING planned_capital_usd,max_order_usd,
                                     max_daily_turnover_usd,revision,trading_paused""",
                        (did,),
                    )
                    row = cur.fetchone()
                held = _invalidate_eth_user_authority_locked(cur, did)
                return {**_snapshot(row), **held}
    except psycopg.Error:
        LOG.warning("LIVE ETH stop unavailable")
        raise HTTPException(503, "LIVE ETH stop state unavailable") from None


def validate_spend_limits(*, budget: dict, order_usd: object,
                          reserved_usd: object, spent_today_usd: object) -> Decimal:
    """Pure pre-send risk gate; future executor MUST call under a DB row lock.

    All inputs must come from verified onchain/transaction ledgers, never a
    user's request or exchange-indicative PAPER prices. The current API has
    no execution path and cannot set trading_paused=False.
    """
    if budget.get("trading_paused") is not False:
        raise HTTPException(409, "LIVE trading is stopped")
    if budget.get("privy_grant_verified") is not True:
        raise HTTPException(409, "Verified Privy spending authorization required")
    notional = _money(order_usd, allow_zero=False)
    reserved = _money(reserved_usd, allow_zero=True)
    spent = _money(spent_today_usd, allow_zero=True)
    cap, each, turnover = validate_limits(
        str(budget["planned_capital_usd"]),
        str(budget["max_order_usd"]),
        str(budget["max_daily_turnover_usd"]),
    )
    if not cap or not each or not turnover:
        raise HTTPException(409, "LIVE risk limits not configured")
    if notional > each or notional + reserved > cap or notional + spent > turnover:
        raise HTTPException(409, "LIVE order exceeds spending limits")
    return notional


def validate_order_key(key: object) -> str:
    if not isinstance(key, str) or not _ID_RE.fullmatch(key):
        raise HTTPException(422, "Invalid idempotency key")
    return key
