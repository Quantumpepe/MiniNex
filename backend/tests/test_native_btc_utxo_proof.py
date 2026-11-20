"""No original BTC UTXO may be signed without actual outpoint evidence."""
import copy

import pytest
from fastapi import HTTPException

from backend import native_btc_utxo_proof as proof
from backend.tests.test_native_btc_thor_outputs import (
    SOURCE, SOURCE_SCRIPT, VAULT, VAULT_SCRIPT,
)

TXID = "ab" * 32
FUNDING_HEIGHT = 901234
TIP = 901237


def _fixtures():
    inputs = [{
        "txid": TXID, "vout": 1, "value_sats": 3500000,
        "script_pubkey_hex": SOURCE_SCRIPT,
    }]
    txs = {
        TXID: {
            "txid": TXID,
            "status": {
                "confirmed": True, "block_height": FUNDING_HEIGHT,
                "block_hash": "f0" * 32,
            },
            "vout": [
                {"value": 10000, "scriptpubkey": VAULT_SCRIPT,
                 "scriptpubkey_address": VAULT},
                {"value": 3500000, "scriptpubkey": SOURCE_SCRIPT,
                 "scriptpubkey_address": SOURCE},
            ],
        },
    }
    spends = {f"{TXID}:1": {"spent": False}}
    return inputs, txs, spends


def _inspect(inputs=None, txs=None, spends=None, owner=SOURCE, tip=TIP):
    orig_i, orig_t, orig_s = _fixtures()
    return proof.inspect_original_btc_utxo_set(
        owner=owner,
        inputs=orig_i if inputs is None else inputs,
        funding_txs=orig_t if txs is None else txs,
        outspends=orig_s if spends is None else spends,
        tip_height=tip,
    )


def test_original_funding_transaction_unspent_and_owner_script_proven():
    r = _inspect()
    assert r["mode"] == "BTC_ORIGINAL_OWNER_UTXO_ESPLORA_READ_ONLY"
    assert r["input_count"] == 1
    assert r["input_total_sats"] == "3500000"
    assert r["inputs"][0]["funding_confirmations"] == 4
    assert r["inputs"][0]["script_pubkey_hex"] == SOURCE_SCRIPT
    assert r["source_wallet"] == SOURCE
    assert r["funding_transactions_checked"] is True
    assert r["current_explorer_unspent_reported"] is True
    assert len(r["proof_sha256"]) == 64
    for flag in (
        "bitcoin_spv_proof_verified", "atomic_utxo_lock_acquired",
        "fresh_privy_signer_policy_verified", "original_order_claim_verified",
        "wallet_signed", "source_transaction_sent", "execution_enabled",
        "can_submit",
    ):
        assert r[flag] is False


@pytest.mark.parametrize("field,value", [
    ("value_sats", 3500001),
    ("value_sats", 3500000.0),
    ("script_pubkey_hex", VAULT_SCRIPT),
    ("script_pubkey_hex", "0014" + "33" * 20),
    ("script_pubkey_hex", None),
    ("txid", "ac" * 32),
    ("txid", "untrusted"),
    ("vout", 0),
    ("vout", -1),
])
def test_forged_original_input_claims_rejected(field, value):
    inputs, txs, spends = _fixtures()
    inputs[0][field] = value
    with pytest.raises(ValueError):
        _inspect(inputs=inputs, txs=txs, spends=spends)


@pytest.mark.parametrize("mutation", [
    lambda tx: tx["vout"][1].update(value=3500001),
    lambda tx: tx["vout"][1].update(scriptpubkey=VAULT_SCRIPT),
    lambda tx: tx["vout"][1].update(scriptpubkey_address=VAULT),
    lambda tx: tx["vout"].pop(),
    lambda tx: tx.update(txid="cd" * 32),
    lambda tx: tx["status"].update(confirmed=False),
    lambda tx: tx["status"].update(block_height=TIP + 1),
    lambda tx: tx["status"].update(block_hash="not-a-block"),
])
def test_forged_funding_evidence_rejected(mutation):
    inputs, txs, spends = _fixtures()
    mutation(txs[TXID])
    with pytest.raises(ValueError):
        _inspect(inputs=inputs, txs=txs, spends=spends)


@pytest.mark.parametrize("spends", [
    {f"{TXID}:1": {"spent": True, "txid": "cd" * 32}},
    {f"{TXID}:1": {"spent": "false"}},
    {f"{TXID}:1": {}},
    {},
])
def test_confirmed_but_already_spent_outpoint_is_not_usable(spends):
    with pytest.raises(ValueError):
        _inspect(spends=spends)


def test_duplicate_or_mixed_owner_utxos_rejected():
    inputs, txs, spends = _fixtures()
    with pytest.raises(ValueError):
        _inspect(inputs=inputs + [copy.deepcopy(inputs[0])])
    with pytest.raises(ValueError):
        _inspect(owner=VAULT)
    with pytest.raises(ValueError):
        _inspect(tip=0)


@pytest.mark.asyncio
async def test_read_only_preflight_rejects_bad_outpoints_before_rpc(monkeypatch):
    async def fake_user(_):
        return {"id": "did:privy:owner"}
    monkeypatch.setattr(proof, "fetch_privy_user", fake_user)
    monkeypatch.setattr(proof, "verified_addresses",
                        lambda _user, _did: {"BTC": SOURCE})
    i, _, _ = _fixtures()
    bad = i + [copy.deepcopy(i[0])]
    with pytest.raises(HTTPException) as exc:
        await proof.get_original_btc_utxo_evidence("did:privy:owner", inputs=bad)
    assert exc.value.status_code == 422


def test_read_only_utxo_inspector_never_signs_or_broadcasts():
    import inspect
    text = inspect.getsource(proof.inspect_original_btc_utxo_set)
    for denied in ("sendrawtransaction", "privy_sign", "private_key",
                   "seed_phrase", "psycopg", "request.post"):
        assert denied not in text
