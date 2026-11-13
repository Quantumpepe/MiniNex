"""Nine-coin PAPER comparison is read-only, account-isolated and cost-aware."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import math
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient

from backend import app as api, extra_paper_report as report
from backend.auto_engine import PaperPortfolio
from backend.user_paper import _state_of
from backend.strategy_replay import EXTRA_REPLAY_ASSETS

AT=1_800_000_000.0


def session(asset="SOL",budget=500,price=100,with_coin=True):
    p=PaperPortfolio(
        asset=asset,settlement="USDC",strategy_mode="LONG",
        stable_cash=400.0 if with_coin else float(budget),
        coin_quantity=1.0 if with_coin else 0.0,
        cost_basis=100.0 if with_coin else 0.0,
        initial_equity=float(budget),realized_profit=7.12,
        trade_count=3,last_evaluated_at=AT,
    )
    state={**_state_of(p),"runtime_started_at":AT-1800,
           "runtime_active_since":AT-1800,
           "runtime_elapsed_seconds":0.0}
    return {"asset":asset,"status":"ACTIVE","settlement":"USDC",
            "strategy_mode":"LONG","horizon":"15m","revision":2,
            "initial_demo_cash":Decimal(budget),"state_json":state}


def event(asset,kind,pnl=None,cost=1.0):
    e={"asset":asset,"kind":kind,"real_trade":False,
       "estimated_costs":{"venue_fee":cost/2,"network_fee":cost/4,
                          "spread_and_slippage_cost":cost/4}}
    if kind=="PAPER_SELL":
        e["realized_net_profit"]=pnl
    return e


def test_all_nine_rows_are_separate_and_missing_coins_do_not_fake_profit():
    out=[report.summarize(asset,None,[],{}) for asset in EXTRA_REPLAY_ASSETS]
    assert len(out)==9 and len(set(r["asset"] for r in out))==9
    assert all(r["status"]=="NOT_STARTED" and r["budget_usd"] is None and
               r["estimated_net_return_pct"] is None and r["trade_count"]==0
               for r in out)
    assert all(r["real_trading_enabled"] is False and
               r["wallet_funds_used"] is False and
               r["parameters_unchanged"] is True for r in out)


def test_valid_price_estimates_current_equity_after_hypothetical_sale_costs():
    row=session()
    source={"SOL":{"market_data_valid":True,"price_usdt":120.0}}
    before=deepcopy(row)
    out=report.summarize("SOL",row,[event("SOL","PAPER_BUY",cost=1.5),
                                    event("SOL","PAPER_SELL",pnl=4.5,cost=2.0),
                                    event("SOL","PAPER_SELL",pnl=-2.1,cost=0.9)],source)
    assert out["status"]=="ACTIVE"
    assert out["budget_usd"]==500
    assert out["trade_count"]==3
    assert out["quote_valid"] is True
    assert 510 < out["estimated_equity_usd"] < 520
    assert out["estimated_total_pnl_usd"]==pytest.approx(
        out["estimated_equity_usd"]-500)
    assert out["estimated_net_return_pct"]>0
    assert out["cumulative_realized_pnl_usd"]==7.12
    assert out["recent_buys"]==1
    assert out["recent_closed_sells"]==2
    assert out["recent_winning_sells"]==1
    assert out["recent_losing_sells"]==1
    assert out["recent_flat_sells"]==0
    assert out["recent_estimated_costs_usd"]==4.4
    assert out["recent_realized_pnl_usd"]==2.4
    assert out["recent_window_limit"]==30
    assert out["parameters_unchanged"] is True
    assert row==before


def test_stale_market_never_claims_virtual_equity_or_current_net_return():
    row=session()
    for feeds in ({},{"SOL":{"market_data_valid":False,"price_usdt":120.0}},
                  {"SOL":{"market_data_valid":True,"price_usdt":None}},
                  {"SOL":{"market_data_valid":True,"price_usdt":math.nan}}):
        out=report.summarize("SOL",row,[],feeds)
        assert out["quote_valid"] is False
        assert out["estimated_equity_usd"] is None
        assert out["estimated_net_return_pct"] is None
        assert out["estimated_total_pnl_usd"] is None
        assert out["cumulative_realized_pnl_usd"]==7.12
    cash=report.summarize("UNI",session(asset="UNI",with_coin=False),[],{})
    assert cash["quote_valid"] is True
    assert cash["estimated_equity_usd"]==500


def test_limited_recent_trades_do_not_masquerade_as_lifetime_fees():
    row=session()
    entries=[event("SOL","PAPER_BUY",cost=0.5) for _ in range(25)]
    entries.extend([event("SOL","PAPER_SELL",pnl=1.25,cost=1.0) for _ in range(5)])
    out=report.summarize("SOL",row,entries,{})
    assert out["recent_buys"]==25
    assert out["recent_closed_sells"]==5
    assert out["recent_winning_sells"]==5
    assert out["recent_estimated_costs_usd"]==17.5
    assert out["recent_realized_pnl_usd"]==6.25
    assert out["trade_count"]==3  # State is authoritative; do not infer lifetime count.
    malformed=report.summarize("SOL",row,[{"asset":"SOL","kind":"PAPER_SELL",
                                         "real_trade":False,
                                         "realized_net_profit":float("nan")}],{})
    assert malformed["recent_costs_complete"] is False
    assert malformed["recent_estimated_costs_usd"] is None
    assert malformed["recent_realized_pnl_usd"] is None


def test_sample_does_not_double_subtract_estimated_buy_or_sell_costs():
    row=session(with_coin=False)
    rows=[event("SOL","PAPER_BUY",cost=11),
          event("SOL","PAPER_SELL",pnl=-3,cost=7)]
    out=report.summarize("SOL",row,rows,{})
    assert out["estimated_equity_usd"]==500
    assert out["recent_estimated_costs_usd"]==18
    assert out["estimated_total_pnl_usd"]==0
    assert out["cumulative_realized_pnl_usd"]==7.12


class FakeDB:
    def __init__(self):
        self.calls=[]
        self.did=None
        self.result=[]
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def cursor(self,**kwargs):return self
    def execute(self,sql,params=()):
        normalized=" ".join(sql.split())
        self.calls.append(normalized)
        if "FROM mininex_extra_paper_sessions" in normalized:
            self.did=params[0]
            self.result=([session("SOL")] if params[0]=="did:privy:alice" else [])
        elif "FROM mininex_extra_paper_trades" in normalized:
            self.result=([{"asset":"SOL","event_json":event("SOL","PAPER_SELL",pnl=2.5)}]
                         if params[0]=="did:privy:alice" else [])
        else:raise AssertionError("Unexpected SQL")
    def fetchall(self):return deepcopy(self.result)


def test_compact_authenticated_comparison_one_db_round_trip_and_one_market_snapshot(monkeypatch):
    db=FakeDB()
    class Feed:
        requests=0
        def snapshot(self):
            self.requests+=1
            return {"SOL":{"market_data_valid":True,"price_usdt":105.0}}
    feed=Feed()
    monkeypatch.setattr(report,"_db_url",lambda:"postgresql://fake")
    monkeypatch.setattr(report,"_ensure_schema",lambda _:None)
    monkeypatch.setattr(report,"_prepare_rows",lambda cur,did:None)
    monkeypatch.setattr(report.psycopg,"connect",lambda *a,**k:db)
    first=report.compare("did:privy:alice",feed)
    assert len(first["assets"])==9
    assert first["assets"][0]["asset"]=="BNB"
    sol=next(r for r in first["assets"] if r["asset"]=="SOL")
    assert sol["status"]=="ACTIVE" and sol["trade_count"]==3
    assert sol["recent_closed_sells"]==1
    assert sum(r["status"]=="NOT_STARTED" for r in first["assets"])==8
    assert feed.requests==1
    assert len(db.calls)==2
    assert all("privy_did=%s" in q for q in db.calls)
    assert not any(q.startswith(("UPDATE","INSERT","DELETE")) for q in db.calls)
    other=report.compare("did:privy:bob",feed)
    assert all(r["status"]=="NOT_STARTED" for r in other["assets"])
    assert first["assets"][0]["budget_usd"] is None


@pytest.fixture
def signed(monkeypatch):
    private=ec.generate_private_key(ec.SECP256R1())
    public=private.public_key().public_bytes(
        serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",public)
    def make(did="did:privy:alice"):
        now=int(time.time())
        return {"Authorization":"Bearer "+jwt.encode({
            "iss":"privy.io","aud":"cmw7sb7ty013o0gl55aqyzi3y",
            "sub":did,"sid":"comparison","iat":now,"exp":now+600,
        },private,algorithm="ES256")}
    return make


def test_comparison_http_requires_owner_token_and_does_not_shadow_asset_route(signed,monkeypatch):
    client=TestClient(api.app)
    assert client.get("/api/extra-paper/comparison").status_code==401
    calls=[]
    monkeypatch.setattr(api,"compare_extra_paper",lambda did,market:
        (calls.append(did) or {"paper_only":True,"assets":[]}))
    response=client.get("/api/extra-paper/comparison",
                        headers=signed("did:privy:alice"))
    assert response.status_code==200
    assert response.headers["cache-control"]=="private, no-store"
    assert calls==["did:privy:alice"]
    monkeypatch.setenv("MININEX_LIVE_TRADING_ENABLED","true")
    assert client.get("/api/extra-paper/comparison",headers=signed()).status_code==404
    assert calls==["did:privy:alice"]
