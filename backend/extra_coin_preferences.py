"""Saved, independent trading setup for the nine additional MiniNex coins.

This shares the same 11-coin strategy engine as BTC/ETH but stores mode,
budget, settlement and horizon per coin and authenticated Privy user.
Preferences are NOT positions, deposits or permission to trade.
"""
from __future__ import annotations

import logging

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .strategy_replay import EXTRA_REPLAY_ASSETS, MODES, WINDOWS
from .storage import _db_url, _ensure_schema, _prepare_rows, _safe_did
from .trade_preferences import STABLES, validate_choice, validate_revision

LOG = logging.getLogger("mininex.extra_coin_preferences")
COINS = tuple(EXTRA_REPLAY_ASSETS)
DEFAULT = {"mode": "LONG", "stable": "USDC", "horizon": "15m",
           "budget_usd": 500, "revision": 0}
assert len(COINS) == 9 and len(set(COINS)) == 9


def _coin(asset: object) -> str:
    if type(asset) is not str or asset not in COINS:
        raise HTTPException(404, "Coin preference unavailable")
    return asset


def _budget(value: object) -> int:
    if type(value) is not int or not 100 <= value <= 5000:
        raise ValueError("Coin budget must be an integer from 100 to 5000")
    return value


def _read(cur, did: str) -> dict:
    cur.execute(
        """SELECT asset,budget_usd,mode,stable,horizon,revision
           FROM mininex_extra_coin_preferences
           WHERE privy_did=%s""",
        (did,),
    )
    rows = list(cur.fetchall())
    selected = {}
    for row in rows:
        asset = _coin(row["asset"])
        if asset in selected:
            raise ValueError("Duplicate coin preferences")
        data = {field: row[field] for field in DEFAULT}
        if (type(data["revision"]) is not int or data["revision"] <= 0
                or type(data["budget_usd"]) is not int
                or not 100 <= data["budget_usd"] <= 5000
                or data["mode"] not in MODES or data["stable"] not in STABLES
                or data["horizon"] not in WINDOWS):
            raise ValueError("Invalid persisted coin settings")
        selected[asset] = data
    return {
        "mode": "INDEPENDENT_COIN_PREFERENCES",
        "assets": list(COINS),
        "settings": {asset: dict(selected.get(asset, DEFAULT)) for asset in COINS},
        "independent_budgets": True,
        "active_sessions_unchanged": True,
        "wallet_authorized": False,
        "live_trading_enabled": False,
    }


def get_coin_preferences(privy_did: str) -> dict:
    did = _safe_did(privy_did)
    try:
        url = _db_url()
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                return _read(cur, did)
    except (psycopg.Error, ValueError):
        LOG.warning("Independent coin setup read unavailable")
        raise HTTPException(503, "Independent coin settings unavailable") from None


def save_coin_preference(privy_did: str, asset: str, *,
                         budget_usd: int, mode: str, stable: str,
                         horizon: str, expected_revision: int) -> dict:
    did = _safe_did(privy_did)
    asset = _coin(asset)
    budget_usd = _budget(budget_usd)
    mode = validate_choice("mode", mode, MODES)
    stable = validate_choice("stable", stable, STABLES)
    horizon = validate_choice("horizon", horizon, tuple(WINDOWS))
    expected_revision = validate_revision(expected_revision)
    try:
        url = _db_url()
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                # A missing row has virtual revision 0. First save inserts
                # version 1; thereafter exact expected-revision updates only.
                if expected_revision == 0:
                    cur.execute(
                        """INSERT INTO mininex_extra_coin_preferences
                           (privy_did,asset,budget_usd,mode,stable,horizon)
                           VALUES (%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (privy_did,asset) DO NOTHING
                           RETURNING revision""",
                        (did, asset, budget_usd, mode, stable, horizon),
                    )
                else:
                    cur.execute(
                        """UPDATE mininex_extra_coin_preferences
                           SET budget_usd=%s,mode=%s,stable=%s,horizon=%s,
                               revision=revision+1,updated_at=now()
                           WHERE privy_did=%s AND asset=%s AND revision=%s
                           RETURNING revision""",
                        (budget_usd, mode, stable, horizon,
                         did, asset, expected_revision),
                    )
                if cur.fetchone() is None:
                    raise HTTPException(
                        409, "Coin settings changed; refresh and try again")
                return _read(cur, did)
    except psycopg.Error:
        LOG.warning("Independent coin setup write unavailable")
        raise HTTPException(503, "Cannot save coin settings") from None
