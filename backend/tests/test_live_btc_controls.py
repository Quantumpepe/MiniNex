"""Native BTC owns separate mainnet LIVE caps; no Privy grant or spend."""
from decimal import Decimal
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import live_btc_controls as btc
from backend import live_eth_controls as eth
from backend.storage import SCHEMA

DID="did:privy:independent-btc"
BOB="did:privy:btc-bob"


class FakeCursor:
    def __init__(self,store):
        self.store=store
        self.row=None
        self.calls=[]
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def fetchone(self):return self.row
    def execute(self,sql,params=()):
        self.calls.append((sql,params))
        if "FROM mininex_live_btc_limits" in sql:
            self.row=dict(self.store[params[0]])
        elif "UPDATE mininex_live_btc_limits" in sql:
            if "SET planned_capital_usd" in sql:
                capital,order,daily,did,revision=params
                self.row=None
                current=self.store[did]
                if current["revision"]==revision:
                    current.update(planned_capital_usd=capital,
                                   max_order_usd=order,
                                   max_daily_turnover_usd=daily,
                                   trading_paused=True,
                                   revision=revision+1)
                    self.row=dict(current)
            elif "SET trading_paused" in sql:
                did=params[0]
                current=self.store[did]
                current.update(trading_paused=True,revision=current["revision"]+1)
                self.row=dict(current)
            else:
                raise AssertionError(sql[:90])
        else:
            raise AssertionError("Unknown BTC SQL: "+sql[:90])


class FakeConnection:
    def __init__(self,cursor):self.cur=cursor
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def cursor(self,**kwargs):return self.cur


@pytest.fixture
def db(monkeypatch):
    default=lambda:dict(
        planned_capital_usd=Decimal("0"),
        max_order_usd=Decimal("0"),
        max_daily_turnover_usd=Decimal("0"),
        trading_paused=True,revision=0,
    )
    store={DID:default(),BOB:default()}
    cursor=FakeCursor(store)
    monkeypatch.setattr(btc,"_db_url",lambda:"postgresql://example")
    monkeypatch.setattr(btc,"_ensure_schema",lambda url:None)
    monkeypatch.setattr(btc,"_prepare_rows",lambda cur,did:None)
    monkeypatch.setattr(btc.psycopg,"connect",lambda *a,**kw:FakeConnection(cursor))
    return store,cursor


def test_btc_schema_is_independent_and_idempotent():
    sql="\n".join(SCHEMA)
    assert "CREATE TABLE IF NOT EXISTS mininex_live_btc_limits" in sql
    assert "trading_paused BOOLEAN NOT NULL DEFAULT TRUE" in sql
    assert "max_order_usd <= planned_capital_usd" in sql
    from backend.storage import _prepare_rows
    import inspect
    code=inspect.getsource(_prepare_rows)
    assert "INSERT INTO mininex_live_btc_limits" in code
    assert "ON CONFLICT (privy_did) DO NOTHING" in code


def test_one_btc_account_defaults_to_paused_and_independent(db):
    value=btc.get_live_btc_limits(DID)
    assert value["asset"]=="BTC"
    assert value["network"]=="bitcoin-mainnet"
    assert value["revision"]==0
    assert value["trading_paused"] is True
    assert value["native_utxo_spend_authorized"] is False
    assert value["btc_crosschain_execution_available"] is False
    assert value["paper_budget_used"] is False
    assert value["automated_signing_enabled"] is False


def test_btc_cap_not_limited_to_5000_demo_usd(db):
    store,queries=db
    result=btc.save_live_btc_limits(DID,
        capital="9900.25",order="4000",daily="8000",
        expected_revision=0)
    assert result["planned_capital_usd"]=="9900.25"
    assert result["trading_paused"] is True
    assert result["revision"]==1
    assert btc.get_live_btc_limits(BOB)["planned_capital_usd"]=="0"
    assert not any("mininex_live_eth_limits" in query for query,_ in queries.calls)
    assert not any("mininex_user_paper_sessions" in query for query,_ in queries.calls)


def test_stale_revision_cannot_overwrite_owner_btc_limits(db):
    btc.save_live_btc_limits(DID,capital="600",order="100",daily="200",expected_revision=0)
    with pytest.raises(HTTPException) as err:
        btc.save_live_btc_limits(DID,capital="700",order="200",
                                 daily="300",expected_revision=0)
    assert err.value.status_code==409
    assert btc.get_live_btc_limits(DID)["max_order_usd"]=="100"


def test_stop_btc_is_idempotent_and_leaves_eth_untouched(db):
    store,queries=db
    store[DID].update(trading_paused=False,revision=5)
    stopped=btc.stop_live_btc(DID)
    assert stopped["trading_paused"] is True
    assert stopped["revision"]==6
    again=btc.stop_live_btc(DID)
    assert again["revision"]==6
    assert all("mininex_live_eth_limits" not in sql for sql,_ in queries.calls)


@pytest.mark.parametrize("limits",[
    ("100","0","20"),("100","110","200"),("100","50","10"),
    ("NaN","5","10"),("50000000","1","1"),(100,10,10),
    ("100.0000001","10","50"),
])
def test_invalid_btc_limits_rejected_before_database(db,limits):
    _,cur=db
    with pytest.raises(HTTPException):
        btc.save_live_btc_limits(DID,capital=limits[0],order=limits[1],
                                  daily=limits[2],expected_revision=0)
    assert cur.calls==[]


@pytest.fixture
def headers(monkeypatch):
    key=ec.generate_private_key(ec.SECP256R1())
    public=key.public_key().public_bytes(
        serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",public)
    now=int(time.time())
    token=jwt.encode({
        "iss":"privy.io","aud":"cmw7sb7ty013o0gl55aqyzi3y",
        "sub":DID,"sid":"btc-owned-limit","iat":now,"exp":now+600,
    },key,algorithm="ES256")
    return {"Authorization":"Bearer "+token}


def test_btc_endpoints_authenticated_do_not_accept_broad_permissions(db,headers):
    client=TestClient(api.app)
    data={"planned_capital_usd":"1500","max_order_usd":"400",
          "max_daily_turnover_usd":"800","expected_revision":0}
    assert client.get("/api/live/btc/limits").status_code==401
    assert client.post("/api/live/btc/stop").status_code==401
    assert client.put("/api/live/btc/limits",json=data).status_code==401
    assert client.put("/api/live/btc/limits",json={
        **data,"privy_signer_id":"external",
    },headers=headers).status_code==422
    result=client.put("/api/live/btc/limits",headers=headers,json=data)
    assert result.status_code==200
    assert result.json()["asset"]=="BTC"
    assert result.json()["revision"]==1
    assert result.json()["trading_paused"] is True
    assert result.json()["live_trading_enabled"] is False
    assert result.json()["automated_signing_enabled"] is False
    own=client.get("/api/live/btc/limits",headers=headers)
    assert own.status_code==200
    assert own.headers["cache-control"]=="private, no-store"
    stopped=client.post("/api/live/btc/stop",headers=headers)
    assert stopped.status_code==200
    assert stopped.json()["trading_paused"] is True
    assert not any(route.path=="/api/live/btc/execute" for route in api.app.routes)
