"""Privy-identity-bound trade settings; never a signer permission.

All preferences are validated and persisted in PostgreSQL. These settings
do not change public PAPER strategies or enable wallet transactions.
"""
from __future__ import annotations

import logging
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from .storage import _db_url, _ensure_schema, _prepare_rows, _safe_did, ASSETS

LOG = logging.getLogger("mininex.trade_preferences")
MODES = ("LONG", "SPOT_SHORT")
STABLES = ("USDC", "USDT")
HORIZONS = ("5m", "15m", "1h", "24h")


def validate_choice(name: str, value: object, choices: tuple[str, ...]) -> str:
    if type(value) is not str or value not in choices:
        raise ValueError(f"invalid {name}")
    return value


def validate_revision(value: object) -> int:
    if type(value) is not int or not 0 <= value < 2_000_000_000:
        raise ValueError("invalid revision")
    return value


def _snapshot(cur, did: str) -> dict:
    cur.execute(
        """SELECT asset,mode,stable,horizon,revision
           FROM mininex_trade_preferences WHERE privy_did=%s ORDER BY asset""",
        (did,),
    )
    rows = list(cur.fetchall())
    if len(rows) != len(ASSETS):
        raise HTTPException(503, "Trade settings unavailable")
    return {
        "mode": "PREFERENCES_ONLY",
        "trading_enabled": False,
        "signing_authorized": False,
        "settings": {
            row["asset"]: {
                "mode": row["mode"],
                "stable": row["stable"],
                "horizon": row["horizon"],
                "revision": row["revision"],
                "authorized": False,
            }
            for row in rows
        },
    }


def get_trade_preferences(privy_did: str) -> dict:
    did = _safe_did(privy_did)
    try:
        url = _db_url()
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                return _snapshot(cur, did)
    except psycopg.Error:
        LOG.warning("MiniNex trade settings storage unavailable")
        raise HTTPException(503, "Trade settings unavailable") from None


def save_trade_preference(
    privy_did: str, asset: str, mode: str, stable: str,
    horizon: str, expected_revision: int,
) -> dict:
    did = _safe_did(privy_did)
    if asset not in ASSETS:
        raise ValueError("invalid asset")
    mode = validate_choice("mode", mode, MODES)
    stable = validate_choice("stable", stable, STABLES)
    horizon = validate_choice("horizon", horizon, HORIZONS)
    revision = validate_revision(expected_revision)
    try:
        url = _db_url()
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                cur.execute(
                    """UPDATE mininex_trade_preferences
                       SET mode=%s,stable=%s,horizon=%s,
                           revision=revision+1,updated_at=now()
                       WHERE privy_did=%s AND asset=%s AND revision=%s
                       RETURNING revision""",
                    (mode, stable, horizon, did, asset, revision),
                )
                if cur.fetchone() is None:
                    raise HTTPException(409, "Trade settings changed; reload and try again")
                return _snapshot(cur, did)
    except psycopg.Error:
        LOG.warning("MiniNex trade settings write unavailable")
        raise HTTPException(503, "Trade settings unavailable") from None
