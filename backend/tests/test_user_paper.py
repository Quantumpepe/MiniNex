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
    monkeypatch.setattr(api,"start_user_paper",lambda did,asset,market:
                        (calls.append(("start",did,asset)) or {"paper_only":True}))
    monkeypatch.setattr(api,"set_user_paper_status",lambda did,asset,action,market,now:
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


def test_all_selected_horizons_have_no_artificial_wait():
    """History is real Binance candle data, not time spent in a session."""
    import inspect
    src=inspect.getsource(user_paper.step_user_paper)
    assert 'warmup_seconds=0.0' in src
    assert '"5m":300.0' in src
    assert '"15m":900.0' in src
    assert '"1h":3600.0' in src
    assert '"24h":86400.0' in src
    assert '24H_HISTORY_NOT_AVAILABLE' not in src


def test_three_minute_history_is_enough_for_conservative_signal_evaluation():
    from backend.auto_engine import PaperAutoEngine,PaperRules
    class History:
        def history(self,asset,limit=3600):
            return [{"timestamp":float(t),"price_usdt":70000.0+0.1*t}
                    for t in range(1,192)]
    engine=PaperAutoEngine(History(),rules=PaperRules(
        warmup_seconds=190,price_window_seconds=900))
    signal=engine._signal("BTC",191.0,70019.1)
    assert signal["ready"] is True
    assert signal["samples"]>=30

def test_5000_virtual_usd_budget_rebase_preserves_existing_results():
    from decimal import Decimal
    assert user_paper.PAPER_START_USD == Decimal("5000.00")
    p=PaperPortfolio(asset="BTC",settlement="USDC",stable_cash=501.128425,
                     initial_equity=500.0,realized_profit=1.128425,
                     trade_count=2,last_exit_price=75500.0)
    assert user_paper._reconcile_virtual_budget(p,Decimal("500.00"),Decimal("5000.00"))
    assert p.stable_cash == pytest.approx(5001.128425)
    assert p.initial_equity == 5000.0
    assert p.realized_profit == pytest.approx(1.128425)
    assert p.trade_count == 2
    assert p.last_exit_price == 75500.0


def test_virtual_budget_shrink_does_not_liquidate_positions():
    from decimal import Decimal
    p=PaperPortfolio(asset="ETH",settlement="USDC",stable_cash=10.0,
                     coin_quantity=0.5,cost_basis=490.0,
                     initial_equity=500.0,trade_count=1)
    assert not user_paper._reconcile_virtual_budget(p,Decimal("500.00"),Decimal("250.00"))
    assert p.stable_cash == 10.0
    assert p.coin_quantity == 0.5
    assert p.cost_basis == 490.0
    assert p.initial_equity == 500.0
    assert p.trade_count == 1

def test_multi_buy_state_survives_personal_paper_roundtrip():
    p=PaperPortfolio(asset="ETH",settlement="USDC",stable_cash=1500.0,
        coin_quantity=1.6,cost_basis=3450.0,entry_price=2150.0,
        last_buy_price=2085.0,add_count=2,trade_count=3)
    q=user_paper._restore("ETH","USDC",user_paper._state_of(p),user_paper.PAPER_START_USD)
    assert q.add_count==2 and q.last_buy_price==2085.0
    assert q.cost_basis==3450.0 and q.trade_count==3
