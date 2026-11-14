"""One account-owned strategy template reused by every MiniNex trading coin.

Change the shared strategy MODE/HORIZON once. All eleven coins inherit it
when a new session is configured; independent budgets, holdings, stablecoin
selection, trade history and running sessions are never reset or shared.
BTC/ETH legacy saved preferences are synchronized in the same DB transaction.
No wallet signing or live orders occur here.
"""
from __future__ import annotations

import logging
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .live_asset_registry import ASSET_ORDER
from .storage import _db_url, _ensure_schema, _prepare_rows, _safe_did
from .trade_preferences import (
    MODES, HORIZONS, validate_choice, validate_revision,
    _snapshot as _core_settings_snapshot,
)

LOG = logging.getLogger("mininex.unified_strategy_profile")
TRADING_ASSETS = tuple(symbol for symbol in ASSET_ORDER if symbol != "ADA")
CORE_ASSETS = ("BTC", "ETH")
assert len(TRADING_ASSETS) == 11 and TRADING_ASSETS[:2] == CORE_ASSETS


def _profile(cur, did: str) -> dict:
    cur.execute(
        """SELECT mode,horizon,revision
           FROM mininex_unified_strategy_profiles
           WHERE privy_did=%s""",
        (did,),
    )
    row = cur.fetchone()
    if row is None:
        raise HTTPException(503, "Shared MiniNex strategy not available")
    return {
        "mode": "SHARED_STRATEGY_PREFERENCES",
        "strategy_mode": row["mode"],
        "horizon": row["horizon"],
        "revision": row["revision"],
        "assets": list(TRADING_ASSETS),
        "ada_in_progress": True,
        "independent_budgets_preserved": True,
        "existing_sessions_unchanged": True,
        "wallet_authorized": False,
        "real_trading_enabled": False,
    }


def get_strategy_profile(privy_did: str) -> dict:
    did = _safe_did(privy_did)
    try:
        url = _db_url()
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                return _profile(cur, did)
    except psycopg.Error:
        LOG.warning("Shared MiniNex strategy read unavailable")
        raise HTTPException(503, "Shared strategy settings unavailable") from None


def save_strategy_profile(privy_did: str, *, mode: str, horizon: str,
                          expected_revision: int) -> dict:
    """Atomic shared-mode update; no individual coin-by-coin save required."""
    did = _safe_did(privy_did)
    mode = validate_choice("mode", mode, MODES)
    horizon = validate_choice("horizon", horizon, HORIZONS)
    revision = validate_revision(expected_revision)
    try:
        url = _db_url()
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                cur.execute(
                    """UPDATE mininex_unified_strategy_profiles
                       SET mode=%s,horizon=%s,revision=revision+1,
                           updated_at=now()
                       WHERE privy_did=%s AND revision=%s
                       RETURNING revision""",
                    (mode, horizon, did, revision),
                )
                if cur.fetchone() is None:
                    raise HTTPException(409, "Shared strategy changed; refresh and try again")
                # Existing BTC/ETH mode and horizon are persisted in exactly
                # the same transaction as the shared template. Their per-coin
                # settlement choices and budget preferences are untouched.
                cur.execute(
                    """UPDATE mininex_trade_preferences
                       SET mode=%s,horizon=%s,revision=revision+1,
                           updated_at=now()
                       WHERE privy_did=%s AND asset = ANY(%s)""",
                    (mode, horizon, did, list(CORE_ASSETS)),
                )
                if cur.rowcount != len(CORE_ASSETS):
                    raise HTTPException(503, "Could not synchronize core strategy settings")
                result = _profile(cur, did)
                result["core_settings"] = _core_settings_snapshot(cur, did)["settings"]
                return result
    except psycopg.Error:
        LOG.warning("Shared MiniNex strategy write unavailable")
        raise HTTPException(503, "Shared strategy settings unavailable") from None
