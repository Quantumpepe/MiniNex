"""Private, read-only latest PAPER buys and sells across BTC, ETH and nine coins.

The personal paper engines already persist every fill in two account-owned
PostgreSQL tables. This endpoint joins only their READ views, never touches
the simulator, market providers, subscriptions, wallets or real orders.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import math
from typing import Any

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .storage import _db_url, _ensure_schema, _safe_did

LOG=logging.getLogger("mininex.home_paper_trades")

CORE_ASSETS=("BTC","ETH")
EXTRA_ASSETS=("BNB","SOL","XRP","TRX","POL","AVAX","LINK","DOGE","UNI")
ALL_ASSETS=CORE_ASSETS+EXTRA_ASSETS
MAX_TRADES=60


def _number(value: Any, *, positive: bool = False) -> float | None:
    if type(value) not in (int,float,str) or value == "":
        return None
    try:
        n=float(value)
    except (TypeError,ValueError,OverflowError):
        return None
    if not math.isfinite(n) or (positive and n<=0):
        return None
    return round(n,8)


def normalize_home_trade(row: Any) -> dict | None:
    """Validate one saved fill before it reaches the shared home feed."""
    if not isinstance(row,dict):
        return None
    asset=row.get("asset")
    event=row.get("event_json")
    event_id=row.get("event_id")
    when=row.get("occurred_at")
    if (asset not in ALL_ASSETS or type(event_id) is not int or event_id<=0 or
        not isinstance(event,dict) or event.get("asset")!=asset or
        event.get("real_trade") is not False or
        event.get("kind") not in ("PAPER_BUY","PAPER_SELL") or
        not isinstance(when,datetime) or when.tzinfo is None or
        event.get("settlement") not in ("USDC","USDT")):
        return None
    price=_number(event.get("price_reference"),positive=True)
    qty=_number(event.get("quantity"),positive=True)
    kind=event["kind"]
    amount=_number(event.get(
        "total_spent" if kind=="PAPER_BUY" else "net_proceeds"
    ),positive=True)
    profit=(_number(event.get("realized_net_profit")) if kind=="PAPER_SELL"
            else None)
    if any(v is None for v in (price,qty,amount)) or (
        kind=="PAPER_SELL" and profit is None
    ):
        return None
    reason=event.get("reason")
    if not isinstance(reason,str) or not 1<=len(reason)<=120:
        reason="PAPER_TRADE"
    # The actual ledger timestamp is authoritative, not any client clock.
    return {
        "id":asset+":"+str(event_id),
        "asset":asset,"kind":kind,
        "at":when.astimezone(timezone.utc).isoformat(),
        "price_reference":price,"quantity":qty,
        "amount_usd":amount,"realized_net_profit":profit,
        "settlement":event["settlement"],"reason":reason,
        "paper_only":True,"real_trade":False,
    }


def recent_home_trades(did: str,limit: int = MAX_TRADES,
                       asset: str = "ALL") -> dict:
    did=_safe_did(did)
    if type(limit) is not int or not 1<=limit<=MAX_TRADES:
        raise HTTPException(422,"Invalid paper-history limit")
    if asset != "ALL" and asset not in ALL_ASSETS:
        raise HTTPException(422,"Unsupported paper-history asset")
    # Selected coin queries its own ledger BEFORE the LIMIT. This matters:
    # filtering a globally limited list could hide older BTC/ETH/SOL trades
    # when another coin has traded more recently.
    if asset in CORE_ASSETS:
        sql="""SELECT asset,event_id,occurred_at,event_json
          FROM mininex_user_paper_trades
          WHERE privy_did=%s AND asset=%s
          ORDER BY occurred_at DESC,event_id DESC LIMIT %s"""
        params=(did,asset,limit)
    elif asset in EXTRA_ASSETS:
        sql="""SELECT asset,event_id,occurred_at,event_json
          FROM mininex_extra_paper_trades
          WHERE privy_did=%s AND asset=%s
          ORDER BY occurred_at DESC,event_id DESC LIMIT %s"""
        params=(did,asset,limit)
    else:
        # The deliberate "All coins" opt-in preserves the previous global
        # view; both tables remain isolated by the same verified Privy DID.
        sql="""SELECT asset,event_id,occurred_at,event_json
          FROM (
              SELECT asset,event_id,occurred_at,event_json
              FROM mininex_user_paper_trades
              WHERE privy_did=%s AND asset=ANY(%s)
              UNION ALL
              SELECT asset,event_id,occurred_at,event_json
              FROM mininex_extra_paper_trades
              WHERE privy_did=%s AND asset=ANY(%s)
          ) paper
          ORDER BY occurred_at DESC,asset ASC,event_id DESC
          LIMIT %s"""
        params=(did,list(CORE_ASSETS),did,list(EXTRA_ASSETS),limit)
    try:
        url=_db_url();_ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql,params)
                rows=cur.fetchall()
    except psycopg.Error:
        LOG.warning("Private home paper activity unavailable")
        raise HTTPException(503,"Personal simulated trade history unavailable") from None
    cleaned=[normalize_home_trade(row) for row in rows]
    return {
        "paper_only":True,"real_trading_enabled":False,
        "wallet_funds_used":False,"scope":"PRIVATE_ACCOUNT",
        "sort":"NEWEST_FIRST","assets":list(ALL_ASSETS),
        "selected_asset":asset,
        "trades":[item for item in cleaned if item is not None and
                  (asset=="ALL" or item["asset"]==asset)],
    }
