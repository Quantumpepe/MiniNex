"""Per-Privy-user, per-asset durable simulation sessions (NO real orders).

Public market data is shared, but positions, balances and fills are scoped to
(privy_did, asset) in PostgreSQL. Explicit paper ticks are serialized with a
row lock to prevent duplicate fills under concurrent Render workers.
No wallet balances, signatures, or deposits are read or authorized.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
import logging
import math
from typing import Any

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .auto_engine import PaperAutoEngine, PaperPortfolio
from .storage import _db_url, _ensure_schema, _prepare_rows, _safe_did, ASSETS

LOG=logging.getLogger("mininex.user_paper")
PAPER_START_USD=Decimal("1000.00")
_STATE_FIELDS=(
    "stable_cash","coin_quantity","cost_basis","initial_equity","entry_price",
    "peak_price","last_exit_price","post_exit_low","last_trade_at","last_evaluated_at","last_decision",
    "last_reason","last_signal_summary","last_sold_quantity","realized_profit","accumulated_buy_costs",
    "accumulated_sell_costs","trade_count",
)


def _asset(asset: str) -> str:
    if asset not in ASSETS:
        raise HTTPException(404,"Only BTC and ETH are supported")
    return asset


def _state_of(p: PaperPortfolio) -> dict:
    return {field:getattr(p,field) for field in _STATE_FIELDS}


def _restore(asset: str, settlement: str, state: dict, initial: Decimal) -> PaperPortfolio:
    if not isinstance(state,dict):
        raise ValueError("Invalid paper state")
    # Do not accept arbitrary/unknown keys from a database blob as object attrs.
    values={name:state[name] for name in _STATE_FIELDS if name in state}
    for key,value in values.items():
        if key in ("last_decision","last_reason","last_signal_summary"):
            if type(value) is not str or len(value)>160:
                raise ValueError("Invalid stored decision")
        elif key=="trade_count":
            if type(value) is not int or value<0 or value>10**12:
                raise ValueError("Invalid stored trade count")
        elif value is not None and (
            type(value) not in (int,float) or not math.isfinite(value)
        ):
            raise ValueError("Invalid stored balance")
    p=PaperPortfolio(asset=asset,settlement=settlement,
                     stable_cash=float(initial),initial_equity=float(initial))
    for name,value in values.items():
        setattr(p,name,value)
    if p.stable_cash<0 or p.coin_quantity<0 or p.cost_basis<0:
        raise ValueError("Invalid negative paper state")
    return p


def _session_data(row: dict | None, trades: list[dict] | None = None) -> dict:
    if row is None:
        return {"status":"NOT_STARTED","paper_only":True,"real_trading_enabled":False,
                "wallet_funds_used":False,"trades":[]}
    state=row["state_json"] or {}
    return {
        "asset":row["asset"],"status":row["status"],
        "settlement":row["settlement"],"strategy_mode":row["strategy_mode"],
        "horizon":row["horizon"],"budget_percent":row["budget_percent"],
        "demo_start_cash":str(row["initial_demo_cash"]),
        "revision":row["revision"],
        "position":{
            "stable_cash":str(state.get("stable_cash",0)),
            "coin_quantity":str(state.get("coin_quantity",0)),
            "cost_basis":str(state.get("cost_basis",0)),
            "peak_price":state.get("peak_price"),
            "last_exit_price":state.get("last_exit_price"),
            "post_exit_low":state.get("post_exit_low"),
            "realized_net_profit":str(state.get("realized_profit",0)),
            "last_decision":state.get("last_decision","NOT_STARTED"),
            "last_reason":state.get("last_reason","NOT_STARTED"),
            "last_signal_summary":state.get("last_signal_summary",""),
            "last_evaluated_at":(datetime.fromtimestamp(state["last_evaluated_at"],tz=timezone.utc).isoformat()
                                 if isinstance(state.get("last_evaluated_at"),(int,float))
                                 and state["last_evaluated_at"]>0 else None),
            "trade_count":state.get("trade_count",0),
        },
        "trades":trades or [],
        "paper_only":True,"real_trading_enabled":False,"wallet_funds_used":False,
        "execution_cadence":"SERVER_AUTOMATIC_15_SECONDS",
        "warning":"Simulation only; saved settings do not authorize live trading. "
                  "Only simulation. Signals use actual loaded historical market data.",
    }


def _session_row(cur,did: str,asset: str,lock: bool=False):
    cur.execute(
        """SELECT * FROM mininex_user_paper_sessions
           WHERE privy_did=%s AND asset=%s """+("FOR UPDATE" if lock else ""),
        (did,asset),
    )
    return cur.fetchone()


def _trades(cur,did: str,asset: str,limit: int=30) -> list[dict]:
    cur.execute(
        """SELECT event_json FROM mininex_user_paper_trades
           WHERE privy_did=%s AND asset=%s
           ORDER BY event_id DESC LIMIT %s""",(did,asset,limit)
    )
    return [r["event_json"] for r in reversed(cur.fetchall())]


def read_user_paper(did: str,asset: str) -> dict:
    did=_safe_did(did)
    asset=_asset(asset)
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur,did)
                row=_session_row(cur,did,asset)
                trades=_trades(cur,did,asset) if row else []
                return _session_data(row,trades)
    except psycopg.Error:
        LOG.warning("MiniNex personal paper history unavailable")
        raise HTTPException(503,"Personal simulation unavailable") from None


def market_feed_for_paper_start(asset: str, market: Any | None = None) -> dict:
    """Use a verified reference price solely to seed a VIRTUAL native holding."""
    if market is None:
        raise HTTPException(503,"Verified demo price unavailable")
    feed=market.snapshot()["markets"][asset]
    price=feed.get("price_usdt")
    if not feed.get("market_data_valid") or type(price) not in (int,float) or not math.isfinite(price) or price<=0:
        raise HTTPException(409,"Wait for verified market price before spot-downtrend simulation")
    return feed


def start_user_paper(did: str,asset: str,market: Any | None = None) -> dict:
    """Opt-in to 1,000 *virtual* USD baseline, scaled by user's saved percent."""
    did=_safe_did(did)
    asset=_asset(asset)
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur,did)
                # Lock preference row to snapshot configuration atomically.
                cur.execute("""SELECT b.percent,t.mode,t.stable,t.horizon
                    FROM mininex_budget_preferences b JOIN mininex_trade_preferences t
                      ON b.privy_did=t.privy_did AND b.asset=t.asset
                    WHERE b.privy_did=%s AND b.asset=%s FOR UPDATE OF b,t""",(did,asset))
                settings=cur.fetchone()
                if not settings or settings["percent"]<=0:
                    raise HTTPException(409,"Save a nonzero demo budget first")
                existing=_session_row(cur,did,asset,lock=True)
                if existing and existing["status"]!="NOT_STARTED":
                    raise HTTPException(409,"Personal simulation already started")
                simulated=PAPER_START_USD*Decimal(settings["percent"])/Decimal(100)
                p=PaperPortfolio(asset=asset,settlement=settings["stable"],
                                 strategy_mode=settings["mode"],
                                 stable_cash=float(simulated),initial_equity=float(simulated))
                if settings["mode"]=="SPOT_SHORT":
                    # Virtual native-coin start, never a real wallet balance.
                    feed=market_feed_for_paper_start(asset,market)
                    price=feed["price_usdt"]
                    p.coin_quantity=float(simulated)/price
                    p.cost_basis=float(simulated)
                    p.stable_cash=0.0
                    p.entry_price=price
                    p.peak_price=price
                cur.execute("""INSERT INTO mininex_user_paper_sessions
                    (privy_did,asset,status,settlement,strategy_mode,horizon,
                     budget_percent,initial_demo_cash,state_json)
                    VALUES (%s,%s,'ACTIVE',%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (privy_did,asset) DO NOTHING""",
                    (did,asset,settings["stable"],settings["mode"],settings["horizon"],
                     settings["percent"],simulated,Jsonb(_state_of(p))))
                if cur.rowcount!=1:
                    raise HTTPException(409,"Personal simulation already exists")
                row=_session_row(cur,did,asset)
                return _session_data(row)
    except psycopg.Error:
        LOG.warning("MiniNex personal paper start unavailable")
        raise HTTPException(503,"Personal simulation unavailable") from None


def set_user_paper_status(did: str,asset: str,action: str) -> dict:
    did=_safe_did(did)
    asset=_asset(asset)
    if action not in ("pause","resume"):
        raise HTTPException(404,"Unknown simulation action")
    before,after=("ACTIVE","PAUSED") if action=="pause" else ("PAUSED","ACTIVE")
    try:
        url=_db_url();_ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur,did)
                cur.execute("""UPDATE mininex_user_paper_sessions
                    SET status=%s,revision=revision+1,updated_at=now()
                    WHERE privy_did=%s AND asset=%s AND status=%s
                    RETURNING *""",(after,did,asset,before))
                row=cur.fetchone()
                if row is None:
                    raise HTTPException(409,"Session not in required state")
                return _session_data(row,_trades(cur,did,asset))
    except psycopg.Error:
        LOG.warning("MiniNex personal paper status unavailable")
        raise HTTPException(503,"Personal simulation unavailable") from None


def step_user_paper(did: str,asset: str,market: Any,now: float) -> dict:
    """Exactly one manual paper strategy evaluation, serialized per DID/coin."""
    did=_safe_did(did);asset=_asset(asset)
    if not math.isfinite(now) or now<=0:
        raise HTTPException(422,"Invalid evaluation time")
    try:
        url=_db_url();_ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur,did)
                row=_session_row(cur,did,asset,lock=True)
                if not row or row["status"]!="ACTIVE":
                    raise HTTPException(409,"Start or resume the personal simulation first")
                # Shared market snapshot, individual portfolio. We do not connect
                # to Privy signer or transfer any assets here.
                engine=PaperAutoEngine(market)
                p=_restore(asset,row["settlement"],row["state_json"],row["initial_demo_cash"])
                p.strategy_mode=row["strategy_mode"]
                if p.last_evaluated_at is not None and now-p.last_evaluated_at<12:
                    return _session_data(row,_trades(cur,did,asset))
                engine.portfolios={asset:p}
                feed=market.snapshot(now=now)["markets"][asset]
                from .auto_engine import PaperRules
                # The selected period is a lookback, never a mandatory wait.
                # Real closed exchange candles are loaded asynchronously on
                # startup. No simulated history and no artificial buy trigger.
                windows={"5m":300.0,"15m":900.0,"1h":3600.0,
                         "24h":86400.0}
                engine.rules=PaperRules(
                    warmup_seconds=0.0,
                    min_history_samples=4,
                    price_window_seconds=windows[row["horizon"]],
                )
                engine.process_asset(asset,feed,now)
                p.last_evaluated_at=now
                history=list(p.trade_history)
                for event in history:
                    event_id=p.trade_count
                    timestamp=datetime.fromisoformat(event["at"])
                    cur.execute("""INSERT INTO mininex_user_paper_trades
                        (privy_did,asset,event_id,occurred_at,event_json)
                        VALUES (%s,%s,%s,%s,%s)
                        ON CONFLICT (privy_did,asset,event_id) DO NOTHING""",
                        (did,asset,event_id,timestamp,Jsonb(event)))
                cur.execute("""UPDATE mininex_user_paper_sessions
                    SET state_json=%s,revision=revision+1,updated_at=now()
                    WHERE privy_did=%s AND asset=%s RETURNING *""",
                    (Jsonb(_state_of(p)),did,asset))
                updated=cur.fetchone()
                return _session_data(updated,_trades(cur,did,asset))
    except psycopg.Error:
        LOG.warning("MiniNex personal paper tick unavailable")
        raise HTTPException(503,"Personal simulation unavailable") from None
