"""Personal demo boundaries: separate Privy users/assets, no wallet execution."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api, storage, user_paper
from backend.auto_engine import PaperPortfolio

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
            "sub":did,"sid":"paper-test","iat":now,"exp":now+600
        },private,algorithm="ES256")}
    return make


def test_user_demo_endpoints_require_login(signed):
    client=TestClient(api.app)
    for path,method in [
        ("/api/user-paper/BTC","GET"),
        ("/api/user-paper/BTC/start","POST"),
        ("/api/user-paper/BTC/pause","POST"),
        ("/api/user-paper/BTC/evaluate","POST"),
    ]:
        assert client.request(method,path).status_code==401
        assert client.request(method,path,headers={"Authorization":"Bearer fake"}).status_code==401


def test_verified_identity_is_never_taken_from_path_or_body(monkeypatch,signed):
    calls=[]
    monkeypatch.setattr(api,"read_user_paper",lambda did,asset:
                        (calls.append(("read",did,asset)) or {"paper_only":True}))
    monkeypatch.setattr(api,"start_user_paper",lambda did,asset:
                        (calls.append(("start",did,asset)) or {"paper_only":True}))
    monkeypatch.setattr(api,"set_user_paper_status",lambda did,asset,action:
                        (calls.append((action,did,asset)) or {"paper_only":True}))
    monkeypatch.setattr(api,"step_user_paper",lambda did,asset,market,now:
                        (calls.append(("evaluate",did,asset)) or {"paper_only":True}))
    client=TestClient(api.app)
    assert client.get("/api/user-paper/BTC",headers=signed("did:privy:alice")).status_code==200
    assert client.post("/api/user-paper/ETH/start",headers=signed("did:privy:bob")).status_code==200
    assert client.post("/api/user-paper/ETH/pause",headers=signed("did:privy:alice")).status_code==200
    assert client.post("/api/user-paper/BTC/evaluate",headers=signed("did:privy:bob")).status_code==200
    assert calls==[
        ("read","did:privy:alice","BTC"),
        ("start","did:privy:bob","ETH"),
        ("pause","did:privy:alice","ETH"),
        ("evaluate","did:privy:bob","BTC"),
    ]


def test_database_constraints_isolate_user_asset_and_fills():
    schema="\n".join(storage.SCHEMA)
    assert "mininex_user_paper_sessions" in schema
    assert "mininex_user_paper_trades" in schema
    assert "PRIMARY KEY (privy_did,asset)" in schema
    assert "PRIMARY KEY (privy_did,asset,event_id)" in schema
    assert "FOR UPDATE" in user_paper._session_row.__code__.co_consts
    assert "CHECK (status IN ('NOT_STARTED','ACTIVE','PAUSED'))" in schema


def test_paper_state_roundtrip_without_writable_private_keys():
    p=PaperPortfolio(asset="BTC",settlement="USDC",stable_cash=500.0,
                     coin_quantity=0.01,cost_basis=230.0,peak_price=30000.0,
                     realized_profit=-9.5,trade_count=12)
    state=user_paper._state_of(p)
    state["private_key"]="should not be loaded"
    recovered=user_paper._restore("BTC","USDC",state,user_paper.PAPER_START_USD)
    assert recovered.coin_quantity==0.01
    assert recovered.realized_profit==-9.5
    assert recovered.trade_count==12
    assert not hasattr(recovered,"private_key")
    assert recovered.asset=="BTC" and recovered.settlement=="USDC"
    assert user_paper._session_data(None)["real_trading_enabled"] is False
    assert user_paper._session_data(None)["wallet_funds_used"] is False


def test_fail_closed_invalid_assets_and_states():
    for invalid in ("SOL","btc",""):
        with pytest.raises(HTTPException):
            user_paper._asset(invalid)
    with pytest.raises(ValueError):
        user_paper._restore("ETH","USDT",{"stable_cash":-1},user_paper.PAPER_START_USD)
    with pytest.raises(ValueError):
        user_paper._restore("ETH","USDT",{"trade_count":True},user_paper.PAPER_START_USD)
