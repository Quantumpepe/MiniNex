"""Durable nine-coin PAPER invariants: no real orders, no cross-user balances."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api, extra_paper, personal_paper_worker as worker, storage
from backend.auto_engine import PaperPortfolio
from backend.strategy_replay import EXTRA_REPLAY_ASSETS


NOW=1_800_000_000.0


class FakeMarkets:
    def __init__(self,valid=True):
        self.valid=valid
        self.calls=0
        self.prices={asset:float(25+idx*10) for idx,asset in enumerate(EXTRA_REPLAY_ASSETS)}

    def snapshot(self, *, now=None):
        self.calls+=1
        return {asset:{"market_data_valid":self.valid,
                       "price_usdt":self.prices[asset] if self.valid else None,
                       "reason":"OK" if self.valid else "MISSING"}
                for asset in EXTRA_REPLAY_ASSETS}

    def history(self,asset,*,limit=3600):
        assert asset in EXTRA_REPLAY_ASSETS
        return [
            {"timestamp":NOW-600+i*5.0,
             "price_usdt":self.prices[asset]*(1+0.00005*i)}
            for i in range(121)
        ][-limit:]


class FakeDB:
    """Small SQL shape simulator exercising service behavior without Postgres."""
    def __init__(self):
        self.sessions={}
        self.events={}
        self.result=None
        self.sql=[]
        self.rowcount=0

    def __enter__(self):return self
    def __exit__(self,*args):return False
    def cursor(self,**_):return self
    def fetchone(self):
        return deepcopy(self.result[0]) if self.result else None
    def fetchall(self):
        return deepcopy(self.result or [])

    @staticmethod
    def unwrap(value):
        return deepcopy(getattr(value,"obj",value))

    def execute(self,sql,params=()):
        self.sql.append(sql)
        normalized=" ".join(sql.split())
        self.result=[]
        self.rowcount=0
        if normalized.startswith("SELECT * FROM mininex_extra_paper_sessions"):
            row=self.sessions.get((params[0],params[1]))
            self.result=[row] if row else []
        elif normalized.startswith("SELECT event_json FROM mininex_extra_paper_trades"):
            did,asset,limit=params
            events=[(event_id,item) for (d,a,event_id),item in self.events.items()
                    if (d,a)==(did,asset)]
            self.result=[{"event_json":x} for _,x in
                         sorted(events,reverse=True)[:limit]]
        elif normalized.startswith("INSERT INTO mininex_extra_paper_sessions"):
            did,asset,stable,mode,horizon,budget,payload=params
            key=(did,asset)
            if key not in self.sessions:
                self.sessions[key]={
                    "privy_did":did,"asset":asset,"status":"ACTIVE",
                    "settlement":stable,"strategy_mode":mode,"horizon":horizon,
                    "initial_demo_cash":Decimal(budget),
                    "state_json":self.unwrap(payload),"revision":0,
                }
                self.result=[self.sessions[key]]
                self.rowcount=1
        elif normalized.startswith("INSERT INTO mininex_extra_paper_trades"):
            did,asset,event_id,occurred_at,payload=params
            self.events.setdefault((did,asset,event_id),self.unwrap(payload))
            self.rowcount=1
        elif normalized.startswith("UPDATE mininex_extra_paper_sessions"):
            if "SET status=" in normalized:
                status,payload,did,asset,expected=params
                row=self.sessions.get((did,asset))
                if row and row["status"]==expected:
                    row.update(status=status,state_json=self.unwrap(payload),
                               revision=row["revision"]+1)
                    self.result=[row]
                    self.rowcount=1
            else:
                payload,did,asset=params
                row=self.sessions.get((did,asset))
                expected="PAUSED" if "status='PAUSED'" in normalized else "ACTIVE"
                if row and row["status"]==expected:
                    row.update(state_json=self.unwrap(payload),
                               revision=row["revision"]+1)
                    self.result=[row]
                    self.rowcount=1
        elif normalized.startswith("SELECT privy_did,asset FROM mininex_extra_paper_sessions"):
            limit=params[0]
            rows=[{"privy_did":did,"asset":asset}
                  for (did,asset),row in self.sessions.items()
                  if row["status"]=="ACTIVE" or
                     (row["status"]=="PAUSED" and row["state_json"].get("pause_exit_pending"))]
            self.result=rows[:limit]
        else:
            raise AssertionError("Unexpected SQL from extra PAPER: "+normalized[:160])


@pytest.fixture
def fake_db(monkeypatch):
    db=FakeDB()
    monkeypatch.setattr(extra_paper,"_db_url",lambda:"postgresql://fake")
    monkeypatch.setattr(extra_paper,"_ensure_schema",lambda _:None)
    monkeypatch.setattr(extra_paper,"_prepare_rows",lambda cur,did:None)
    monkeypatch.setattr(extra_paper.psycopg,"connect",lambda *args,**kwargs:db)
    return db


@pytest.fixture
def signed(monkeypatch):
    private=ec.generate_private_key(ec.SECP256R1())
    public=private.public_key().public_bytes(
        serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",public)
    def make(did="did:privy:alice"):
        at=int(time.time())
        return {"Authorization":"Bearer "+jwt.encode({
            "iss":"privy.io","aud":"cmw7sb7ty013o0gl55aqyzi3y",
            "sub":did,"sid":"extra-paper-test","iat":at,"exp":at+600
        },private,algorithm="ES256")}
    return make


def test_extra_paper_schema_is_separate_and_per_account_coin():
    sql="\n".join(storage.SCHEMA)
    assert "CREATE TABLE IF NOT EXISTS mininex_extra_paper_sessions" in sql
    assert "CREATE TABLE IF NOT EXISTS mininex_extra_paper_trades" in sql
    assert "PRIMARY KEY (privy_did,asset,event_id)" in sql
    assert "FOREIGN KEY (privy_did,asset)" in sql
    assert "('BNB','SOL','XRP','TRX','POL','AVAX','LINK','DOGE','UNI')" in sql
    assert "asset TEXT NOT NULL CHECK (asset IN ('BTC','ETH'))" in sql
    assert "mininex_live_order_intents" in sql
    assert "FOR UPDATE" in extra_paper._row.__code__.co_consts
    assert len(EXTRA_REPLAY_ASSETS)==9


@pytest.mark.parametrize("coin",EXTRA_REPLAY_ASSETS)
def test_each_coin_cannot_use_real_wallet_or_restricted_asset(coin):
    assert extra_paper.valid_asset(coin)==coin
    assert extra_paper.validate_start(500,"LONG","USDC","15m")==Decimal(500)
    for bad in ("ADA","BTC","ETH","JAM","xrp","",None):
        with pytest.raises(HTTPException):
            extra_paper.valid_asset(bad)


@pytest.mark.parametrize("bad",[True,500.1,"500",99,5001,-1,0,None])
def test_virtual_budget_validation_is_exact_and_bounded(bad):
    with pytest.raises(HTTPException):
        extra_paper.validate_start(bad,"LONG","USDT","5m")
    for mode,stable,horizon in [("LEVERAGED_SHORT","USDT","15m"),
                                ("LONG","ETH","15m"),("LONG","USDC","72h")]:
        with pytest.raises(HTTPException):
            extra_paper.validate_start(500,mode,stable,horizon)


def test_start_is_opt_in_one_session_per_account_coin_and_no_global_capital_rotation(fake_db):
    feed=FakeMarkets()
    for did,coin,budget in (
        ("did:privy:alice","SOL",900),
        ("did:privy:alice","BNB",250),
        ("did:privy:bob","SOL",1250),
    ):
        started=extra_paper.start(did,coin,budget,"LONG","USDC","15m",feed,NOW)
        assert started["status"]=="ACTIVE"
        assert started["asset"]==coin
        assert started["budget_usd"]==budget
        assert started["wallet_funds_used"] is False
        assert started["real_trading_enabled"] is False
        assert started["budget_isolated"] is True
    assert len(fake_db.sessions)==3
    a=extra_paper.read("did:privy:alice","SOL")
    b=extra_paper.read("did:privy:bob","SOL")
    assert a["budget_usd"]==900
    assert b["budget_usd"]==1250
    assert a["position"]["stable_cash"]=="900.0"
    assert b["position"]["stable_cash"]=="1250.0"
    with pytest.raises(HTTPException) as err:
        extra_paper.start("did:privy:alice","SOL",900,"LONG","USDC","15m",feed,NOW)
    assert err.value.status_code==409
    # Only explicit starts exist. A read on a missing coin never auto-starts.
    missing=extra_paper.read("did:privy:alice","DOGE")
    assert missing["status"]=="NOT_STARTED"
    assert len(fake_db.sessions)==3


def test_worker_tick_is_durable_and_never_repeats_before_cadence(fake_db):
    feed=FakeMarkets()
    extra_paper.start("did:privy:alice","SOL",500,"LONG","USDT","15m",feed,NOW)
    first=extra_paper.tick("did:privy:alice","SOL",feed,NOW)
    assert first["status"]=="ACTIVE"
    assert first["asset"]=="SOL"
    assert first["real_trading_enabled"] is False
    assert first["wallet_funds_used"] is False
    rev=first["revision"]
    again=extra_paper.tick("did:privy:alice","SOL",feed,NOW+5)
    assert again["revision"]==rev
    second=extra_paper.tick("did:privy:alice","SOL",feed,NOW+16)
    assert second["revision"]==rev+1
    assert all(event["real_trade"] is False for event in second["trades"])
    assert all("mininex_extra_paper_" in query
               for query in fake_db.sql)


def test_pause_resumes_preserving_state_and_converts_virtual_coin_to_stable(fake_db):
    feed=FakeMarkets()
    started=extra_paper.start("did:privy:alice","XRP",200,
                              "SPOT_SHORT","USDT","5m",feed,NOW)
    assert float(started["position"]["coin_quantity"])>0
    assert float(started["position"]["stable_cash"])==0
    paused=extra_paper.set_status("did:privy:alice","XRP","pause",feed,NOW+10)
    assert paused["status"]=="PAUSED"
    assert paused["pause_exit_pending"] is False
    assert paused["pause_exit_completed"] is True
    assert float(paused["position"]["coin_quantity"])==0
    assert float(paused["position"]["stable_cash"])>0
    assert len(paused["trades"])==1
    assert paused["trades"][0]["reason"]=="USER_PAUSE_TO_STABLE"
    profit=float(paused["position"]["realized_net_profit"])
    with pytest.raises(HTTPException):
        extra_paper.set_status("did:privy:alice","XRP","pause",feed,NOW+12)
    resumed=extra_paper.set_status("did:privy:alice","XRP","resume",feed,NOW+20)
    assert resumed["status"]=="ACTIVE"
    assert float(resumed["position"]["realized_net_profit"])==pytest.approx(profit)
    assert len(resumed["trades"])==1


def test_unverified_feed_keeps_pause_immediate_and_pending_sell_retries_once(fake_db):
    feed=FakeMarkets()
    extra_paper.start("did:privy:alice","UNI",350,"SPOT_SHORT","USDC","15m",feed,NOW)
    feed.valid=False
    paused=extra_paper.set_status("did:privy:alice","UNI","pause",feed,NOW+10)
    assert paused["status"]=="PAUSED"
    assert paused["pause_exit_pending"] is True
    assert float(paused["position"]["coin_quantity"])>0
    with pytest.raises(HTTPException) as err:
        extra_paper.set_status("did:privy:alice","UNI","resume",feed,NOW+11)
    assert err.value.status_code==409
    blocked=extra_paper.tick("did:privy:alice","UNI",feed,NOW+20)
    assert blocked["pause_exit_pending"] is True
    assert blocked["status"]=="PAUSED"
    feed.valid=True
    completed=extra_paper.tick("did:privy:alice","UNI",feed,NOW+35)
    assert completed["status"]=="PAUSED"
    assert completed["pause_exit_pending"] is False
    assert len(completed["trades"])==1
    with pytest.raises(HTTPException):
        extra_paper.tick("did:privy:alice","UNI",feed,NOW+50)
    assert len(fake_db.events)==1


def test_cash_only_and_stale_position_valuations_never_fake_profit(fake_db):
    feed=FakeMarkets(valid=False)
    extra_paper.start("did:privy:alice","DOGE",600,"LONG","USDC","15m",feed,NOW)
    cash=extra_paper.read("did:privy:alice","DOGE",feed)
    assert cash["position"]["valuation"]["quote_valid"] is True
    assert cash["position"]["valuation"]["estimated_total_equity_usd"]==600
    feed.valid=True
    extra_paper.start("did:privy:alice","AVAX",700,"SPOT_SHORT","USDC","15m",feed,NOW)
    feed.valid=False
    held=extra_paper.read("did:privy:alice","AVAX",feed)
    assert held["position"]["valuation"]["quote_valid"] is False
    assert held["position"]["valuation"]["estimated_total_equity_usd"] is None


def test_extra_worker_scans_only_opted_in_and_keeps_failures_isolated(monkeypatch):
    monkeypatch.setenv("DATABASE_URL","postgresql://fake")
    monkeypatch.setenv("MININEX_PAPER_AUTO_ENABLED","true")
    monkeypatch.setenv("MININEX_LIVE_TRADING_ENABLED","false")
    monkeypatch.setattr(worker,"active_extra_keys",lambda:[
        ("did:privy:bad","SOL"),("did:privy:good","DOGE")])
    touched=[]
    def tick(did,coin,source,now):
        if "bad" in did:raise ValueError("fixture")
        touched.append((did,coin))
    monkeypatch.setattr(worker,"tick_extra_paper",tick)
    assert worker.process_extra_sessions(FakeMarkets())==1
    assert touched==[("did:privy:good","DOGE")]
    monkeypatch.setenv("MININEX_LIVE_TRADING_ENABLED","true")
    assert worker.process_extra_sessions(FakeMarkets())==0


def test_public_paper_endpoints_require_privy_auth_and_ada_is_disabled(signed,monkeypatch):
    client=TestClient(api.app)
    for path,method in [
        ("/api/extra-paper/SOL","GET"),
        ("/api/extra-paper/SOL/start","POST"),
        ("/api/extra-paper/SOL/pause","POST"),
        ("/api/extra-paper/SOL/resume","POST"),
    ]:
        assert client.request(method,path).status_code==401
    calls=[]
    monkeypatch.setattr(api,"read_extra_paper",
        lambda did,asset,market:(calls.append(("read",did,asset)) or {"paper_only":True}))
    monkeypatch.setattr(api,"start_extra_paper",
        lambda did,asset,budget,mode,stable,horizon,market:
            (calls.append(("start",did,asset,budget,mode,stable,horizon)) or
             {"paper_only":True}))
    monkeypatch.setattr(api,"set_extra_paper_status",
        lambda did,asset,action,market,now:
            (calls.append((action,did,asset)) or {"paper_only":True}))
    assert client.get("/api/extra-paper/SOL",headers=signed("did:privy:alice")).status_code==200
    assert client.post("/api/extra-paper/UNI/start",
        headers=signed("did:privy:bob"),
        json={"budget_usd":600,"mode":"LONG","stable":"USDT","horizon":"15m"}).status_code==200
    assert client.post("/api/extra-paper/UNI/pause",
        headers=signed("did:privy:alice")).status_code==200
    assert calls==[
        ("read","did:privy:alice","SOL"),
        ("start","did:privy:bob","UNI",600,"LONG","USDT","15m"),
        ("pause","did:privy:alice","UNI"),
    ]
    for bad in [{"budget_usd":0},{"budget_usd":5001},{"budget_usd":"500"},
                {"budget_usd":500,"secret":"no"}]:
        assert client.post("/api/extra-paper/SOL/start",
            headers=signed(),json=bad).status_code==422
    # ADA remains explicitly excluded from this module.
    with pytest.raises(HTTPException):
        extra_paper.valid_asset("ADA")


def test_live_cutover_shuts_off_extra_paper_routes_and_worker(signed,monkeypatch):
    monkeypatch.setenv("MININEX_LIVE_TRADING_ENABLED","true")
    client=TestClient(api.app)
    assert client.get("/api/extra-paper/SOL",headers=signed()).status_code==404
    assert client.post("/api/extra-paper/SOL/start",headers=signed(),
        json={"budget_usd":500}).status_code==404
    assert client.post("/api/extra-paper/SOL/pause",headers=signed()).status_code==404
    assert worker.auto_worker_allowed() is False
