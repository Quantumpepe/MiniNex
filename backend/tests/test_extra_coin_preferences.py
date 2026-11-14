"""Account-persistent nine-coin settings: independent AUTO, budget and stable."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api, storage
from backend import extra_coin_preferences as cp

DID = "did:privy:alice"
OTHER = "did:privy:bob"


class FakeDB:
    def __init__(self):
        self.settings = {}
        self.current = []
        self.queries = []
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def cursor(self, **kwargs): return self
    def fetchall(self): return list(self.current)
    def fetchone(self): return self.current[0] if self.current else None
    def execute(self, sql, params=()):
        self.queries.append((sql, params))
        if "SELECT asset,budget_usd,mode,stable,horizon,revision" in sql:
            did = params[0]
            self.current = [dict(row, asset=asset) for (owner, asset), row
                            in self.settings.items() if owner == did]
        elif "INSERT INTO mininex_extra_coin_preferences" in sql:
            did, asset, budget, mode, stable, horizon = params
            key = (did, asset)
            self.current = []
            if key not in self.settings:
                self.settings[key] = dict(
                    budget_usd=budget, mode=mode, stable=stable,
                    horizon=horizon, revision=1)
                self.current = [{"revision": 1}]
        elif "UPDATE mininex_extra_coin_preferences" in sql:
            budget, mode, stable, horizon, did, asset, revision = params
            key = (did, asset)
            self.current = []
            if key in self.settings and self.settings[key]["revision"] == revision:
                self.settings[key].update(
                    budget_usd=budget, mode=mode, stable=stable,
                    horizon=horizon, revision=revision + 1)
                self.current = [{"revision": revision + 1}]
        else:
            raise AssertionError("Unexpected query: " + sql[:90])


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(cp, "_db_url", lambda: "postgresql://fake")
    monkeypatch.setattr(cp, "_ensure_schema", lambda url: None)
    monkeypatch.setattr(cp, "_prepare_rows", lambda cur, did: None)
    monkeypatch.setattr(cp.psycopg, "connect", lambda *a, **kw: fake)
    return fake


@pytest.fixture
def signed(monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY", pub)
    def make(did=DID):
        now = int(time.time())
        return {"Authorization": "Bearer " + jwt.encode({
            "iss": "privy.io", "aud": "cmw7sb7ty013o0gl55aqyzi3y",
            "sub": did, "sid": "coin-preferences",
            "iat": now, "exp": now+600,
        }, key, algorithm="ES256")}
    return make


def test_defaults_for_nine_coins_without_creating_sessions(db):
    value = cp.get_coin_preferences(DID)
    assert value["assets"] == list(cp.COINS)
    assert len(value["settings"]) == 9
    assert all(row == cp.DEFAULT for row in value["settings"].values())
    assert "ADA" not in value["settings"]
    assert value["live_trading_enabled"] is False
    assert value["wallet_authorized"] is False
    assert not any("paper_sessions" in sql for sql, _ in db.queries)


@pytest.mark.parametrize("coin", cp.COINS)
def test_each_coin_can_save_its_own_auto_budget_and_horizon(db, coin):
    result = cp.save_coin_preference(
        DID, coin, budget_usd=2400, mode="AUTO",
        stable="USDT", horizon="1h", expected_revision=0)
    assert result["settings"][coin] == {
        "budget_usd": 2400, "mode": "AUTO",
        "stable": "USDT", "horizon": "1h", "revision": 1}
    assert sum(row["revision"] != 0 for row in result["settings"].values()) == 1
    assert cp.get_coin_preferences(OTHER)["settings"][coin]["revision"] == 0


def test_changes_persist_with_version_and_no_cross_asset_updates(db):
    first = cp.save_coin_preference(
        DID, "SOL", budget_usd=700, mode="LONG", stable="USDC",
        horizon="15m", expected_revision=0)
    second = cp.save_coin_preference(
        DID, "BNB", budget_usd=900, mode="SPOT_SHORT", stable="USDT",
        horizon="5m", expected_revision=0)
    assert first["settings"]["SOL"]["revision"] == 1
    assert second["settings"]["BNB"]["revision"] == 1
    result = cp.save_coin_preference(
        DID, "SOL", budget_usd=5000, mode="AUTO", stable="USDT",
        horizon="24h", expected_revision=1)
    assert result["settings"]["SOL"]["revision"] == 2
    assert result["settings"]["BNB"]["budget_usd"] == 900
    with pytest.raises(HTTPException) as err:
        cp.save_coin_preference(
            DID, "SOL", budget_usd=100, mode="LONG", stable="USDC",
            horizon="15m", expected_revision=1)
    assert err.value.status_code == 409
    assert cp.get_coin_preferences(DID)["settings"]["SOL"]["budget_usd"] == 5000


@pytest.mark.parametrize("patch", [
    {"budget_usd": 0}, {"budget_usd": 5001}, {"budget_usd": True},
    {"mode": "LEVERAGED_SHORT"}, {"stable": "DAI"}, {"horizon": "2h"},
    {"expected_revision": -1}, {"expected_revision": "0"},
])
def test_invalid_changes_rejected_before_database(db, patch):
    args = dict(budget_usd=500, mode="LONG", stable="USDC",
                horizon="15m", expected_revision=0)
    args.update(patch)
    with pytest.raises(ValueError):
        cp.save_coin_preference(DID, "UNI", **args)
    assert db.queries == []


def test_unknown_coin_never_has_a_saved_row(db):
    with pytest.raises(HTTPException) as err:
        cp.save_coin_preference(
            DID, "ADA", budget_usd=100, mode="AUTO", stable="USDC",
            horizon="15m", expected_revision=0)
    assert err.value.status_code == 404
    assert not db.queries


def test_routes_require_privy_login_and_scope_writes_by_did(db, signed):
    c = TestClient(api.app)
    url = "/api/coin-preferences"
    assert c.get(url).status_code == 401
    auth_body = {"budget_usd": 500, "mode": "LONG", "stable": "USDC",
                 "horizon": "15m", "expected_revision": 0}
    assert c.put(url+"/SOL", json=auth_body).status_code == 401
    read = c.get(url, headers=signed(DID))
    assert read.status_code == 200
    assert read.headers["cache-control"] == "private, no-store"
    body = {"budget_usd": 1300, "mode": "AUTO", "stable": "USDT",
            "horizon": "24h", "expected_revision": 0}
    resp = c.put(url+"/SOL", json=body, headers=signed(OTHER))
    assert resp.status_code == 200
    assert cp.get_coin_preferences(OTHER)["settings"]["SOL"]["budget_usd"] == 1300
    assert cp.get_coin_preferences(DID)["settings"]["SOL"]["budget_usd"] == 500
    assert c.put(url+"/ADA", json=body, headers=signed()).status_code == 404
    assert c.put(url+"/SOL", json={**body, "sign": True},
                 headers=signed()).status_code == 422


def test_schema_creates_independent_settings_without_modifying_existing_sessions():
    schema = "\n".join(storage.SCHEMA)
    assert "CREATE TABLE IF NOT EXISTS mininex_extra_coin_preferences" in schema
    assert "PRIMARY KEY (privy_did,asset)" in schema
    assert "mininex_extra_paper_sessions" in schema
    assert "mininex_user_paper_sessions" in schema
