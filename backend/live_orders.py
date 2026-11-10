"""Durable, account-scoped LIVE order audit ledger shared by ETH and future BTC.

This module is *not* an executor. It never signs, broadcasts, triggers a quote,
accepts a paper trade, or turns a budget setting into wallet authority.

The intent schema and state machine are designed to prevent retries from
creating duplicate on-chain orders when send results are uncertain. A future
signed engine must reserve atomically, persist SENDING *before* broadcasting,
then reconcile chain receipts before deciding whether a retry is possible.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import logging
import re

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .storage import _db_url, _ensure_schema, _prepare_rows, _safe_did

LOG = logging.getLogger("mininex.live_orders")
ASSET_CHAINS = {"ETH": "ethereum-mainnet", "BTC": "bitcoin-mainnet"}
ORDER_STATES = (
    "RESERVED",
    "SENDING",
    "RECONCILIATION_REQUIRED",
    "SUBMITTED",
    "CONFIRMED",
    "REVERTED",
    "REORG_PENDING",
    "CANCELLED",
    "EXPIRED",
)
_TERMINAL_STATES = frozenset({"CONFIRMED", "REVERTED", "CANCELLED", "EXPIRED"})
_RESERVED_STATES = frozenset({
    "RESERVED", "SENDING", "RECONCILIATION_REQUIRED", "SUBMITTED",
    "REORG_PENDING",
})
# From-state to allowed next state. RECONCILIATION_REQUIRED may NOT be
# cancelled just because a request timed out: it might already have broadcast.
_TRANSITIONS = {
    "RESERVED": frozenset({"SENDING", "CANCELLED", "EXPIRED"}),
    "SENDING": frozenset({"SUBMITTED", "RECONCILIATION_REQUIRED"}),
    "RECONCILIATION_REQUIRED": frozenset({"SUBMITTED", "REVERTED"}),
    "SUBMITTED": frozenset({"CONFIRMED", "REVERTED", "REORG_PENDING"}),
    "REORG_PENDING": frozenset({"SUBMITTED", "CONFIRMED", "REVERTED"}),
    "CONFIRMED": frozenset(),
    "REVERTED": frozenset(),
    "CANCELLED": frozenset(),
    "EXPIRED": frozenset(),
}
_ORDER_KEY = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{7,95}$")
_TX_HASH = re.compile(r"^(?:0x)?[a-fA-F0-9]{64}$")
_BUDGET_USD = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$")


def valid_asset(asset: object) -> str:
    if type(asset) is not str or asset not in ASSET_CHAINS:
        raise HTTPException(404, "Unsupported LIVE asset")
    return asset


def valid_order_key(key: object) -> str:
    if type(key) is not str or not _ORDER_KEY.fullmatch(key):
        raise ValueError("Invalid internal order idempotency key")
    return key


def valid_notional(raw: object) -> Decimal:
    """Only six-digit exact USD amounts, no float rounding/scientific notation."""
    if type(raw) is not str or len(raw) > 48 or not _BUDGET_USD.fullmatch(raw):
        raise ValueError("Invalid exact USD notional")
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        raise ValueError("Invalid exact USD notional") from None
    if not amount.is_finite() or not 0 < amount < 10_000_000:
        raise ValueError("USD notional outside supported range")
    return amount


def verify_transition(current: str, target: str, *,
                      tx_hash: str | None = None,
                      confirmed_receipt: bool = False) -> None:
    """Reject accidental retries and false settlement on uncertain sends.

    A future executor must validate the on-chain receipt independently; this
    helper makes receipt proof a prerequisite but is NOT itself a receipt
    verifier. There is currently no public or internal caller transitioning
    LIVE orders, so a fabricated bool cannot move money through an API.
    """
    if target not in _TRANSITIONS.get(current, frozenset()):
        raise ValueError("Invalid order status transition")
    if target in {"SUBMITTED", "CONFIRMED", "REVERTED", "REORG_PENDING"}:
        if not isinstance(tx_hash, str) or not _TX_HASH.fullmatch(tx_hash):
            raise ValueError("A verified blockchain transaction hash is required")
    if target in {"CONFIRMED", "REVERTED"} and confirmed_receipt is not True:
        raise ValueError("Independently checked blockchain receipt required")


def serialize_order(row: dict) -> dict:
    """No private signer data, price assumptions or wallet secrets in API."""
    state = row["state"]
    if state not in ORDER_STATES:
        raise ValueError("Corrupt LIVE order state")
    asset = valid_asset(row["asset"])
    notional = valid_notional(str(row["reserved_notional_usd"]))
    created = row["created_at"]
    if not isinstance(created, datetime) or created.tzinfo is None:
        raise ValueError("Invalid order timestamp")
    return {
        "id": valid_order_key(row["intent_key"]),
        "asset": asset,
        "network": ASSET_CHAINS[asset],
        "side": row["side"] if row["side"] in ("BUY", "SELL") else None,
        "stable": row["stable"] if row["stable"] in ("USDC", "USDT") else None,
        "status": state,
        "reserved_notional_usd": str(notional),
        "created_at": created.astimezone(timezone.utc).isoformat(),
        "submitted_at": row.get("submitted_at").astimezone(timezone.utc).isoformat()
                        if isinstance(row.get("submitted_at"), datetime)
                        and row["submitted_at"].tzinfo else None,
        "tx_hash": row.get("tx_hash") if (
            isinstance(row.get("tx_hash"), str)
            and _TX_HASH.fullmatch(row["tx_hash"])
        ) else None,
        "final": state in _TERMINAL_STATES,
        "uncertain": state in {"SENDING", "RECONCILIATION_REQUIRED", "REORG_PENDING"},
    }


def get_live_order_history(privy_did: str, *, asset: str,
                           limit: int = 30) -> dict:
    """Read-only private history; there is intentionally NO HTTP write route."""
    did = _safe_did(privy_did)
    asset = valid_asset(asset)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise HTTPException(422, "Invalid LIVE order history limit")
    try:
        url = _db_url()
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                cur.execute(
                    """SELECT intent_key,asset,side,stable,state,reserved_notional_usd,
                              tx_hash,created_at,submitted_at
                       FROM mininex_live_order_intents
                       WHERE privy_did=%s AND asset=%s
                       ORDER BY created_at DESC,intent_key DESC LIMIT %s""",
                    (did, asset, limit),
                )
                rows = list(cur.fetchall())
        return {
            "mode": "LIVE_ORDER_AUDIT_ONLY",
            "asset": asset,
            "network": ASSET_CHAINS[asset],
            "orders": [serialize_order(row) for row in rows],
            "execution_enabled": False,
            "automatic_signing_enabled": False,
            "can_submit": False,
            "description": "Wallet-bound order history only; real order submission is disabled.",
        }
    except psycopg.Error:
        LOG.warning("LIVE order history unavailable")
        raise HTTPException(503, "LIVE order history unavailable") from None


def build_risk_snapshot(controls: dict, *, unfilled_total_usd: object,
                        confirmed_24h_usd: object, proposed_usd: object) -> dict:
    """Pure future pre-reservation check for atomic row-locked caller.

    The future executor must derive all three USD amounts from independently
    verified quotes and durable order rows while holding the same PostgreSQL
    account/asset limit lock. Never trust client-provided USD numbers.
    """
    from .live_eth_controls import validate_spend_limits

    def exact(value: object, *, zero: bool) -> str:
        if isinstance(value, Decimal):
            value = str(value)
        if type(value) is not str:
            raise ValueError("Internal notional must not be an approximate float")
        if zero and value == "0":
            return value
        return str(valid_notional(value))

    budget = {
        "trading_paused": controls.get("trading_paused"),
        "privy_grant_verified": controls.get("privy_grant_verified"),
        "planned_capital_usd": controls.get("planned_capital_usd", "0"),
        "max_order_usd": controls.get("max_order_usd", "0"),
        "max_daily_turnover_usd": controls.get("max_daily_turnover_usd", "0"),
    }
    notional = exact(proposed_usd, zero=False)
    outstanding = exact(unfilled_total_usd, zero=True)
    confirmed = exact(confirmed_24h_usd, zero=True)
    accepted = validate_spend_limits(
        budget=budget, order_usd=notional,
        reserved_usd=outstanding, spent_today_usd=(
            str(Decimal(confirmed) + Decimal(outstanding))),
    )
    return {
        "proposed_usd": str(accepted),
        "outstanding_usd": outstanding,
        "rolling_24h_including_pending_usd": str(Decimal(confirmed)+Decimal(outstanding)),
    }


# Fail-closed activation is intentionally hard-coded. This cannot be changed
# by user requests, environment variables, an existing paper session or an
# administrator's accidental change to a budget setting.
LIVE_RESERVATION_ACTIVATED = False


def prepare_live_order_reservation(*args, **kwargs) -> None:
    """Future integration hook, disabled until real privileged execution exists.

    No partial reservation is created and no funds are transferred.
    """
    raise HTTPException(503, "LIVE order reservation and execution not yet enabled")
