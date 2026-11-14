"""Stop must pause first, then atomically sell only simulated coins to USDC/USDT.

Never creates real-wallet orders, never fabricates proceeds on a stale quote.
Existing personalized balances and transactions remain durable and isolated.
"""
import inspect
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend import user_paper as paper
from backend import personal_paper_worker as worker
from backend.auto_engine import PaperPortfolio


NOW = 1800000000.0
DID = "did:privy:alice"


def row(asset="BTC",settlement="USDC",quantity=0.01,active_since=NOW-100):
    p=PaperPortfolio(asset=asset,settlement=settlement,
                     stable_cash=100.0,coin_quantity=quantity,
                     initial_equity=600.0,cost_basis=470.0,
                     trade_count=3,entry_price=47000.0)
    state={
        **paper._state_of(p),
        "runtime_started_at":NOW-300.0,
        "runtime_elapsed_seconds":42.0,
        "pause_exit_pending":True,
    }
    if active_since is not None:
        state["runtime_active_since"]=active_since
    return {"privy_did":DID,"asset":asset,"settlement":settlement,
            "status":"PAUSED","state_json":state,
            "initial_demo_cash":600.0,"revision":7,
            "strategy_mode":"LONG","horizon":"15m","budget_percent":100}


class DummyCursor:
    def __init__(self,record):
        self.row=deepcopy(record)
        self.events=[]
        self.writes=[]
    def execute(self,sql,args):
        self.writes.append(sql)
        if "INSERT INTO mininex_user_paper_trades" in sql:
            self.events.append((args[2],args[4].obj))
        elif "UPDATE mininex_user_paper_sessions" in sql:
            self.row["state_json"]=deepcopy(args[0].obj)
            self.row["revision"]+=1
    def fetchone(self):
        return deepcopy(self.row)


class Market:
    def __init__(self,valid=True,price=50000.0,explode=False):
        self.valid,self.price,self.explode=valid,price,explode
    def snapshot(self,now=None):
        if self.explode:
            raise RuntimeError("Market source offline")
        return {"markets":{
            "BTC":{"price_usdt":self.price,"market_data_valid":self.valid},
            "ETH":{"price_usdt":3000.0,"market_data_valid":self.valid},
        }}


@pytest.mark.parametrize("asset,stable,price",[
    ("BTC","USDC",50000.0),("ETH","USDT",3000.0),
])
def test_paused_paper_position_is_fully_sold_with_fee_aware_stable_settlement(asset,stable,price):
    original=row(asset=asset,settlement=stable,quantity=0.02 if asset=="BTC" else 1.0,
                 active_since=None)
    cur=DummyCursor(original)
    result=paper._settle_pending_pause(cur,original,DID,asset,Market(),NOW)
    state=result["state_json"]
    assert result["status"]=="PAUSED"
    assert state["coin_quantity"]==0.0
    assert state["stable_cash"]>100.0
    assert state["cost_basis"]==0.0
    assert state["trade_count"]==4
    assert state["last_reason"]=="USER_PAUSE_TO_STABLE"
    assert "pause_exit_pending" not in state
    assert len(cur.events)==1
    event=cur.events[0][1]
    assert event["asset"]==asset
    assert event["settlement"]==stable
    assert event["kind"]=="PAPER_SELL"
    assert event["real_trade"] is False
    assert event["quantity"]>0
    assert event["estimated_costs"]["venue_fee"]>0
    assert event["estimated_costs"]["spread_and_slippage_cost"]>0
    assert event["net_proceeds"]>0
    assert state["stable_cash"]==pytest.approx(100.0+event["net_proceeds"],abs=1e-5)
    assert paper._session_data(result)["pause_exit_completed"] is True

    # Repeated worker scans or requests cannot create duplicate fills.
    again=paper._settle_pending_pause(cur,result,DID,asset,Market(),NOW+30)
    assert again==result
    assert len(cur.events)==1


@pytest.mark.parametrize("market",[
    Market(valid=False),Market(price=float("nan")),
    Market(price=0),Market(price=True),Market(explode=True),
])
def test_missing_or_unverified_quote_keeps_paper_coin_and_exit_pending(market):
    original=row()
    cur=DummyCursor(original)
    result=paper._settle_pending_pause(cur,original,DID,"BTC",market,NOW)
    assert result==original
    assert cur.events==[]
    assert cur.writes==[]
    assert result["state_json"]["pause_exit_pending"] is True


def test_pending_paper_exit_automatically_retries_after_reference_recovers():
    original=row(active_since=None)
    cur=DummyCursor(original)
    assert paper._settle_pending_pause(cur,original,DID,"BTC",Market(False),NOW)==original
    recovered=paper._settle_pending_pause(cur,original,DID,"BTC",Market(True),NOW+15)
    assert recovered["state_json"]["coin_quantity"]==0.0
    assert recovered["state_json"].get("pause_exit_pending") is None
    assert len(cur.events)==1


def test_runtime_is_accumulated_across_worker_steps_and_pauses(monkeypatch):
    p=PaperPortfolio(asset="BTC",settlement="USDC")
    original={"runtime_started_at":NOW-600,"runtime_active_since":NOW-100,
              "runtime_elapsed_seconds":240.0}
    restored=paper._paper_state_with_runtime(p,original,NOW,active=True)
    assert restored["runtime_active_since"]==NOW-100
    assert restored["runtime_elapsed_seconds"]==240.0
    row_=row(active_since=NOW-100)
    row_["status"]="ACTIVE"
    row_["state_json"]=restored
    monkeypatch.setattr(paper.time,"time",lambda:NOW)
    seconds,tracked=paper._active_runtime_seconds(row_,restored)
    assert tracked is True
    assert seconds==340
    after=paper._paper_state_with_runtime(p,restored,NOW+100,active=False)
    assert after["runtime_active_since"]==NOW-100
    assert after["runtime_elapsed_seconds"]==240.0


def test_pending_pause_only_scheduler_selects_paused_rows_and_never_runs_entry():
    source=inspect.getsource(worker.active_session_keys)
    assert "status='ACTIVE'" in source
    assert "status='PAUSED' AND state_json->>'pause_exit_pending'='true'" in source
    step=inspect.getsource(paper.step_user_paper)
    assert 'row["status"]=="PAUSED"' in step
    assert 'completed=_settle_pending_pause(' in step
    assert step.index("completed=_settle_pending_pause") < step.index("evaluate_coin_strategy(")
    assert '"status"]!="ACTIVE"' in step
    src=inspect.getsource(paper.set_user_paper_status)
    assert 'row["status"]!=before' in src
    assert '"pause_exit_pending") is True' in src
    assert 'raise HTTPException(409,"Pause settlement must finish before resume")' in src
    assert 'status=%s,state_json=%s' in src


def test_empty_position_pauses_without_fabricated_sale():
    original=row(quantity=0.0,active_since=None)
    cur=DummyCursor(original)
    result=paper._settle_pending_pause(cur,original,DID,"BTC",Market(),NOW)
    assert result["state_json"]["coin_quantity"]==0.0
    assert result["state_json"]["stable_cash"]==100.0
    assert result["state_json"]["trade_count"]==3
    assert result["state_json"].get("pause_exit_pending") is None
    assert cur.events==[]


def test_both_mainnet_execution_and_live_signatures_stay_disabled():
    from backend import app as api
    source=inspect.getsource(api.user_paper_action)
    assert "set_user_paper_status(did,asset,action,market,time.time())" in source
    assert "_require_paper_visible()" in source
    assert "verify_privy_bearer(authorization)" in source
    assert all(x not in inspect.getsource(paper._settle_pending_pause)
               for x in ("sendTransaction","signMessage","private_key","swapExact"))
