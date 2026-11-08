"""MiniNex persisted budget *preferences*, never trading authorization.

A Privy DID must be independently verified by the API before entry. Budgets are
saved as preferences only; no signer, wallet rights or live orders are enabled.
"""
from __future__ import annotations

import logging

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .storage import _db_url, _ensure_schema, _prepare_rows, _safe_did, ASSETS

LOG = logging.getLogger("mininex.budgets")


def validate_percent(percent: object) -> int:
    if type(percent) is not int or not 0 <= percent <= 100:
        raise ValueError("budget percent must be an integer from 0 to 100")
    return percent


def validate_revision(revision: object) -> int:
    if type(revision) is not int or not 0 <= revision < 2_000_000_000:
        raise ValueError("invalid revision")
    return revision


def _snapshot(cur, did: str) -> dict:
    cur.execute(
        """SELECT asset,percent,revision FROM mininex_budget_preferences
           WHERE privy_did=%s ORDER BY asset""", (did,)
    )
    rows = list(cur.fetchall())
    if len(rows) != len(ASSETS):
        raise HTTPException(503, "Budget preferences are not available")
    return {
        "mode": "PREFERENCES_ONLY",
        "trading_enabled": False,
        "signing_authorized": False,
        "budgets": {row["asset"]: {
            "percent": row["percent"], "revision": row["revision"],
            "authorized": False,
        } for row in rows}
    }


def get_budget_preferences(privy_did: str) -> dict:
    did = _safe_did(privy_did)
    try:
        url = _db_url()
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                result = _snapshot(cur, did)
        return result
    except psycopg.Error:
        LOG.warning("MiniNex budget storage unavailable")
        raise HTTPException(503, "Budget preferences are not available") from None


def save_budget_preference(
    privy_did: str, asset: str, percent: int, expected_revision: int
) -> dict:
    did = _safe_did(privy_did)
    if asset not in ASSETS:
        raise ValueError("unsupported asset")
    pct = validate_percent(percent)
    version = validate_revision(expected_revision)
    try:
        url = _db_url()
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                cur.execute(
                    """UPDATE mininex_budget_preferences
                       SET percent=%s,revision=revision+1,updated_at=now()
                       WHERE privy_did=%s AND asset=%s AND revision=%s
                       RETURNING revision""",
                    (pct, did, asset, version),
                )
                if cur.fetchone() is None:
                    raise HTTPException(409, "Budget changed; refresh and try again")
                result = _snapshot(cur, did)
        return result
    except psycopg.Error:
        LOG.warning("MiniNex budget storage unavailable")
        raise HTTPException(503, "Budget preferences are not available") from None
