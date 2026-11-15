"""A stale/missing Privy V2 quorum prevents any ETH AUTO durable send claim."""
import asyncio

import httpx
import pytest
from fastapi import HTTPException

from backend import eth_auto_order_submit as swap
from backend import eth_auto_native_cycle as native

DID = "did:privy:quorum-test-user"
WALLET = "0x" + "a" * 40
WALLET_ID = "wallet_q_test_123"
KEY = "sig_abcdef01234567890123456789"


def _identity(monkeypatch, module):
    monkeypatch.setattr(module, "signer_configuration",
                        lambda did: {"signer_id": "signer123456", "policy_id": "policy123456"})
    async def user(did):
        return {"id": did}
    async def metadata(wallet_id):
        return {"id": wallet_id}
    monkeypatch.setattr(module, "fetch_privy_user", user)
    monkeypatch.setattr(module, "fetch_privy_wallet_metadata", metadata)
    monkeypatch.setattr(module, "_owned_eth_wallet",
                        lambda user, did: {"wallet_id": WALLET_ID, "address": WALLET, "delegated": True})
    monkeypatch.setattr(module, "_scoped_signer", lambda *a, **kw: True)


@pytest.mark.parametrize("review", [
    {}, {"remote_quorum_public_key_verified": False},
    {"remote_quorum_get_succeeded": True},
])
def test_eth_swap_never_claims_when_registered_quorum_is_unverified(monkeypatch, review):
    _identity(monkeypatch, swap)
    async def remote():
        return review
    monkeypatch.setattr(swap, "check_privy_backend_quorum", remote)
    monkeypatch.setattr(swap, "_request_digest",
                        lambda *a, **kw: pytest.fail("No order digest before remote quorum check"))
    monkeypatch.setattr(swap, "_db_url",
                        lambda: pytest.fail("No DB reservation on quorum mismatch"))
    async def action():
        async with httpx.AsyncClient() as client:
            return await swap.submit_privy_eth_auto_order(
                did=DID, intent_key=KEY, tx={},
                side="BUY", stable="USDC", input_atomic="100",
                minimum_output_atomic="100", client=client,
            )
    with pytest.raises(HTTPException) as err:
        asyncio.run(action())
    assert err.value.status_code == 503


@pytest.mark.parametrize("review", [
    {}, {"remote_quorum_public_key_verified": False},
    {"remote_quorum_get_succeeded": True},
])
def test_eth_native_never_claims_when_registered_quorum_unverified(monkeypatch, review):
    _identity(monkeypatch, native)
    async def remote():
        return review
    monkeypatch.setattr(native, "check_privy_backend_quorum", remote)
    monkeypatch.setattr(native, "_db_url",
                        lambda: pytest.fail("No native DB claim on quorum mismatch"))
    monkeypatch.setattr(native, "get_wallet_balances",
                        lambda *args: pytest.fail("No chain read after quorum mismatch"))
    async def action():
        async with httpx.AsyncClient() as client:
            return await native.send_eth_auto_native_leg(
                DID, source_key=KEY, operation="DEPOSIT",
                amount_wei=10**16, stable="USDC", client=client,
            )
    with pytest.raises(HTTPException) as err:
        asyncio.run(action())
    assert err.value.status_code == 503
