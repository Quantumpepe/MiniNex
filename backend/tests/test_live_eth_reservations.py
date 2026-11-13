"""Internal-only ETH atomic LIVE reservation contract: no actual orders or Privy API.

The private transaction core is tested with a simulated SQL cursor. The
public wrapper is always disabled and there are no exposed write endpoints.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import live_eth_reservations as claims
from backend import live_orders


DID = "did:privy:test-reservation"
OTHER_DID = "did:privy:other-reservation"
STRATEGY = "strategy_session_20261113"
SIGNAL = "decision_000001"
QUOTE = "a" * 64
PROOF = {
    "wallet_owner_verified": True,
    "signer_and_restricted_policy_verified": True,
    "router_calldata_policy_verified": True,
    "revocable_user_consent_verified": True,
}


class MockTransaction:
    """Mock the *shape* of one SQL transaction and row locking, not Postgres."""
    def __init__(self, *, paused=False, cash="500", order_cap="150",
                 turnover="350", pending="0", confirmed="0"):
        self.controls = {
            "planned_capital_usd": Decimal(cash),
            "max_order_usd": Decimal(order_cap),
            "max_daily_turnover_usd": Decimal(turnover),
            "trading_paused": paused,
            "revision": 7,
        }
        self.pending = Decimal(pending)
        self.confirmed = Decimal(confirmed)
        self.bindings = {}
        self.orders = {}
        self.queries = []
        self._result = None
        self.fail_binding_write = False

    def execute(self, query, params):
        text = " ".join(query.split())
        self.queries.append((text, params))
        if "FROM mininex_live_eth_limits" in text:
            assert "FOR UPDATE" in text
            assert params == (DID,)
            self._result = deepcopy(self.controls)
        elif "FROM mininex_live_signal_bindings" in text:
            did, asset, digest = params
            self._result = deepcopy(self.bindings.get((did, asset, digest)))
        elif "FROM mininex_live_order_intents" in text:
            assert "LIMIT" not in text
            assert params[1:] == (DID, "ETH")
            self._result = {
                "pending_usd": self.pending,
                "confirmed_24h_usd": self.confirmed,
            }
        elif text.startswith("INSERT INTO mininex_live_order_intents"):
            did, intent, side, stable, amount = params
            key = (did, "ETH", intent)
            assert key not in self.orders
            self.orders[key] = {
                "side": side, "stable": stable, "amount": amount,
            }
        elif text.startswith("INSERT INTO mininex_live_signal_bindings"):
            if self.fail_binding_write:
                raise RuntimeError("Injected binding write error")
            did, digest, intent, payload = params
            key = (did, "ETH", digest)
            assert key not in self.bindings
            self.bindings[key] = {
                "signal_digest": digest, "intent_key": intent,
                "payload_digest": payload,
            }
        else:
            raise AssertionError("Unexpected reservation SQL: " + text[:120])

    def fetchone(self):
        return self._result

    def transaction(self, action):
        """Simulated commit/rollback of both writes, never onchain funding."""
        before = (deepcopy(self.orders), deepcopy(self.bindings))
        try:
            return action()
        except Exception:
            self.orders, self.bindings = before
            raise


def attempt(cur, *, did=DID, signal=SIGNAL, amount="100", quote=QUOTE,
            proof=PROOF):
    return claims._reserve_under_eth_limits_lock(
        cur, did=did, strategy_run_id=STRATEGY,
        signal_event_id=signal, side="BUY", stable="USDC",
        proposed_usd=amount, quote_digest=quote,
        privy_execution_proof=proof,
    )


def test_public_reservation_entrypoints_stay_disabled():
    assert live_orders.LIVE_RESERVATION_ACTIVATED is False
    for entrypoint in (claims.prepare_live_eth_reservation,
                       live_orders.prepare_live_order_reservation):
        with pytest.raises(HTTPException) as err:
            entrypoint(
                wallet="0x" + "a" * 40, trusted=True,
                did=DID, amount="100",
            )
        assert err.value.status_code == 503
    paths = {route.path for route in api.app.routes}
    assert "/api/live/eth/reserve" not in paths
    assert "/api/live/eth/execute" not in paths
    assert "/api/live/eth/sign-and-send" not in paths
    assert "/api/live/orders/prepare" not in paths
    client = TestClient(api.app)
    assert client.post("/api/live/eth/reserve", json={}).status_code == 404


@pytest.mark.parametrize("invalid", [
    None,
    {},
    {"wallet_owner_verified": True},
    {**PROOF, "wallet_owner_verified": False},
    {**PROOF, "router_calldata_policy_verified": "true"},
    {**PROOF, "revocable_user_consent_verified": False},
])
def test_missing_or_forged_looking_privy_metadata_cannot_claim_anything(invalid):
    db = MockTransaction()
    with pytest.raises(HTTPException) as err:
        attempt(db, proof=invalid)
    assert err.value.status_code == 409
    assert db.queries == []
    assert not db.orders
    assert not db.bindings


def test_stop_switch_is_locked_and_never_creates_a_reservation():
    db = MockTransaction(paused=True)
    with pytest.raises(HTTPException) as err:
        attempt(db)
    assert err.value.status_code == 409
    assert len(db.queries) == 1
    assert "FOR UPDATE" in db.queries[0][0]
    assert db.bindings == {}
    assert db.orders == {}


def test_one_strategist_signal_claims_once_and_replays_never_resend():
    db = MockTransaction()
    first = db.transaction(lambda: attempt(db))
    assert first["claim"] == "RESERVED_NO_SEND"
    assert first["safe_to_send"] is False
    assert first["notional_usd"] == "100"
    assert len(db.orders) == len(db.bindings) == 1
    statements = [sql for sql, _ in db.queries]
    assert "FOR UPDATE" in statements[0]
    assert "FROM mininex_live_signal_bindings" in statements[1]
    assert "FROM mininex_live_order_intents" in statements[2]
    assert statements[3].startswith("INSERT INTO mininex_live_order_intents")
    assert statements[4].startswith("INSERT INTO mininex_live_signal_bindings")
    assert all("ON CONFLICT" not in sql for sql in statements)

    count = len(db.queries)
    retry = db.transaction(lambda: attempt(db))
    assert retry["claim"] == "DUPLICATE_NO_NEW_ORDER"
    assert retry["safe_to_send"] is False
    assert len(db.orders) == len(db.bindings) == 1
    # No new notional aggregation or write for a duplicate.
    assert len(db.queries) == count + 2

    # Replaying one event with altered quote or amount is an integrity error.
    for changed in (
        {"quote": "b" * 64},
        {"amount": "101"},
    ):
        with pytest.raises(ValueError):
            db.transaction(lambda changed=changed: attempt(db, **changed))
    assert len(db.orders) == len(db.bindings) == 1


@pytest.mark.parametrize("pending,confirmed,amount", [
    ("410", "0", "100"),
    ("0", "290", "100"),
    ("250", "10", "100"),
    ("0", "0", "151"),
])
def test_pending_capital_and_daily_turnover_reject_overspend(
    pending, confirmed, amount,
):
    db = MockTransaction(pending=pending, confirmed=confirmed)
    with pytest.raises(HTTPException):
        db.transaction(lambda: attempt(db, amount=amount))
    assert not db.orders
    assert not db.bindings


def test_sql_transaction_rollback_prevents_half_binding_after_failure():
    db = MockTransaction()
    db.fail_binding_write = True
    with pytest.raises(RuntimeError):
        db.transaction(lambda: attempt(db))
    assert not db.orders
    assert not db.bindings


def test_invalid_owner_and_wrong_notional_reject_before_any_write():
    for did, amount in ((OTHER_DID, "100"), (DID, "100.0000001"),
                        (DID, "NaN"), (DID, "-50")):
        db = MockTransaction()
        with pytest.raises((ValueError, HTTPException, AssertionError)):
            db.transaction(lambda: attempt(db, did=did, amount=amount))
        assert not db.orders
        assert not db.bindings
