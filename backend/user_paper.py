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
import time
from typing import Any

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .auto_engine import PaperAutoEngine, PaperPortfolio
from .shared_coin_strategy import evaluate_coin_strategy, exit_coin_to_stable
from .storage import _db_url, _ensure_schema, _prepare_rows, _safe_did, ASSETS

LOG=logging.getLogger("mininex.user_paper")
PAPER_START_USD=Decimal("5000.00")
_STATE_FIELDS=(
    "stable_cash","coin_quantity","cost_basis","initial_equity","entry_price",
    "peak_price","last_exit_price","post_exit_low","last_trade_at","last_evaluated_at","last_decision",
    "last_reason","last_signal_summary","last_sold_quantity","last_buy_price","add_count",
    "realized_profit","accumulated_buy_costs",
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
        elif key in ("trade_count","add_count"):
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


def _reconcile_virtual_budget(p: PaperPortfolio, current: Decimal, requested: Decimal) -> bool:
    """Rebase PAPER cash without rewriting fills, positions or realized PnL.

    Increases credit virtual stable cash. Decreases are deferred if capital is
    locked in a position, rather than forcing a sale or negative cash balance.
    This never accesses a real wallet or grants trading permission.
    """
    if requested < 0:
        raise ValueError("Negative virtual budget")
    delta = float(requested - current)
    if not math.isfinite(delta):
        raise ValueError("Invalid virtual budget")
    if delta < 0 and p.stable_cash + delta < -1e-7:
        return False
    p.stable_cash = max(0.0, p.stable_cash + delta)
    p.initial_equity = float(requested)
    return True


# Server-owned runtime/PAUSED-exit metadata persists through normal ticks.
_RUNTIME_FIELDS=("runtime_started_at","runtime_active_since","runtime_elapsed_seconds")

def _paper_state_with_runtime(p: PaperPortfolio, stored: dict | None,
                              now: float, *, active: bool=False) -> dict:
    data=_state_of(p)
    original=stored if isinstance(stored,dict) else {}
    for field in _RUNTIME_FIELDS:
        value=original.get(field)
        if type(value) in (int,float) and math.isfinite(value) and value>=0:
            data[field]=float(value)
    if "runtime_started_at" not in data:
        data["runtime_started_at"]=now
    if "runtime_elapsed_seconds" not in data:
        data["runtime_elapsed_seconds"]=0.0
    if active and "runtime_active_since" not in data:
        data["runtime_active_since"]=now
    if original.get("pause_exit_pending") is True:
        data["pause_exit_pending"]=True
    return data


def _active_runtime_seconds(row: dict, state: dict) -> tuple[int,bool]:
    elapsed=state.get("runtime_elapsed_seconds")
    active_since=state.get("runtime_active_since")
    if type(elapsed) not in (float,int) or not math.isfinite(elapsed) or elapsed<0:
        return 0,False
    if row["status"]=="ACTIVE" and type(active_since) in (float,int) and math.isfinite(active_since):
        elapsed+=max(0.0,time.time()-active_since)
    return max(0,int(elapsed)),True


def _session_data(row: dict | None, trades: list[dict] | None = None) -> dict:
    if row is None:
        return {"status":"NOT_STARTED","paper_only":True,"real_trading_enabled":False,
                "wallet_funds_used":False,"trades":[],
                "runtime_active_seconds":None,"runtime_tracked":False,
                "pause_exit_pending":False,"pause_exit_completed":False}
    state=row["state_json"] or {}
    runtime_seconds,runtime_tracked=_active_runtime_seconds(row,state)
    return {
        "asset":row["asset"],"status":row["status"],
        "runtime_active_seconds":runtime_seconds if runtime_tracked else None,
        "runtime_tracked":runtime_tracked,
        "pause_exit_pending":state.get("pause_exit_pending") is True,
        "pause_exit_completed":row["status"]=="PAUSED" and state.get("pause_exit_pending") is not True
                               and float(state.get("coin_quantity",0))==0,
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
            "add_count":state.get("add_count",0),
            "last_buy_price":state.get("last_buy_price"),
        },
        "trades":trades or [],
        "paper_only":True,"real_trading_enabled":False,"wallet_funds_used":False,
        "execution_cadence":"SERVER_AUTOMATIC_15_SECONDS",
        "warning":"Simulation only; saved settings do not authorize live trading. "
                  "Only simulation. Signals use actual loaded historical market data.",
    }



def add_paper_mark_to_market(result: dict, market: Any) -> dict:
    """Read-only estimated liquidation value, never a live executable quote.

    Keep all user positions private. A missing or unverified current price
    produces no valuation rather than a misleading PnL or phantom balance.
    """
    if result.get("paper_only") is not True:
        raise ValueError("Not a simulation")
    position=result.get("position")
    if not isinstance(position,dict) or result.get("asset") not in ASSETS:
        return result
    result=dict(result)
    result["position"]=dict(position)
    asset=result["asset"]
    empty={"quote_valid":False,"price_usdt":None,"estimated_exit_net_usd":None,
           "unrealized_net_pnl_usd":None,"unrealized_net_pct":None,
           "estimated_total_equity_usd":None,
           "estimated_sale_gross_usd":None,"estimated_sale_costs_usd":None,
           "estimated_sale_fee_usd":None,"estimated_sale_spread_slippage_usd":None,
           "estimated_sale_network_fee_usd":None,
           "last_buy_costs_usd":None,
           "pricing_basis":"PAPER_ESTIMATE_ONLY"}
    result["position"]["valuation"]=empty
    try:
        coin=float(position.get("coin_quantity") or 0)
        cash=float(position.get("stable_cash") or 0)
        basis=float(position.get("cost_basis") or 0)
        if not all(math.isfinite(x) for x in (coin,cash,basis)):
            return result
        if coin<0 or cash<0 or basis<0:
            return result
        # Pure stablecoin PAPER balances have a verifiable cash value even if
        # BTC/ETH data is offline. Do not fabricate a coin quote, or suppress
        # legitimate user cash because the public market feed is stale.
        if coin == 0:
            if basis != 0:
                return result  # inconsistent ledger: never invent equity
            empty.update({
                "quote_valid":True,
                "price_usdt":None,
                "estimated_sale_gross_usd":0.0,
                "estimated_sale_costs_usd":0.0,
                "estimated_sale_fee_usd":0.0,
                "estimated_sale_spread_slippage_usd":0.0,
                "estimated_sale_network_fee_usd":0.0,
                "estimated_exit_net_usd":0.0,
                "unrealized_net_pnl_usd":0.0,
                "unrealized_net_pct":0.0,
                "estimated_total_equity_usd":round(cash,6),
                "pricing_basis":"PAPER_CASH_ONLY_NO_MARKET_QUOTE",
            })
            return result
        # An open BTC/ETH position must still have a fresh cross-checked
        # executable-looking reference before estimated liquidation PnL.
        feed=market.snapshot()["markets"][asset]
        if not feed.get("market_data_valid"):
            return result
        price=float(feed["price_usdt"])
        if not math.isfinite(price) or price<=0:
            return result
        engine=PaperAutoEngine(market)
        sale=engine._sell_quote(asset,coin,price) if coin>engine.rules.min_coin_units else None
        if coin>engine.rules.min_coin_units and not sale:
            return result
        net=sale["net_proceeds"] if sale else 0.0
        unrealized=net-basis if sale else 0.0
        # The buy fill was already included in cost_basis; never deduct
        # the displayed entry costs again from unrealized PnL.
        last_buy_costs=None
        if sale:
            for trade in reversed(result.get("trades") or []):
                if isinstance(trade,dict) and trade.get("kind")=="PAPER_BUY":
                    costs=trade.get("estimated_costs")
                    if isinstance(costs,dict):
                        try:
                            amounts=[float(costs[key]) for key in (
                                "venue_fee","network_fee","spread_and_slippage_cost")]
                            if all(math.isfinite(v) and v>=0 for v in amounts):
                                last_buy_costs=round(sum(amounts),6)
                        except (KeyError,ValueError,TypeError,OverflowError):
                            pass
                    break
        sale_costs=(sale["venue_fee"]+sale["network_fee"]
                    +sale["spread_and_slippage_cost"]) if sale else 0.0
        empty.update({
            "quote_valid":True,
            "price_usdt":round(price,8),
            "estimated_sale_gross_usd":round(sale["market_value"],6) if sale else 0.0,
            "estimated_sale_costs_usd":round(sale_costs,6),
            "estimated_sale_fee_usd":round(sale["venue_fee"],6) if sale else 0.0,
            "estimated_sale_spread_slippage_usd":round(sale["spread_and_slippage_cost"],6) if sale else 0.0,
            "estimated_sale_network_fee_usd":round(sale["network_fee"],6) if sale else 0.0,
            "last_buy_costs_usd":last_buy_costs,
            "estimated_exit_net_usd":round(net,6),
            "unrealized_net_pnl_usd":round(unrealized,6),
            "unrealized_net_pct":round(unrealized/basis*100,4) if basis>0 else 0.0,
            "estimated_total_equity_usd":round(cash+net,6),
        })
    except (TypeError,ValueError,KeyError,OverflowError,AttributeError):
        pass
    return result


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
    """Opt-in to 5,000 *virtual* USD baseline, scaled by user's saved percent."""
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
                started_at=time.time()
                state={**_state_of(p),"runtime_started_at":started_at,
                       "runtime_active_since":started_at,
                       "runtime_elapsed_seconds":0.0}
                cur.execute("""INSERT INTO mininex_user_paper_sessions
                    (privy_did,asset,status,settlement,strategy_mode,horizon,
                     budget_percent,initial_demo_cash,state_json)
                    VALUES (%s,%s,'ACTIVE',%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (privy_did,asset) DO NOTHING""",
                    (did,asset,settings["stable"],settings["mode"],settings["horizon"],
                     settings["percent"],simulated,Jsonb(state)))
                if cur.rowcount!=1:
                    raise HTTPException(409,"Personal simulation already exists")
                row=_session_row(cur,did,asset)
                return _session_data(row)
    except psycopg.Error:
        LOG.warning("MiniNex personal paper start unavailable")
        raise HTTPException(503,"Personal simulation unavailable") from None


def _settle_pending_pause(cur, row: dict, did: str, asset: str,
                          market: Any | None, now: float) -> dict:
    """Sell simulated native holdings with an independently verified quote.

    User is already PAUSED before attempting the sale. An invalid/offline
    market leaves the position untouched and pending for automatic retry.
    Same row-level lock as engine buys, so no duplicate or race with fills.
    """
    stored=row["state_json"] or {}
    if row["status"]!="PAUSED" or stored.get("pause_exit_pending") is not True:
        return row
    p=_restore(asset,row["settlement"],stored,row["initial_demo_cash"])
    updated=_paper_state_with_runtime(p,stored,now,active=False)
    if p.coin_quantity>0:
        if market is None:
            return row
        try:
            if not exit_coin_to_stable(asset,p,market,now):
                return row  # Keep the paused virtual position pending.
        except Exception:
            # A missing market cannot re-activate a paused session.
            return row
        event=p.trade_history[-1]
        cur.execute("""INSERT INTO mininex_user_paper_trades
            (privy_did,asset,event_id,occurred_at,event_json)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (privy_did,asset,event_id) DO NOTHING""",
            (did,asset,p.trade_count,datetime.fromisoformat(event["at"]),Jsonb(event)))
        updated=_paper_state_with_runtime(p,stored,now,active=False)
    updated.pop("pause_exit_pending",None)
    cur.execute("""UPDATE mininex_user_paper_sessions
        SET state_json=%s,revision=revision+1,updated_at=now()
        WHERE privy_did=%s AND asset=%s AND status='PAUSED'
        RETURNING *""",(Jsonb(updated),did,asset))
    return cur.fetchone()


def set_user_paper_status(did: str,asset: str,action: str,
                          market: Any | None = None,now: float | None = None) -> dict:
    did=_safe_did(did)
    asset=_asset(asset)
    if action not in ("pause","resume"):
        raise HTTPException(404,"Unknown simulation action")
    at=time.time() if now is None else now
    if type(at) not in (int,float) or not math.isfinite(at) or at<=0:
        raise HTTPException(422,"Invalid session clock")
    before,after=("ACTIVE","PAUSED") if action=="pause" else ("PAUSED","ACTIVE")
    try:
        url=_db_url();_ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur,did)
                row=_session_row(cur,did,asset,lock=True)
                if not row or row["status"]!=before:
                    raise HTTPException(409,"Session not in required state")
                stored=row["state_json"] or {}
                if action=="resume" and stored.get("pause_exit_pending") is True:
                    raise HTTPException(409,"Pause settlement must finish before resume")
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
                cur.execute("""UPDATE mininex_user_paper_sessions
                    SET status=%s,state_json=%s,revision=revision+1,updated_at=now()
                    WHERE privy_did=%s AND asset=%s AND status=%s
                    RETURNING *""",(after,Jsonb(state),did,asset,before))
                updated=cur.fetchone()
                if action=="pause" and state.get("pause_exit_pending") is True:
                    updated=_settle_pending_pause(cur,updated,did,asset,market,at)
                return _session_data(updated,_trades(cur,did,asset))
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
                if row and row["status"]=="PAUSED" and (row["state_json"] or {}).get("pause_exit_pending") is True:
                    completed=_settle_pending_pause(cur,row,did,asset,market,now)
                    return _session_data(completed,_trades(cur,did,asset))
                if not row or row["status"]!="ACTIVE":
                    raise HTTPException(409,"Start or resume the personal simulation first")
                # Shared market snapshot, individual portfolio. We do not connect
                # to Privy signer or transfer any assets here.
                p=_restore(asset,row["settlement"],row["state_json"],row["initial_demo_cash"])
                p.strategy_mode=row["strategy_mode"]
                # Apply saved percentage to this session, including on a new baseline.
                # The session row lock prevents duplicate credits across workers.
                cur.execute("""SELECT percent FROM mininex_budget_preferences
                    WHERE privy_did=%s AND asset=%s""",(did,asset))
                preference=cur.fetchone()
                if preference is None:
                    raise HTTPException(503,"Personal demo budget unavailable")
                previous=Decimal(row["initial_demo_cash"])
                requested=PAPER_START_USD*Decimal(preference["percent"])/Decimal(100)
                applied=_reconcile_virtual_budget(p,previous,requested)
                new_initial=requested if applied else previous
                new_percent=int(preference["percent"]) if applied else row["budget_percent"]
                if p.last_evaluated_at is not None and now-p.last_evaluated_at<12:
                    if new_initial != previous or new_percent != row["budget_percent"]:
                        cur.execute("""UPDATE mininex_user_paper_sessions
                            SET state_json=%s,initial_demo_cash=%s,budget_percent=%s,
                                revision=revision+1,updated_at=now()
                            WHERE privy_did=%s AND asset=%s RETURNING *""",
                            (Jsonb(_paper_state_with_runtime(p,row["state_json"],now,active=True)),new_initial,new_percent,did,asset))
                        row=cur.fetchone()
                    return _session_data(row,_trades(cur,did,asset))
                # Shared with BNB/SOL/XRP/TRX/POL/AVAX/LINK/DOGE/UNI.
                # One signal engine, one market lookback and identical
                # momentum/DCA/trailing decisions for every asset.
                evaluate_coin_strategy(asset,p,market,now,row["horizon"])
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
                    SET state_json=%s,initial_demo_cash=%s,budget_percent=%s,
                        revision=revision+1,updated_at=now()
                    WHERE privy_did=%s AND asset=%s RETURNING *""",
                    (Jsonb(_paper_state_with_runtime(p,row["state_json"],now,active=True)),new_initial,new_percent,did,asset))
                updated=cur.fetchone()
                return _session_data(updated,_trades(cur,did,asset))
    except psycopg.Error:
        LOG.warning("MiniNex personal paper tick unavailable")
        raise HTTPException(503,"Personal simulation unavailable") from None
