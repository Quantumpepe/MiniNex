"""Durable MiniNex account and HWM ledger on PostgreSQL.

Never interpolate a DID or event ID into SQL; all user identifiers are bound
parameters and authenticated by Privy upstream. No public fee write API exists.
Neither a wallet balance nor ownership nor trade permission is inferred here.
"""
from __future__ import annotations

import logging
import os
from decimal import Decimal
from threading import Lock

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .fees import RATE, assess_realized_gain, decimal_amount

LOG = logging.getLogger("mininex.storage")
_BOOTSTRAPPED = False
_BOOTSTRAP_LOCK = Lock()
ASSETS = ("BTC", "ETH")

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS mininex_accounts (
        privy_did TEXT PRIMARY KEY,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS mininex_asset_hwm (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        asset TEXT NOT NULL CHECK (asset IN ('BTC','ETH')),
        cumulative_realized_net_usd NUMERIC(36,12) NOT NULL DEFAULT 0,
        high_water_mark_usd NUMERIC(36,12) NOT NULL DEFAULT 0
            CHECK (high_water_mark_usd >= 0),
        total_fees_assessed_usd NUMERIC(36,12) NOT NULL DEFAULT 0
            CHECK (total_fees_assessed_usd >= 0),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (privy_did,asset)
    )""",
    """CREATE TABLE IF NOT EXISTS mininex_fee_events (
        privy_did TEXT NOT NULL,
        asset TEXT NOT NULL,
        execution_id TEXT NOT NULL,
        realized_net_delta_usd NUMERIC(36,12) NOT NULL,
        cumulative_realized_net_usd NUMERIC(36,12) NOT NULL,
        high_water_mark_usd NUMERIC(36,12) NOT NULL,
        fee_rate NUMERIC(10,8) NOT NULL DEFAULT 0.05 CHECK (fee_rate = 0.05),
        fee_assessed_usd NUMERIC(36,12) NOT NULL DEFAULT 0
            CHECK (fee_assessed_usd >= 0),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (privy_did,asset,execution_id),
        FOREIGN KEY (privy_did,asset)
            REFERENCES mininex_asset_hwm(privy_did,asset)
    )""",
)


def _db_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise HTTPException(503, "MiniNex persistent database not configured")
    if not url.startswith(("postgresql://", "postgres://")):
        raise HTTPException(503, "MiniNex persistent database URL invalid")
    return url


def _ensure_schema(url: str) -> None:
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    with _BOOTSTRAP_LOCK:
        if _BOOTSTRAPPED:
            return
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor() as cur:
                for statement in SCHEMA:
                    cur.execute(statement)
        _BOOTSTRAPPED = True


def _safe_did(privy_did: str) -> str:
    if not isinstance(privy_did, str) or not privy_did.startswith("did:privy:") or len(privy_did) > 256:
        raise ValueError("verified Privy DID required")
    return privy_did


def _prepare_rows(cur, did: str) -> None:
    cur.execute(
        """INSERT INTO mininex_accounts(privy_did) VALUES (%s)
           ON CONFLICT (privy_did) DO UPDATE SET last_seen_at=now()""",
        (did,),
    )
    for asset in ASSETS:
        cur.execute(
            """INSERT INTO mininex_asset_hwm(privy_did,asset) VALUES (%s,%s)
               ON CONFLICT (privy_did,asset) DO NOTHING""",
            (did,asset),
        )


def get_account_summary(privy_did: str) -> dict:
    """Create/read a private account only after successful JWT verification."""
    did = _safe_did(privy_did)
    url = _db_url()
    try:
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                cur.execute(
                    """SELECT asset, cumulative_realized_net_usd,high_water_mark_usd,
                              total_fees_assessed_usd
                       FROM mininex_asset_hwm WHERE privy_did=%s ORDER BY asset""",
                    (did,),
                )
                ledgers = list(cur.fetchall())
    except psycopg.Error:
        LOG.warning("MiniNex database unavailable (error details withheld)")
        raise HTTPException(503, "MiniNex database currently unavailable") from None

    return {
        "authenticated": True,
        "account_persistent": True,
        "mode": "PAPER_ONLY_NO_LIVE_ORDERS",
        "wallet_connected": False,
        "trading_enabled": False,
        "performance_fee_pct": "5.00",
        "hwm_mode": "REALIZED_NET_USD_PREPARED_NOT_COLLECTED",
        "ledger": {
            row["asset"]: {
                "cumulative_realized_net_usd": str(row["cumulative_realized_net_usd"]),
                "high_water_mark_usd": str(row["high_water_mark_usd"]),
                "total_fees_assessed_usd": str(row["total_fees_assessed_usd"]),
            }
            for row in ledgers
        },
    }


def record_trusted_realized_close(
    *, privy_did: str, asset: str, execution_id: str, realized_net_usd: object
) -> dict:
    """Server-INTERNAL accounting only. NOT called by current paper executor.

    Execute only after an independently confirmed, immutable live trade fill and
    correct cost basis are reconciled. Actual fee collection is not implemented.
    A repeated execution ID is idempotent even under concurrent workers.
    """
    did = _safe_did(privy_did)
    if asset not in ASSETS:
        raise ValueError("asset must be BTC or ETH")
    if not isinstance(execution_id, str) or not 1 <= len(execution_id) <= 180:
        raise ValueError("invalid execution id")
    if any(c.isspace() for c in execution_id):
        raise ValueError("execution id cannot contain whitespace")
    delta = decimal_amount(realized_net_usd)
    url = _db_url()
    _ensure_schema(url)
    with psycopg.connect(url, connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            _prepare_rows(cur,did)
            cur.execute(
                """SELECT cumulative_realized_net_usd,high_water_mark_usd
                   FROM mininex_asset_hwm WHERE privy_did=%s AND asset=%s FOR UPDATE""",
                (did,asset),
            )
            row = cur.fetchone()
            cur.execute(
                """SELECT fee_assessed_usd,cumulative_realized_net_usd,high_water_mark_usd
                   FROM mininex_fee_events
                   WHERE privy_did=%s AND asset=%s AND execution_id=%s""",
                (did,asset,execution_id),
            )
            existing=cur.fetchone()
            if existing:
                return {"duplicate":True,"fee_assessed_usd":str(existing["fee_assessed_usd"]),
                        "high_water_mark_usd":str(existing["high_water_mark_usd"]),
                        "cumulative_realized_net_usd":str(existing["cumulative_realized_net_usd"])}
            assessment=assess_realized_gain(
                row["cumulative_realized_net_usd"],row["high_water_mark_usd"],delta,
            )
            cur.execute(
                """INSERT INTO mininex_fee_events
                   (privy_did,asset,execution_id,realized_net_delta_usd,
                    cumulative_realized_net_usd,high_water_mark_usd,fee_rate,fee_assessed_usd)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (did,asset,execution_id,delta,assessment.cumulative_usd,
                 assessment.high_water_mark_usd,RATE,assessment.fee_assessed_usd),
            )
            cur.execute(
                """UPDATE mininex_asset_hwm
                   SET cumulative_realized_net_usd=%s,high_water_mark_usd=%s,
                       total_fees_assessed_usd=total_fees_assessed_usd+%s,
                       updated_at=now()
                   WHERE privy_did=%s AND asset=%s""",
                (assessment.cumulative_usd,assessment.high_water_mark_usd,
                 assessment.fee_assessed_usd,did,asset),
            )
    return {"duplicate":False,"fee_assessed_usd":str(assessment.fee_assessed_usd),
            "high_water_mark_usd":str(assessment.high_water_mark_usd),
            "cumulative_realized_net_usd":str(assessment.cumulative_usd)}
