"""Read-only Privy-bound per-chain explorer views, including Polygon WPOL."""
import asyncio

import pytest
from fastapi import HTTPException
from backend import chain_transaction_history as chains

ADDR = "0x" + "a" * 40
OUTSIDE = "0x" + "b" * 40
WRAPPED = "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270"
HASH = "0x" + "7" * 64


def test_polygon_native_wrap_is_a_token_conversion_not_a_usdc_sale():
    row = {
        "hash": HASH, "from": {"hash": ADDR}, "to": {"hash": WRAPPED},
        "value": "25000000000000000000", "method": "deposit",
        "status": "ok", "result": "success", "block_number": 97837614,
        "timestamp": "2026-11-23T19:24:50Z",
    }
    tx = chains.evm_transaction(row, ADDR, "POL")
    assert tx["type"] == "WRAP"
    assert tx["label"] == "Wrap POL → WPOL"
    assert tx["quantity"] == "25"
    assert tx["explorer_url"] == "https://polygonscan.com/tx/" + HASH
    assert not tx["tokens"]
    assert chains.evm_transaction({**row, "from": {"hash": OUTSIDE}}, ADDR, "POL") is None


def test_polygon_token_transfer_groups_native_wrap_and_ignores_spoof_tokens():
    row = {
        "tx_hash": HASH, "from": {"hash": "0x" + "0" * 40},
        "to": {"hash": ADDR}, "block_number": 97837614,
        "timestamp": "2026-11-23T19:24:50Z",
        "token": {"type": "ERC-20", "address_hash": WRAPPED},
        "total": {"value": "25000000000000000000", "decimals": "18"},
    }
    event = chains.evm_token_transfer(row, ADDR, "POL")
    assert event["asset"] == "WPOL" and event["direction"] == "IN"
    native = chains.evm_transaction({
        "hash": HASH, "from": {"hash": ADDR}, "to": {"hash": WRAPPED},
        "value": "25000000000000000000", "method": "deposit",
        "status": "ok", "block_number": 97837614,
        "timestamp": "2026-11-23T19:24:50Z",
    }, ADDR, "POL")
    merged = chains.combine_evm_events([native], [event], "POL")
    assert len(merged) == 1
    assert merged[0]["tokens"] == [
        {"asset": "WPOL", "quantity": "25", "direction": "IN"}
    ]
    assert chains.evm_token_transfer(
        {**row, "token": {**row["token"], "address_hash": OUTSIDE}},
        ADDR, "POL") is None
    assert chains.evm_token_transfer(
        {**row, "total": {"value": "25000000000000000000", "decimals": "6"}},
        ADDR, "POL") is None


def test_evm_contract_call_not_hidden_as_zero_value_native_transfer():
    tx = chains.evm_transaction({
        "hash": HASH, "from": {"hash": ADDR}, "to": {"hash": OUTSIDE},
        "value": "0", "method": "approve",
        "status": "ok", "block_number": 97837614,
        "timestamp": "2026-11-23T19:24:50Z",
    }, ADDR, "POL")
    assert tx["type"] == "CONTRACT"
    assert tx["label"] == "approve" and tx["quantity"] is None


def test_owner_private_chain_history_uses_server_privy_wallet(monkeypatch):
    identities = []
    async def owner(did):
        return {"id": did, "linked_accounts": [
            {"type": "wallet", "chain_type": "ethereum",
             "wallet_client_type": "privy", "address": ADDR},
        ]}
    async def explorer(client, chain, address):
        identities.append((chain, address))
        return [], "ready"
    monkeypatch.setattr(chains, "fetch_privy_user", owner)
    monkeypatch.setattr(chains, "_blockscout", explorer)
    a = asyncio.run(chains.read_chain_transactions("did:privy:alice", "POL"))
    assert a["wallet_ownership_verified"] is True
    assert a["preview_only"] is True and a["tax_complete"] is False
    assert a["history_complete"] is False
    assert a["explorer_address_url"] == "https://polygonscan.com/address/" + ADDR.lower()
    assert identities == [("POL", ADDR.lower())]
    with pytest.raises(HTTPException) as err:
        asyncio.run(chains.read_chain_transactions("did:privy:alice", "FAKE"))
    assert err.value.status_code == 404


def test_native_xrp_hashes_use_xrpl_and_never_assume_native_payment_for_token():
    raw = {"result": {"status": "success", "transactions": [{
        "tx_json": {
            "hash": "B" * 64, "Account": "rMyTestWallet",
            "Destination": "rOther", "TransactionType": "Payment",
            "Amount": "4250000", "date": 900000000,
        }, "validated": True, "meta": {"TransactionResult": "tesSUCCESS"},
    }]}}
    items = chains._read_xrp(raw, "rMyTestWallet")
    assert len(items) == 1
    assert items[0]["quantity"] == "4.25"
    assert items[0]["explorer_url"] == "https://xrpscan.com/tx/" + "B" * 64
    assert items[0]["confirmed"] is True


def test_wallet_missing_keeps_empty_history_without_leaking_another_user(monkeypatch):
    async def owner(did):
        return {"id": did, "linked_accounts": []}
    monkeypatch.setattr(chains, "fetch_privy_user", owner)
    result = asyncio.run(chains.read_chain_transactions("did:privy:alice", "POL"))
    assert result["source_status"] == "wallet_missing"
    assert result["items"] == []
    assert result["explorer_address_url"] is None
