"""One mode/horizon save is shared by all eleven MiniNex trading coins."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import storage, unified_strategy_profile as profile

DID = "did:privy:one-strategy"


class Cursor:
    def __init__(self):
        self.profile = {"mode": "LONG", "horizon": "15m", "revision": 0}
        self.core = {
            "BTC": {"mode": "LONG", "stable": "USDC", "horizon": "15m", "revision": 3,
                    "authorized": False},
            "ETH": {"mode": "SPOT_SHORT", "stable": "USDT", "horizon": "1h", "revision": 5,
                    "authorized": False},
        }
        self.rowcount = 0
        self.result = None
        self.queries = []
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def execute(self, sql, args):
        self.queries.append((sql, args))
        if "UPDATE mininex_unified_strategy_profiles" in sql:
            mode, horizon, did, rev = args
            assert did == DID
            self.result = None
            if rev == self.profile["revision"]:
                self.profile = {"mode": mode, "horizon": horizon, "revision": rev + 1}
                self.result = {"revision": rev + 1}
        elif "UPDATE mininex_trade_preferences" in sql:
            mode, horizon, did, assets = args
            assert did == DID and assets == ["BTC", "ETH"]
            for asset in assets:
                self.core[asset] = {**self.core[asset], "mode": mode, "horizon": horizon,
                                    "revision": self.core[asset]["revision"] + 1}
            self.rowcount = len(assets)
        elif "SELECT mode,horizon,revision" in sql:
            assert args == (DID,)
            self.result = dict(self.profile)
        else:
            raise AssertionError("Unexpected SQL statement " + sql[:80])
    def fetchone(self): return self.result


class Connection:
    def __init__(self, cursor): self.c = cursor
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def cursor(self, **kwargs): return self.c


def fake_database(monkeypatch):
    c = Cursor()
    monkeypatch.setattr(profile, "_db_url", lambda: "postgresql://test")
    monkeypatch.setattr(profile, "_ensure_schema", lambda url: None)
    monkeypatch.setattr(profile, "_prepare_rows", lambda cursor, did: None)
    monkeypatch.setattr(profile.psycopg, "connect", lambda *args, **kwargs: Connection(c))
    monkeypatch.setattr(profile, "_core_settings_snapshot",
                        lambda cursor, did: {"settings": dict(cursor.core)})
    return c


def test_profile_covers_every_coin_without_ada_and_preserves_independent_capital(monkeypatch):
    c = fake_database(monkeypatch)
    state = profile.get_strategy_profile(DID)
    assert len(state["assets"]) == 11
    assert state["assets"] == ["BTC", "ETH", "BNB", "SOL", "XRP", "TRX", "POL",
                               "AVAX", "LINK", "DOGE", "UNI"]
    assert state["ada_in_progress"] is True
    assert state["independent_budgets_preserved"] is True
    assert state["existing_sessions_unchanged"] is True
    assert state["real_trading_enabled"] is False

    result = profile.save_strategy_profile(
        DID, mode="SPOT_SHORT", horizon="24h", expected_revision=0)
    assert result["strategy_mode"] == "SPOT_SHORT"
    assert result["horizon"] == "24h"
    assert result["revision"] == 1
    assert result["core_settings"]["BTC"]["mode"] == "SPOT_SHORT"
    assert result["core_settings"]["ETH"]["horizon"] == "24h"
    assert result["core_settings"]["BTC"]["stable"] == "USDC"
    assert result["core_settings"]["ETH"]["stable"] == "USDT"
    assert len([q for q in c.queries if "UPDATE mininex_trade_preferences" in q[0]]) == 1
    assert not any("paper_sessions" in q[0] or "budget" in q[0] or "order" in q[0]
                   for q in c.queries)


def test_revision_conflict_must_not_change_btc_eth(monkeypatch):
    c = fake_database(monkeypatch)
    with pytest.raises(HTTPException) as e:
        profile.save_strategy_profile(
            DID, mode="SPOT_SHORT", horizon="1h", expected_revision=10)
    assert e.value.status_code == 409
    assert c.profile["revision"] == 0
    assert c.core["BTC"]["revision"] == 3
    assert c.core["ETH"]["revision"] == 5


@pytest.mark.parametrize("mode,horizon,rev", [
    ("LEVERAGED", "1h", 0), ("LONG", "2h", 0),
    ("LONG", "15m", True), ("LONG", "15m", -1),
])
def test_bad_modes_horizons_or_revisions_never_change_storage(monkeypatch,mode,horizon,rev):
    c = fake_database(monkeypatch)
    with pytest.raises(ValueError):
        profile.save_strategy_profile(DID, mode=mode, horizon=horizon, expected_revision=rev)
    assert c.queries == []


@pytest.fixture
def signed(monkeypatch):
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",public)
    def token(did=DID):
        now = int(time.time())
        raw = jwt.encode({
            "iss": "privy.io", "aud": "cmw7sb7ty013o0gl55aqyzi3y",
            "sub": did, "sid": "shared-profile", "iat": now, "exp": now + 600,
        }, private, algorithm="ES256")
        return {"Authorization": "Bearer " + raw}
    return token


def test_shared_profile_route_needs_login_and_only_uses_authenticated_identity(monkeypatch, signed):
    calls = []
    result = {
        "mode": "SHARED_STRATEGY_PREFERENCES", "strategy_mode": "LONG",
        "horizon": "15m", "revision": 0, "assets": list(profile.TRADING_ASSETS),
        "ada_in_progress": True, "independent_budgets_preserved": True,
        "existing_sessions_unchanged": True, "wallet_authorized": False,
        "real_trading_enabled": False,
    }
    monkeypatch.setattr(api, "get_strategy_profile",
                        lambda did: (calls.append(("read",did)) or result))
    monkeypatch.setattr(api, "save_strategy_profile",
                        lambda did,**kwargs: (calls.append(("write",did,kwargs)) or result))
    client = TestClient(api.app)
    assert client.get("/api/strategy/profile").status_code == 401
    assert client.put("/api/strategy/profile",
                      json={"mode":"LONG","horizon":"15m","expected_revision":0}).status_code == 401
    assert client.get("/api/strategy/profile",headers=signed()).status_code == 200
    response = client.put("/api/strategy/profile",headers=signed("did:privy:bob"),
                          json={"mode":"LONG","horizon":"5m","expected_revision":0})
    assert response.status_code == 200
    assert response.json()["real_trading_enabled"] is False
    assert calls == [
        ("read",DID),
        ("write","did:privy:bob",
         {"mode":"LONG","horizon":"5m","expected_revision":0}),
    ]
    for body in [
        {"mode":"LONG","horizon":"2h","expected_revision":0},
        {"mode":"LONG","horizon":"15m","expected_revision":"0"},
        {"mode":"SHORT","horizon":"15m","expected_revision":0},
        {"mode":"LONG","horizon":"15m","expected_revision":0,"budget":500},
    ]:
        assert client.put("/api/strategy/profile",headers=signed(),json=body).status_code == 422


def test_schema_retains_independent_coin_tables_and_one_strategy_row():
    sql = "\n".join(storage.SCHEMA)
    assert "mininex_unified_strategy_profiles" in sql
    assert "mininex_user_paper_sessions" in sql
    assert "mininex_extra_paper_sessions" in sql
    assert "mininex_budget_preferences" in sql
