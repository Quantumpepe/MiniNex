"""BTC original-owner preflight only builds, never signs or sends BTC."""
import asyncio
import copy
import hashlib

import pytest
from fastapi import HTTPException

from backend import native_btc_original_preflight as preflight
from backend import native_btc_utxo_proof as proof
from backend.tests.test_native_btc_thor_outputs import (
    quote, AMOUNT, NOW, SOURCE, VAULT, ETH, SOURCE_SCRIPT,
)
from backend.tests.test_native_btc_utxo_proof import _fixtures, TIP


@pytest.mark.parametrize(("rate", "value"), [
    ({"1": 1.01}, 2),
    ({"1": "2.000"}, 2),
    ({"1": 0.99}, 1),
    ({"1": 72}, 72),
])
def test_fresh_real_network_fee_estimate_ceil(rate, value):
    assert preflight.validate_original_btc_network_fee_estimate(rate) == value


@pytest.mark.parametrize("raw", [
    {}, [], None, {"1": 0}, {"1": -1}, {"1": "NaN"},
    {"1": "Infinity"}, {"1": "not-sats"}, {"1": float("inf")},
    {"1": 1_000_001}, {"1": False},
])
def test_unverified_miner_fee_cannot_build_btc_psbt(raw):
    with pytest.raises(ValueError):
        preflight.validate_original_btc_network_fee_estimate(raw)


def _proof():
    inputs, txs, spends = _fixtures()
    return proof.inspect_original_btc_utxo_set(
        owner=SOURCE, inputs=inputs,
        funding_txs=txs, outspends=spends, tip_height=TIP,
    )


def _run(monkeypatch, *, change_quote=None, change_proof=None, fee_rate=5,
         now=NOW):
    async def terms(*a, **kw):
        assert kw["asset"] == "BTC" and kw["side"] == "SELL"
        q = quote()
        if change_quote:
            change_quote(q)
        return q

    async def utxo(did, *, inputs):
        assert did == "did:privy:btc_test"
        evidence = _proof()
        if change_proof:
            change_proof(evidence)
        return evidence

    async def miner_fee():
        return fee_rate

    monkeypatch.setattr(preflight, "get_private_native_thornode_terms", terms)
    monkeypatch.setattr(preflight, "get_original_btc_utxo_evidence", utxo)
    monkeypatch.setattr(preflight, "_get_fresh_bitcoin_fee_rate", miner_fee)
    monkeypatch.setattr(preflight.time, "time", lambda: now)
    return asyncio.run(preflight.prepare_original_btc_unsigned_psbt(
        "did:privy:btc_test", stable="USDC", amount=AMOUNT,
        input_utxos=_fixtures()[0],
    ))


def test_real_owner_and_source_chain_and_fresh_quote_bound_psbt(monkeypatch):
    r = _run(monkeypatch)
    assert r["mode"] == "ORIGINAL_BTC_PSBT_OWNER_PREFLIGHT_READ_ONLY"
    assert r["source_wallet"] == SOURCE
    assert r["destination_wallet"] == ETH
    assert r["fee_rate_sats_per_vbyte"] == 5
    assert r["btc_utxo_tip_height"] == TIP
    assert r["current_esplora_unspent_reported"] is True
    unsigned = r["unsigned_psbt"]
    assert unsigned["source_wallet"] == SOURCE
    assert unsigned["inbound_vault"] == VAULT
    assert unsigned["source_amount_sats"] == 2500000
    assert unsigned["psbt_created"] is True
    assert unsigned["input_count"] == 1
    assert len(unsigned["psbt_base64"]) > 80
    assert r["original_preflight_sha256"] != unsigned["psbt_sha256"]
    for item in (
        "bitcoin_spv_proof_verified", "bitcoin_utxo_atomic_lock_acquired",
        "fresh_privy_owner_permission_granted", "original_order_send_claim_acquired",
        "wallet_signed", "source_transaction_sent", "execution_enabled",
        "can_submit",
    ):
        assert r[item] is False


@pytest.mark.parametrize("mutate", [
    lambda q: q.update(source_wallet=VAULT),
    lambda q: q.update(amount="0.01"),
    lambda q: q.update(original_vault_verified=False),
    lambda q: q.update(inbound_vault="bc1q" + "q" * 38),
    lambda q: q.update(destination_wallet=SOURCE),
    lambda q: q.update(quote_expires_at=quote()["quote_expires_at"].replace(year=2024)),
])
def test_original_private_quote_cannot_redirect_source_or_signer(monkeypatch, mutate):
    with pytest.raises(HTTPException) as exc:
        _run(monkeypatch, change_quote=mutate)
    assert exc.value.status_code == 409


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(source_wallet=VAULT),
    lambda p: p.update(current_explorer_unspent_reported=False),
    lambda p: p.update(funding_transactions_checked=False),
    lambda p: p.update(bitcoin_spv_proof_verified=True),
    lambda p: p.update(input_count=2),
])
def test_original_owner_utxo_evidence_must_agree(monkeypatch, mutate):
    with pytest.raises(HTTPException) as exc:
        _run(monkeypatch, change_proof=mutate)
    assert exc.value.status_code == 409


def test_expired_route_after_network_fetch_is_not_an_executable_psbt(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        _run(monkeypatch, now=NOW + 500)
    assert exc.value.status_code == 409


def test_original_psbt_can_auto_select_owner_inputs_without_manual_coin_control(monkeypatch):
    async def terms(*a, **kw):
        return quote()
    async def candidates(_did):
        rows = _fixtures()[0]
        return {
            "source_wallet": SOURCE,
            "candidates": rows,
            "confirmed_candidate_count": len(rows),
            "candidate_chain_provenance_checked": False,
        }
    async def utxo(_did, *, inputs):
        assert inputs == _fixtures()[0]
        return _proof()
    async def fee():
        return 5
    monkeypatch.setattr(preflight, "get_private_native_thornode_terms", terms)
    monkeypatch.setattr(preflight, "get_original_btc_utxo_candidates", candidates)
    monkeypatch.setattr(preflight, "get_original_btc_utxo_evidence", utxo)
    monkeypatch.setattr(preflight, "_get_fresh_bitcoin_fee_rate", fee)
    monkeypatch.setattr(preflight.time, "time", lambda: NOW)
    r = asyncio.run(preflight.prepare_original_btc_unsigned_psbt(
        "did:privy:btc_test", stable="USDC", amount=AMOUNT,
    ))
    assert r["automatic_input_selection_used"] is True
    assert len(r["unverified_selection_sha256"]) == 64
    assert r["unsigned_psbt"]["input_count"] == 1
    assert r["bitcoin_utxo_atomic_lock_acquired"] is False
    assert r["can_submit"] is False


def test_original_psbt_auto_selection_rejects_owner_change(monkeypatch):
    async def terms(*a, **kw):
        return quote()
    async def candidates(_did):
        return {"source_wallet": VAULT, "candidates": _fixtures()[0]}
    async def fee():
        return 5
    monkeypatch.setattr(preflight, "get_private_native_thornode_terms", terms)
    monkeypatch.setattr(preflight, "get_original_btc_utxo_candidates", candidates)
    monkeypatch.setattr(preflight, "_get_fresh_bitcoin_fee_rate", fee)
    monkeypatch.setattr(preflight.time, "time", lambda: NOW)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(preflight.prepare_original_btc_unsigned_psbt(
            "did:privy:btc_test", stable="USDC", amount=AMOUNT,
        ))
    assert exc.value.status_code == 409


def test_original_psbt_bundle_does_not_broadcast_or_grant_wallet_rights():
    import inspect
    source = inspect.getsource(preflight.prepare_original_btc_unsigned_psbt)
    for forbidden in ("send_raw_transaction", "sendrawtransaction",
                      "privy_sign", "private_key", "psycopg", "broadcast("):
        assert forbidden not in source
