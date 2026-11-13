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
    assert response["selected_asset"]=="ALL"
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
    checked_scopes=[]
    monkeypatch.setattr(api,"recent_home_trades",lambda did,asset:
        (checked_scopes.append((did,asset)) or
         {"scope":"PRIVATE_ACCOUNT","paper_only":True,"trades":[],"selected_asset":asset}))
    reply=client.get("/api/home/paper-trades",
                     headers={"Authorization":"Bearer verified-session"})
    assert reply.status_code==200
    assert checked==["Bearer verified-session"]
    assert checked_scopes==[("did:privy:alice","ALL")]
    assert reply.headers["cache-control"]=="private, no-store"
    assert reply.headers["x-content-type-options"]=="nosniff"
    monkeypatch.setenv("MININEX_LIVE_TRADING_ENABLED","true")
    assert client.get("/api/home/paper-trades",
        headers={"Authorization":"Bearer verified-session"}).status_code==404
    assert checked==["Bearer verified-session"]


@pytest.mark.parametrize("asset,table", [
    (asset, "mininex_user_paper_trades" if asset in ("BTC", "ETH")
     else "mininex_extra_paper_trades")
    for asset in home.ALL_ASSETS
])
def test_selected_coin_history_is_filtered_in_sql_before_limit(asset,table,monkeypatch):
    db=FakeDB([fill(asset,5)])
    monkeypatch.setattr(home,"_db_url",lambda:"postgresql://fake")
    monkeypatch.setattr(home,"_ensure_schema",lambda _:None)
    monkeypatch.setattr(home.psycopg,"connect",lambda *a,**kw:db)
    result=home.recent_home_trades("did:privy:owner",asset=asset)
    assert result["selected_asset"]==asset
    assert [row["asset"] for row in result["trades"]]==[asset]
    assert len(db.calls)==1
    sql,params=db.calls[0]
    assert table in sql and "UNION ALL" not in sql
    assert "AND asset=%s" in sql
    assert "LIMIT %s" in sql
    assert params==("did:privy:owner",asset,60)


def test_wrong_coin_results_cannot_leak_into_selected_view(monkeypatch):
    db=FakeDB([fill("ETH",2),fill("BTC",1)])
    monkeypatch.setattr(home,"_db_url",lambda:"postgresql://fake")
    monkeypatch.setattr(home,"_ensure_schema",lambda _:None)
    monkeypatch.setattr(home.psycopg,"connect",lambda *a,**kw:db)
    result=home.recent_home_trades("did:privy:owner",asset="BTC")
    assert [x["asset"] for x in result["trades"]]==["BTC"]
    for invalid in ("ADA","DOGE2","BTC;DROP",None,12):
        with pytest.raises(HTTPException) as err:
            home.recent_home_trades("did:privy:owner",asset=invalid)
        assert err.value.status_code==422


def test_explicit_all_coin_mode_remains_available_in_private_api(monkeypatch):
    checked=[]
    monkeypatch.setattr(api,"verify_privy_bearer",lambda token:
        {"user_id":"did:privy:alice"})
    monkeypatch.setattr(api,"recent_home_trades",lambda did,asset:
        (checked.append((did,asset)) or {"selected_asset":asset,"trades":[]}))
    client=TestClient(api.app)
    for symbol in ("BTC","ETH","SOL","ALL"):
        reply=client.get("/api/home/paper-trades?asset="+symbol,
            headers={"Authorization":"Bearer valid"})
        assert reply.status_code==200
        assert reply.json()["selected_asset"]==symbol
    assert checked==[("did:privy:alice",x) for x in
                    ("BTC","ETH","SOL","ALL")]
