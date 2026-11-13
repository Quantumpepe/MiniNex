"""Unified, owner-only eleven-coin PAPER trade feed regression tests."""
from __future__ import annotations

from datetime import datetime,timezone,timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api, home_paper_trades as home

WHEN=datetime(2026,11,13,18,10,tzinfo=timezone.utc)


def fill(asset,event_id,kind="PAPER_BUY",moment=WHEN,**patch):
    event={
        "asset":asset,"kind":kind,"at":moment.isoformat(),
        "settlement":"USDC","real_trade":False,
        "reason":"REBOUND","price_reference":100.5,"quantity":1.2,
        "total_spent":120.5,
        "net_proceeds":117.5,"realized_net_profit":-3.0,
    }
    event.update(patch)
    return {"asset":asset,"event_id":event_id,"occurred_at":moment,
            "event_json":event}


def test_exact_coin_roster_and_ada_always_excluded():
    assert home.ALL_ASSETS==(
        "BTC","ETH","BNB","SOL","XRP","TRX","POL","AVAX",
        "LINK","DOGE","UNI"
    )
    assert "ADA" not in home.ALL_ASSETS
    assert len(set(home.ALL_ASSETS))==11
    for asset in home.ALL_ASSETS:
        row=home.normalize_home_trade(fill(asset,1))
        assert row is not None
        assert row["asset"]==asset
        assert row["paper_only"] is True
        assert row["real_trade"] is False
    assert home.normalize_home_trade(fill("ADA",1)) is None


def test_canonical_paper_fields_show_amount_price_sell_profit_and_no_real_execution():
    buy=home.normalize_home_trade(fill("BTC",2))
    assert buy["id"]=="BTC:2"
    assert buy["kind"]=="PAPER_BUY"
    assert buy["amount_usd"]==120.5
    assert buy["price_reference"]==100.5
    assert buy["quantity"]==1.2
    assert buy["realized_net_profit"] is None
    sell=home.normalize_home_trade(fill("ETH",3,"PAPER_SELL",
        price_reference=2222.15,net_proceeds=250.0,realized_net_profit=-18.75))
    assert sell["asset"]=="ETH"
    assert sell["price_reference"]==2222.15
    assert sell["amount_usd"]==250.0
    assert sell["realized_net_profit"]==-18.75
    assert sell["at"].startswith("2026-11-13T18:10:00")


@pytest.mark.parametrize("bad",[
    lambda row:row.update({"asset":"ADA"}),
    lambda row:row.update({"event_id":True}),
    lambda row:row.update({"event_id":-1}),
    lambda row:row.update({"occurred_at":"2026-11-13"}),
    lambda row:row.update({"occurred_at":datetime(2026,11,13,18,10)}),
    lambda row:row["event_json"].update({"real_trade":True}),
    lambda row:row["event_json"].update({"kind":"LIVE_BUY"}),
    lambda row:row["event_json"].update({"asset":"BNB"}),
    lambda row:row["event_json"].update({"settlement":"BTC"}),
    lambda row:row["event_json"].update({"price_reference":float("nan")}),
    lambda row:row["event_json"].update({"quantity":0}),
    lambda row:row["event_json"].update({"total_spent":-5}),
    lambda row:row["event_json"].update({"total_spent":float("inf")}),
])
def test_untrusted_or_real_trade_rows_are_rejected(bad):
    row=fill("SOL",1)
    bad(row)
    assert home.normalize_home_trade(row) is None


def test_sell_without_realized_net_pnl_is_not_claimed_as_success():
    row=fill("UNI",5,"PAPER_SELL",realized_net_profit=None)
    assert home.normalize_home_trade(row) is None


class FakeDB:
    def __init__(self,rows=None):
        self.rows=rows or []
        self.calls=[]
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def cursor(self,**_):return self
    def execute(self,sql,params):
        self.calls.append((sql,params))
    def fetchall(self):
        return self.rows


def test_one_bounded_owner_scoped_query_joins_two_ledgers_without_mutating(monkeypatch):
    rows=[
        fill("SOL",1,moment=WHEN+timedelta(seconds=3)),
        fill("BTC",9,moment=WHEN+timedelta(seconds=2)),
        fill("LINK",2,"PAPER_SELL",moment=WHEN+timedelta(seconds=1)),
        fill("ETH",5,moment=WHEN),
    ]
    db=FakeDB(rows)
    monkeypatch.setattr(home,"_db_url",lambda:"postgresql://fake")
    monkeypatch.setattr(home,"_ensure_schema",lambda url:None)
    monkeypatch.setattr(home.psycopg,"connect",lambda *a,**kw:db)
    response=home.recent_home_trades("did:privy:alice")
    assert response["scope"]=="PRIVATE_ACCOUNT"
    assert response["sort"]=="NEWEST_FIRST"
    assert response["wallet_funds_used"] is False
    assert response["real_trading_enabled"] is False
    assert [row["asset"] for row in response["trades"]]==[
        "SOL","BTC","LINK","ETH"]
    assert len(db.calls)==1
    sql,params=db.calls[0]
    assert "mininex_user_paper_trades" in sql
    assert "mininex_extra_paper_trades" in sql
    assert "UNION ALL" in sql
    assert "ORDER BY occurred_at DESC,asset ASC,event_id DESC" in sql
    assert not any(keyword in sql for keyword in ("INSERT","UPDATE","DELETE"))
    assert params==(
        "did:privy:alice",["BTC","ETH"],
        "did:privy:alice",list(home.EXTRA_ASSETS),60)
    for value in (0,61,True,"60"):
        with pytest.raises(HTTPException) as err:
            home.recent_home_trades("did:privy:alice",value)
        assert err.value.status_code==422
    assert len(db.calls)==1


def test_unstarted_user_has_no_cross_account_or_simulated_global_events(monkeypatch):
    db=FakeDB([])
    monkeypatch.setattr(home,"_db_url",lambda:"postgresql://fake")
    monkeypatch.setattr(home,"_ensure_schema",lambda url:None)
    monkeypatch.setattr(home.psycopg,"connect",lambda *a,**kw:db)
    assert home.recent_home_trades("did:privy:empty")["trades"]==[]
    assert db.calls[0][1][0]=="did:privy:empty"


def test_api_requires_privy_token_and_paper_mode(monkeypatch):
    client=TestClient(api.app)
    assert client.get("/api/home/paper-trades").status_code==401
    checked=[]
    monkeypatch.setattr(api,"verify_privy_bearer",lambda token:
        checked.append(token) or {"user_id":"did:privy:alice"})
    monkeypatch.setattr(api,"recent_home_trades",lambda did:
        {"scope":"PRIVATE_ACCOUNT","paper_only":True,"trades":[]})
    reply=client.get("/api/home/paper-trades",
                     headers={"Authorization":"Bearer verified-session"})
    assert reply.status_code==200
    assert checked==["Bearer verified-session"]
    assert reply.headers["cache-control"]=="private, no-store"
    assert reply.headers["x-content-type-options"]=="nosniff"
    monkeypatch.setenv("MININEX_LIVE_TRADING_ENABLED","true")
    assert client.get("/api/home/paper-trades",
        headers={"Authorization":"Bearer verified-session"}).status_code==404
    assert checked==["Bearer verified-session"]
