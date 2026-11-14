"""Owner-authenticated, strictly read-only native ETH cycle API tests."""
import asyncio
from copy import deepcopy

import pytest
from fastapi import HTTPException

from backend import app as api

DID = "did:privy:only-owner"
A = "0x" + "a" * 64
B = "0x" + "b" * 64


def evidence(side="BUY", stable="USDC"):
    return ({
        "asset": "ETH", "network": "ethereum-mainnet", "tx_hash": A,
        "side": side, "stable": stable,
        "swap_output_asset": "WETH" if side == "BUY" else stable,
        "state": "FINALIZED_ERC20_SWAP_LEG",
        "transaction_finalized": True, "swap_erc20_leg_verified": True,
        "source_spent_amount": "100" if side == "BUY" else "0.1",
        "destination_received_amount": "0.1" if side == "BUY" else "100",
        "network_fee_eth": "0.0001", "execution_enabled": False,
        "can_submit": False,
    }, {
        "asset": "ETH", "network": "ethereum-mainnet", "tx_hash": B,
        "operation": "WITHDRAW" if side == "BUY" else "DEPOSIT",
        "converted_asset": "ETH" if side == "BUY" else "WETH",
        "state": "FINALIZED_WETH9_CONVERSION",
        "transaction_finalized": True, "confirmed_native_conversion": True,
        "weth9_event_amount": "0.1", "network_fee_eth": "0.0002",
        "execution_enabled": False, "can_submit": False,
    })


@pytest.mark.parametrize("side,stable", [
    ("BUY", "USDC"), ("BUY", "USDT"),
    ("SELL", "USDC"), ("SELL", "USDT"),
])
def test_route_checks_both_receipts_under_same_owner(monkeypatch, side, stable):
    swap, conversion = evidence(side, stable)
    calls = []
    monkeypatch.setattr(api, "verify_privy_bearer",
                        lambda token: {"user_id": DID})

    async def read_swap(did, tx, *, side, stable):
        calls.append(("swap", did, tx))
        return deepcopy(swap)

    async def read_weth(did, tx, *, operation):
        calls.append(("weth", did, tx, operation))
        return deepcopy(conversion)

    monkeypatch.setattr(api, "check_user_router02_receipt", read_swap)
    monkeypatch.setattr(api, "check_user_weth9_receipt", read_weth)
    result = asyncio.run(api.eth_compare_verified_native_cycle(
        swap_tx_hash=A, conversion_tx_hash=B, side=side, stable=stable,
        authorization="Bearer placeholder"))
    assert result.status_code == 200
    assert result.headers["cache-control"] == "private, no-store"
    assert calls == [
        ("swap", DID, A),
        ("weth", DID, B, "WITHDRAW" if side == "BUY" else "DEPOSIT"),
    ]
    assert b'"ledger_settled":false' in result.body
    assert b'"can_submit":false' in result.body


def test_same_hash_is_rejected_before_chain_queries(monkeypatch):
    monkeypatch.setattr(api, "verify_privy_bearer",
                        lambda token: {"user_id": DID})
    with pytest.raises(HTTPException) as failure:
        asyncio.run(api.eth_compare_verified_native_cycle(
            swap_tx_hash=A, conversion_tx_hash=A,
            side="BUY", stable="USDC", authorization="Bearer placeholder"))
    assert failure.value.status_code == 422


def test_auth_failure_stops_receipt_checks(monkeypatch):
    def deny(_):
        raise HTTPException(401, "Unauthorized")
    monkeypatch.setattr(api, "verify_privy_bearer", deny)
    with pytest.raises(HTTPException) as failure:
        asyncio.run(api.eth_compare_verified_native_cycle(
            swap_tx_hash=A, conversion_tx_hash=B, side="BUY",
            stable="USDC", authorization=None))
    assert failure.value.status_code == 401


def test_failed_proof_never_returns_success(monkeypatch):
    swap, conversion = evidence()
    conversion["weth9_event_amount"] = "0.2"
    monkeypatch.setattr(api, "verify_privy_bearer",
                        lambda token: {"user_id": DID})

    async def read_swap(did, tx, *, side, stable):
        return swap

    async def read_weth(did, tx, *, operation):
        return conversion

    monkeypatch.setattr(api, "check_user_router02_receipt", read_swap)
    monkeypatch.setattr(api, "check_user_weth9_receipt", read_weth)
    with pytest.raises(HTTPException) as failure:
        asyncio.run(api.eth_compare_verified_native_cycle(
            swap_tx_hash=A, conversion_tx_hash=B, side="BUY",
            stable="USDC", authorization="Bearer placeholder"))
    assert failure.value.status_code == 409
