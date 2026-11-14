"""Durable, opt-in, nine-coin PAPER trading accounts for MiniNex.

No wallet access, signatures, real deposits, real swaps, live order routes or
automatic user enrollment. Every account/coin has its own PostgreSQL row,
simulation capital, strategy state and trade history. All modifications hold a
row lock. The existing BTC/ETH paper and live ledgers are untouched.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import logging
import math
import time
from typing import Any

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .auto_engine import PaperAutoEngine, PaperPortfolio, PaperRules
from .shared_coin_strategy import evaluate_coin_strategy, exit_coin_to_stable
from .storage import _db_url, _ensure_schema, _prepare_rows, _safe_did
from .strategy_replay import EXTRA_REPLAY_ASSETS, WINDOWS, MODES
from .user_paper import (
    _state_of, _restore, _paper_state_with_runtime,
    _session_data,
)

LOG=logging.getLogger("mininex.extra_paper")
STABLES=("USDC","USDT")
MIN_BUDGET=100
MAX_BUDGET=5000
TICK_SECONDS=12


def valid_asset(asset: str) -> str:
    if asset not in EXTRA_REPLAY_ASSETS:
        raise HTTPException(404,"This coin has no persistent paper engine")
    return asset


def validate_start(budget: Any, mode: str, stable: str, horizon: str) -> Decimal:
    # Typed integers only: booleans, currency strings and fractions are not
    # permission to fund anything. This is virtual capital, not a wallet grant.
    if type(budget) is not int or not MIN_BUDGET <= budget <= MAX_BUDGET:
        raise HTTPException(422,"Virtual budget must be 100–5000 USD")
    if mode not in MODES or stable not in STABLES or horizon not in WINDOWS:
        raise HTTPException(422,"Invalid paper-only strategy settings")
    return Decimal(budget)


class ExtraPaperMarket:
    """Adapter around EXISTING verified extra-coin market prices/history."""
    def __init__(self, source: Any):
        self.source=source

    def snapshot(self, *, now: float | None = None) -> dict:
        return {"markets":self.source.snapshot(now=now)}

    def history(self, asset: str, *, limit: int = 3600) -> list[dict]:
        return self.source.history(asset,limit=limit)


def _row(cur: Any,did: str,asset: str,lock: bool=False):
    cur.execute("""SELECT * FROM mininex_extra_paper_sessions
        WHERE privy_did=%s AND asset=%s """+("FOR UPDATE" if lock else ""),
        (did,asset))
    return cur.fetchone()


def _trades(cur: Any,did: str,asset: str,limit: int=30) -> list[dict]:
    cur.execute("""SELECT event_json FROM mininex_extra_paper_trades
        WHERE privy_did=%s AND asset=%s
        ORDER BY event_id DESC LIMIT %s""",(did,asset,min(max(1,limit),30)))
    return [r["event_json"] for r in reversed(cur.fetchall())]


def _present(asset: str,row: dict | None,trades: list[dict] | None = None) -> dict:
    if row is None:
        return {"asset":asset,"status":"NOT_STARTED",
                "paper_only":True,"simulation_only":True,
                "real_trading_enabled":False,"wallet_funds_used":False,
                "budget_isolated":True,"trades":[],
                "runtime_active_seconds":None,"runtime_tracked":False,
                "pause_exit_pending":False,"pause_exit_completed":False,
                "cost_model":"GENERIC_HYPOTHETICAL_PAPER_COSTS_NOT_CHAIN_QUOTES"}
    # Existing BTC/ETH presentation logic, WITHOUT editing those user accounts.
    view=_session_data({**row,"budget_percent":None},trades)
    view.pop("budget_percent",None)
    view.update({"asset":asset,"budget_usd":float(row["initial_demo_cash"]),
                 "simulation_only":True,"budget_isolated":True,
                 "cost_model":"GENERIC_HYPOTHETICAL_PAPER_COSTS_NOT_CHAIN_QUOTES",
                 "warning":"Simulated spot fills and hypothetical costs only. "
                           "No wallet funds or real orders are used."})
    return view


def mark_to_market(result: dict,source: Any) -> dict:
    """Show virtual equity only when the price is verified, never a live quote."""
    asset=valid_asset(result.get("asset"))
    if result.get("paper_only") is not True:
        raise ValueError("Not a paper account")
    position=result.get("position")
    if not isinstance(position,dict):
        return result
    result={**result,"position":dict(position)}
    mark={"quote_valid":False,"price_usdt":None,
          "estimated_exit_net_usd":None,"estimated_total_equity_usd":None,
          "unrealized_net_pnl_usd":None,"unrealized_net_pct":None,
          "pricing_basis":"GENERIC_PAPER_ESTIMATE_ONLY"}
    result["position"]["valuation"]=mark
    try:
        qty=float(position.get("coin_quantity",0))
        cash=float(position.get("stable_cash",0))
        basis=float(position.get("cost_basis",0))
        if any(not math.isfinite(v) or v<0 for v in (qty,cash,basis)):
            return result
        if qty==0:
            if basis==0:
                mark.update({"quote_valid":True,"estimated_exit_net_usd":0.0,
                             "estimated_total_equity_usd":round(cash,6),
                             "unrealized_net_pnl_usd":0.0,"unrealized_net_pct":0.0,
                             "pricing_basis":"VIRTUAL_CASH_ONLY"})
            return result
        feed=source.snapshot().get(asset,{})
        raw=feed.get("price_usdt")
        if feed.get("market_data_valid") is not True or type(raw) not in (int,float):
            return result
        price=float(raw)
        if not math.isfinite(price) or price<=0:
            return result
        estimated=PaperAutoEngine(ExtraPaperMarket(source))._sell_quote(asset,qty,price)
        if estimated is None:
            return result
        net=estimated["net_proceeds"]
        mark.update({"quote_valid":True,"price_usdt":round(price,8),
                     "estimated_exit_net_usd":round(net,6),
                     "estimated_total_equity_usd":round(cash+net,6),
                     "unrealized_net_pnl_usd":round(net-basis,6),
                     "unrealized_net_pct":round((net/basis-1)*100,4) if basis>0 else None})
    except (ValueError,TypeError,KeyError,OverflowError,AttributeError):
        return result
    return result


def _history_coverage(asset: str,horizon: str,source: Any) -> dict:
    """Display-only actual observation span, not permission to trade.

    24h uses the new public 15-minute history alongside existing recent
    minute closes and ticks. Missing history is explained, not hidden behind
    a new manual diagnostic button or blocking an active paper session.
    """
    required=WINDOWS[horizon]
    fallback={"status":"UNAVAILABLE","available_minutes":None,
              "required_minutes":int(required/60),"recent_age_seconds":None,
              "complete":False}
    try:
        clock=getattr(source,"clock",time.time)
        now=float(clock()) if callable(clock) else time.time()
        samples=source.history(asset,limit=3600)
        if not math.isfinite(now) or not isinstance(samples,list):
            return fallback
        stamps=[]
        for sample in samples:
            if not isinstance(sample,dict):
                continue
            stamp=sample.get("timestamp")
            price=sample.get("price_usdt")
            if (type(stamp) not in (int,float) or
                type(price) not in (int,float) or
                not math.isfinite(stamp) or not math.isfinite(price) or
                price<=0 or not 0<=now-stamp<=required):
                continue
            stamps.append(float(stamp))
        if not stamps:
            return {"status":"INSUFFICIENT","available_minutes":0,
                    "required_minutes":int(required/60),
                    "recent_age_seconds":None,"complete":False}
        span=max(stamps)-min(stamps)
        age=max(0.0,now-max(stamps))
        complete=span >= required*0.95 and age<=15
        return {"status":"READY" if complete else "PARTIAL",
                "available_minutes":round(span/60,1),
                "required_minutes":int(required/60),
                "recent_age_seconds":round(age,1),
                "complete":complete}
    except (TypeError,ValueError,AttributeError,OverflowError):
        return fallback
    except Exception:
        LOG.warning("Extra-coin history coverage unavailable for %s",asset)
        return fallback


def read(did: str,asset: str,source: Any | None = None) -> dict:
    did=_safe_did(did);asset=valid_asset(asset)
    try:
        url=_db_url();_ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur,did)
                row=_row(cur,did,asset)
                view=_present(asset,row,_trades(cur,did,asset) if row else None)
                if source is not None:
                    view=mark_to_market(view,source)
                    if row:
                        view["history_coverage"]=_history_coverage(
                            asset,row["horizon"],source)
                return view
    except psycopg.Error:
        LOG.warning("Extra-coin PAPER read unavailable")
        raise HTTPException(503,"Personal extra-coin simulation unavailable") from None


def start(did: str,asset: str,budget_usd: int,mode: str,stable: str,
          horizon: str,source: Any,now: float | None = None) -> dict:
    did=_safe_did(did);asset=valid_asset(asset)
    budget=validate_start(budget_usd,mode,stable,horizon)
    at=time.time() if now is None else now
    if not math.isfinite(at) or at<=0:
        raise HTTPException(422,"Invalid clock")
    p=PaperPortfolio(asset=asset,settlement=stable,strategy_mode=mode,
                     stable_cash=float(budget),initial_equity=float(budget))
    if mode=="SPOT_SHORT":
        feed=source.snapshot(now=at).get(asset,{})
        price=feed.get("price_usdt")
        if feed.get("market_data_valid") is not True or type(price) not in (int,float) or not math.isfinite(price) or price<=0:
            raise HTTPException(409,"Verified price required to start virtual coin holdings")
        p.coin_quantity=float(budget)/price
        p.cost_basis=float(budget)
        p.stable_cash=0.0
        p.entry_price=float(price)
        p.peak_price=float(price)
    state={**_state_of(p),"runtime_started_at":at,
           "runtime_active_since":at,"runtime_elapsed_seconds":0.0}
    try:
        url=_db_url();_ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur,did)
                cur.execute("""INSERT INTO mininex_extra_paper_sessions
                    (privy_did,asset,status,settlement,strategy_mode,horizon,
                     initial_demo_cash,state_json)
                    VALUES (%s,%s,'ACTIVE',%s,%s,%s,%s,%s)
                    ON CONFLICT (privy_did,asset) DO NOTHING
                    RETURNING *""",(did,asset,stable,mode,horizon,budget,Jsonb(state)))
                row=cur.fetchone()
                if not row:
                    raise HTTPException(409,"Coin simulation already exists; pause/resume it")
                return _present(asset,row,[])
    except psycopg.Error:
        LOG.warning("Extra-coin PAPER start unavailable")
        raise HTTPException(503,"Personal extra-coin simulation unavailable") from None


def _record_event(cur: Any,did: str,asset: str,p: PaperPortfolio) -> None:
    if not p.trade_history:
        return
    if len(p.trade_history)!=1:
        raise ValueError("Unexpected number of paper fills per evaluation")
    event=p.trade_history[0]
    if event.get("asset")!=asset or event.get("real_trade") is not False:
        raise ValueError("Unexpected non-paper fill")
    cur.execute("""INSERT INTO mininex_extra_paper_trades
        (privy_did,asset,event_id,occurred_at,event_json)
        VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT (privy_did,asset,event_id) DO NOTHING""",
        (did,asset,p.trade_count,datetime.fromisoformat(event["at"]),Jsonb(event)))


def _settle_pause(cur: Any,row: dict,did: str,asset: str,
                  source: Any,now: float) -> dict:
    stored=row["state_json"] or {}
    if row["status"]!="PAUSED" or stored.get("pause_exit_pending") is not True:
        return row
    p=_restore(asset,row["settlement"],stored,row["initial_demo_cash"])
    if p.coin_quantity>0:
        try:
            if not exit_coin_to_stable(asset,p,ExtraPaperMarket(source),now):
                return row
        except Exception:
            # A temporary bad market API must not undo PAUSED status.
            return row
        _record_event(cur,did,asset,p)
    state=_paper_state_with_runtime(p,stored,now,active=False)
    state.pop("pause_exit_pending",None)
    cur.execute("""UPDATE mininex_extra_paper_sessions
        SET state_json=%s,revision=revision+1,updated_at=now()
        WHERE privy_did=%s AND asset=%s AND status='PAUSED'
        RETURNING *""",(Jsonb(state),did,asset))
    return cur.fetchone()


def set_status(did: str,asset: str,action: str,source: Any,
               now: float | None = None) -> dict:
    did=_safe_did(did);asset=valid_asset(asset)
    if action not in ("pause","resume"):
        raise HTTPException(404,"Unknown coin simulation action")
    at=time.time() if now is None else now
    if not math.isfinite(at) or at<=0:
        raise HTTPException(422,"Invalid session clock")
    before,after=("ACTIVE","PAUSED") if action=="pause" else ("PAUSED","ACTIVE")
    try:
        url=_db_url();_ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur,did)
                row=_row(cur,did,asset,lock=True)
                if not row or row["status"]!=before:
                    raise HTTPException(409,"Coin simulation not in required state")
                stored=row["state_json"] or {}
                if action=="resume" and stored.get("pause_exit_pending") is True:
                    raise HTTPException(409,"Pending virtual conversion to stablecoin")
                p=_restore(asset,row["settlement"],stored,row["initial_demo_cash"])
                state=_paper_state_with_runtime(p,stored,at,active=action=="resume")
                if action=="pause":
                    started=state.pop("runtime_active_since",None)
                    if started is not None:
                        state["runtime_elapsed_seconds"]+=max(0.0,at-started)
                    if p.coin_quantity>0:
                        state["pause_exit_pending"]=True
                    else:
                        state.pop("pause_exit_pending",None)
                else:
                    state["runtime_active_since"]=at
                    state.pop("pause_exit_pending",None)
                cur.execute("""UPDATE mininex_extra_paper_sessions
                    SET status=%s,state_json=%s,revision=revision+1,updated_at=now()
                    WHERE privy_did=%s AND asset=%s AND status=%s RETURNING *""",
                    (after,Jsonb(state),did,asset,before))
                updated=cur.fetchone()
                if action=="pause" and state.get("pause_exit_pending") is True:
                    updated=_settle_pause(cur,updated,did,asset,source,at)
                return _present(asset,updated,_trades(cur,did,asset))
    except psycopg.Error:
        LOG.warning("Extra-coin PAPER status unavailable")
        raise HTTPException(503,"Personal extra-coin simulation unavailable") from None


def tick(did: str,asset: str,source: Any,now: float) -> dict:
    did=_safe_did(did);asset=valid_asset(asset)
    if not math.isfinite(now) or now<=0:
        raise HTTPException(422,"Invalid evaluation time")
    try:
        url=_db_url();_ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur,did)
                row=_row(cur,did,asset,lock=True)
                if row and row["status"]=="PAUSED" and (row["state_json"] or {}).get("pause_exit_pending") is True:
                    done=_settle_pause(cur,row,did,asset,source,now)
                    return _present(asset,done,_trades(cur,did,asset))
                if not row or row["status"]!="ACTIVE":
                    raise HTTPException(409,"Start or resume this coin's simulation first")
                p=_restore(asset,row["settlement"],row["state_json"],row["initial_demo_cash"])
                p.strategy_mode=row["strategy_mode"]
                if p.last_evaluated_at is not None and now-p.last_evaluated_at<TICK_SECONDS:
                    return _present(asset,row,_trades(cur,did,asset))
                # The SAME decision path as BTC/ETH: no per-coin strategy fork.
                evaluate_coin_strategy(asset,p,ExtraPaperMarket(source),now,row["horizon"])
                _record_event(cur,did,asset,p)
                state=_paper_state_with_runtime(p,row["state_json"],now,active=True)
                cur.execute("""UPDATE mininex_extra_paper_sessions
                    SET state_json=%s,revision=revision+1,updated_at=now()
                    WHERE privy_did=%s AND asset=%s AND status='ACTIVE'
                    RETURNING *""",(Jsonb(state),did,asset))
                updated=cur.fetchone()
                return _present(asset,updated,_trades(cur,did,asset))
    except psycopg.Error:
        LOG.warning("Extra-coin PAPER tick unavailable")
        raise HTTPException(503,"Personal extra-coin simulation unavailable") from None
