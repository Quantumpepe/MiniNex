"""BTC-owned LIVE risk settings and emergency stop, independent from ETH.

These settings never create a native BTC signer, deposit address, funding
instruction, swap route, Privy delegation or automatic trading permission.
BTC policies and cross-chain settlement are NOT configured by this endpoint.
"""
from __future__ import annotations

from decimal import Decimal
import logging

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .live_eth_controls import validate_limits, _rev
from .storage import _db_url, _ensure_schema, _prepare_rows, _safe_did

LOG = logging.getLogger("mininex.live_btc_controls")


def _snapshot(row: dict) -> dict:
    return {
        "asset":"BTC",
        "network":"bitcoin-mainnet",
        "mode":"BTC_LIVE_LIMITS_PREPARATION_ONLY",
        "planned_capital_usd":str(row["planned_capital_usd"]),
        "max_order_usd":str(row["max_order_usd"]),
        "max_daily_turnover_usd":str(row["max_daily_turnover_usd"]),
        "revision":row["revision"],
        "trading_paused":row["trading_paused"],
        "privy_btc_signer_verified":False,
        "btc_crosschain_execution_available":False,
        "native_utxo_spend_authorized":False,
        "paper_budget_used":False,
        "live_trading_enabled":False,
        "automated_signing_enabled":False,
        "description":"Separate native BTC LIVE limits, no wallet delegation or real orders.",
    }


def _locked_row(cur, did: str) -> dict:
    cur.execute(
        """SELECT planned_capital_usd,max_order_usd,max_daily_turnover_usd,
                  revision,trading_paused
           FROM mininex_live_btc_limits
           WHERE privy_did=%s FOR UPDATE""",
        (did,),
    )
    row=cur.fetchone()
    if type(row) is not dict:
        raise HTTPException(503,"BTC limits unavailable")
    return row


def get_live_btc_limits(privy_did: str) -> dict:
    did=_safe_did(privy_did)
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur,did)
                return _snapshot(_locked_row(cur,did))
    except psycopg.Error:
        LOG.warning("BTC live limits temporarily unavailable")
        raise HTTPException(503,"BTC live limits unavailable") from None


def save_live_btc_limits(privy_did: str, *, capital: str, order: str,
                         daily: str, expected_revision: int) -> dict:
    did=_safe_did(privy_did)
    cap,each,turnover=validate_limits(capital,order,daily)
    revision=_rev(expected_revision)
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur,did)
                cur.execute(
                    """UPDATE mininex_live_btc_limits
                       SET planned_capital_usd=%s,max_order_usd=%s,
                           max_daily_turnover_usd=%s,trading_paused=TRUE,
                           revision=revision+1,updated_at=now()
                       WHERE privy_did=%s AND revision=%s
                       RETURNING planned_capital_usd,max_order_usd,
                                 max_daily_turnover_usd,revision,trading_paused""",
                    (cap,each,turnover,did,revision),
                )
                row=cur.fetchone()
                if row is None:
                    raise HTTPException(409,"BTC limits changed; refresh and retry")
                return _snapshot(row)
    except psycopg.Error:
        LOG.warning("BTC live limits could not be saved")
        raise HTTPException(503,"BTC live limits unavailable") from None


def stop_live_btc(privy_did: str) -> dict:
    """Pause BTC trading without ever resuming or signing a transaction."""
    did=_safe_did(privy_did)
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur,did)
                row=_locked_row(cur,did)
                if row["trading_paused"] is not True:
                    cur.execute(
                        """UPDATE mininex_live_btc_limits
                           SET trading_paused=TRUE,revision=revision+1,updated_at=now()
                           WHERE privy_did=%s
                           RETURNING planned_capital_usd,max_order_usd,
                                     max_daily_turnover_usd,revision,trading_paused""",
                        (did,),
                    )
                    row=cur.fetchone()
                    if row is None:
                        raise HTTPException(503,"Cannot stop BTC LIVE limits")
                return _snapshot(row)
    except psycopg.Error:
        LOG.warning("BTC stop switch could not be checked")
        raise HTTPException(503,"BTC stop switch unavailable") from None
