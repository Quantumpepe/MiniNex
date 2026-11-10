"""BTC/ETH LIVE order audit + risk preflight: never sign or duplicate an order."""
from datetime import datetime, timezone
from decimal import Decimal
import time
import inspect

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import live_orders as orders
from backend.storage import SCHEMA

DID = "did:privy:live-order-audit"
TX_HASH = "0x" + "a" * 64


@pytest.fixture
def headers(monkeypatch):
    secret = ec.generate_private_key(ec.SECP256R1())
    public = secret.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY", public)
    now = int(time.time())
    token = jwt.encode({
        "iss": "privy.io", "aud": "cmw7sb7ty013o0gl55aqyzi3y",
        "sub": DID, "sid": "order-audit-session",
        "iat": now, "exp": now + 600,
    }, secret, algorithm="ES256")
    return {"Authorization": "Bearer " + token}


def sample_order(**extra):
    result = {
        "intent_key": "eth_signal_123456",
        "asset": "ETH", "side": "BUY", "stable": "USDC",
        "state": "RESERVED",
        "reserved_notional_usd": Decimal("80.000000"),
        "tx_hash": None, "submitted_at": None,
        "created_at": datetime(2026, 11, 10, tzinfo=timezone.utc),
    }
    result.update(extra)
    return result


def test_network_scoped_asset_and_unique_order_keys():
    assert orders.valid_asset("BTC") == "BTC"
    assert orders.valid_asset("ETH") == "ETH"
    for value in ("BNB", "SOL", "XRP", "", "btc", None, 22):
        with pytest.raises(HTTPException) as err:
            orders.valid_asset(value)
        assert err.value.status_code == 404
    assert orders.valid_order_key("eth_signal_123456") == "eth_signal_123456"
    for value in ("", "123", "spaces no", "../../wallet", None, True):
        with pytest.raises(ValueError):
            orders.valid_order_key(value)


def test_exact_micro_usd_no_float_rounding_or_invalid_values():
    assert orders.valid_notional("0.000001") == Decimal("0.000001")
    assert orders.valid_notional("500.000000") == Decimal("500.000000")
    assert orders.valid_notional("0.000000", allow_zero=True) == 0
    for raw in ("0", "0.000000", "-5", "1e9", "NaN", "Infinity",
                " 10", "10000000", "1.0000001", "", "01", True, None, 2.0):
        with pytest.raises(ValueError):
            orders.valid_notional(raw)


def test_transition_graph_does_not_retry_uncertain_broadcasts():
    orders.verify_transition("RESERVED", "SENDING")
    orders.verify_transition("RESERVED", "CANCELLED")
    orders.verify_transition("RESERVED", "EXPIRED")
    orders.verify_transition("SENDING", "RECONCILIATION_REQUIRED")
    orders.verify_transition("RECONCILIATION_REQUIRED", "SUBMITTED", tx_hash=TX_HASH)
    orders.verify_transition("SUBMITTED", "CONFIRMED", tx_hash=TX_HASH,
                             confirmed_receipt=True)
    orders.verify_transition("SUBMITTED", "REVERTED", tx_hash=TX_HASH,
                             confirmed_receipt=True)
    for src, dst in [
        ("RESERVED", "SUBMITTED"), ("RESERVED", "CONFIRMED"),
        ("SENDING", "SENDING"), ("SENDING", "CANCELLED"),
        ("RECONCILIATION_REQUIRED", "CANCELLED"),
        ("RECONCILIATION_REQUIRED", "SENDING"),
        ("SUBMITTED", "CANCELLED"), ("CONFIRMED", "SENDING"),
        ("REVERTED", "SUBMITTED"), ("CANCELLED", "SENDING"),
        ("EXPIRED", "RESERVED"),
    ]:
        with pytest.raises(ValueError):
            orders.verify_transition(src, dst, tx_hash=TX_HASH, confirmed_receipt=True)


def test_receipt_fraud_and_invalid_hashes_are_blocked():
    for tx in (None, "0x1234", "hash", 123, "0x"+"z"*64):
        with pytest.raises(ValueError):
            orders.verify_transition("SENDING", "SUBMITTED", tx_hash=tx)
    for dest in ("CONFIRMED", "REVERTED"):
        with pytest.raises(ValueError):
            orders.verify_transition("SUBMITTED", dest, tx_hash=TX_HASH)
    assert orders.LIVE_RESERVATION_ACTIVATED is False
    with pytest.raises(HTTPException) as err:
        orders.prepare_live_order_reservation()
    assert err.value.status_code == 503


def test_order_api_never_labels_uncertain_pending_as_final():
    prepared = orders.serialize_order(sample_order())
    assert prepared["status"] == "RESERVED"
    assert prepared["final"] is False
    assert prepared["uncertain"] is False
    assert prepared["tx_hash"] is None
    transmitting = orders.serialize_order(sample_order(state="SENDING"))
    assert transmitting["uncertain"] is True
    assert transmitting["final"] is False
    sent = orders.serialize_order(sample_order(
        state="SUBMITTED", tx_hash=TX_HASH,
        submitted_at=datetime(2026, 11, 10, 10, tzinfo=timezone.utc),
    ))
    assert sent["final"] is False
    assert sent["tx_hash"] == TX_HASH
    completed = orders.serialize_order(sample_order(state="CONFIRMED", tx_hash=TX_HASH))
    assert completed["final"] is True
    for patch in (
        {"asset": "DOGE"}, {"state": "MAGIC"}, {"reserved_notional_usd": 0},
        {"created_at": datetime(2026, 11, 10)},
    ):
        with pytest.raises((ValueError, HTTPException)):
            orders.serialize_order(sample_order(**patch))


def test_rollover_exposure_includes_pending_without_underestimating_24h():
    budget = {
        "planned_capital_usd": "200",
        "max_order_usd": "75",
        "max_daily_turnover_usd": "180",
        "trading_paused": False,
        "privy_grant_verified": True,
    }
    assert orders.build_risk_snapshot(
        budget, unfilled_total_usd="25.000000",
        confirmed_24h_usd="75", proposed_usd="50") == {
            "proposed_usd": "50", "outstanding_usd": "25.000000",
            "rolling_24h_including_pending_usd": "100.000000",
        }
    for pending, settled, proposed in (
        ("195", "0", "25"), ("0", "160", "25"),
        ("0", "0", "76"), ("0", "0", "10000000"),
    ):
        with pytest.raises((HTTPException, ValueError)):
            orders.build_risk_snapshot(
                budget, unfilled_total_usd=pending,
                confirmed_24h_usd=settled, proposed_usd=proposed)
    for gate in (
        {"trading_paused": True}, {"privy_grant_verified": False},
        {"max_order_usd": "0"}, {"planned_capital_usd": "0"},
    ):
        with pytest.raises(HTTPException):
            orders.build_risk_snapshot(
                {**budget, **gate}, unfilled_total_usd="0",
                confirmed_24h_usd="0", proposed_usd="10")


def test_live_orders_sql_constraints_are_strict_and_separate_from_paper():
    sql = "\n".join(SCHEMA)
    assert "CREATE TABLE IF NOT EXISTS mininex_live_order_intents" in sql
    assert "PRIMARY KEY (privy_did,asset,intent_key)" in sql
    assert "state NOT IN ('SUBMITTED','CONFIRMED','REVERTED','REORG_PENDING')" in sql
    assert "mininex_user_paper_sessions" in sql
    assert "mininex_live_eth_limits" in sql
    # Fresh PostgreSQL bootstrap must create referenced tables before the FK.
    assert sql.index("CREATE TABLE IF NOT EXISTS mininex_fee_events") < sql.index(
        "CREATE TABLE IF NOT EXISTS mininex_treasury_obligations")

    assert "SEND" not in inspect.getsource(api.live_order_history).upper().split("def live_order_history")[0]


def test_user_can_only_read_own_orders_no_client_submit(monkeypatch, headers):
    seen=[]
    def history(did, *, asset, limit=30):
        seen.append((did,asset,limit))
        return {
            "mode": "LIVE_ORDER_AUDIT_ONLY",
            "asset": asset,
            "orders": [orders.serialize_order(sample_order(asset=asset))],
            "execution_enabled": False,
            "automatic_signing_enabled": False,
            "can_submit": False,
        }
    monkeypatch.setattr(api, "get_live_order_history", history)
    client=TestClient(api.app)
    assert client.get("/api/live/orders/ETH").status_code == 401
    assert client.get("/api/live/orders/BTC", headers={"Authorization":"Bearer invalid"}).status_code == 401
    response=client.get("/api/live/orders/ETH?limit=12",headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["can_submit"] is False
    assert response.json()["orders"][0]["status"] == "RESERVED"
    assert seen == [(DID,"ETH",12)]
    assert client.post("/api/live/orders/ETH", headers=headers, json={}).status_code == 405
    assert client.get("/api/live/orders/ETH?limit=bad",headers=headers).status_code == 422
    assert not any(route.path in ("/api/live/orders/prepare",
                                  "/api/live/orders/execute",
                                  "/api/live/orders/send") for route in api.app.routes)
