"""Actual authenticated EVM mainnet holdings for five MiniNex assets."""
import asyncio
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient
from fastapi import HTTPException

from backend import app as api
from backend import multicoin_evm_balances as evm

DID = "did:privy:multi-evm"
ADDR = "0x" + "a" * 40
WEI = 10**18


def owner(address=ADDR, *, client="privy"):
    return {"id": DID, "linked_accounts": [{
        "type": "wallet", "chain_type": "ethereum",
        "wallet_client_type": client, "address": address,
    }]}


class Reply:
    def __init__(self, value):
        self.value = value
        self.content = b'{"jsonrpc":"2.0"}'
    def raise_for_status(self):
        pass
    def json(self):
        return self.value


def rpc_reply(req, *, bad_chain=None, malformed_tokens=False):
    url = req["url"]
    network = next(
        chain for chain, (_, _, fallback, _) in evm._RPC.items() if fallback == url)
    chain_id = evm._RPC[network][0]
    json = req["json"]
    method, rid = json["method"], json["id"]
    if method == "eth_chainId":
        result = hex(bad_chain) if bad_chain is not None else hex(chain_id)
    elif method == "eth_getBalance":
        result = hex({"BNB": 3, "POL": 4, "AVAX": 5}[network] * WEI)
    elif method == "eth_call":
        # LINK and UNI have 18 decimals; the response is a full 32-byte ABI word.
        result = "0x" + format((7 if rid == 3 else 9) * WEI, "064x")
        if malformed_tokens:
            result = "0x123"
    else:
        raise AssertionError("Unexpected RPC method")
    return Reply({"jsonrpc": "2.0", "id": rid, "result": result})


@pytest.fixture
def rpc_client(monkeypatch):
    requests = []
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, **kwargs):
            record = {"url": url, **kwargs}
            requests.append(record)
            return rpc_reply(record)
    monkeypatch.setattr(evm.httpx, "AsyncClient", Client)
    return requests


def test_five_real_onchain_balances_and_token_addresses(monkeypatch, rpc_client):
    async def fetch(did):
        assert did == DID
        return owner()
    monkeypatch.setattr(evm, "fetch_privy_user", fetch)
    result = asyncio.run(evm.get_multicoin_evm_balances(DID))
    assert result["asset_order"] == ["BNB", "POL", "AVAX", "LINK", "UNI"]
    assert [result["assets"][k]["balance"] for k in result["asset_order"]] == [
        "3", "4", "5", "7", "9",
    ]
    assert all(result["assets"][k]["status"] == "verified" for k in result["asset_order"])
    assert result["real_trading_enabled"] is False
    assert result["wallet_funds_used_for_paper"] is False
    chain_checks = [req for req in rpc_client if req["json"]["method"] == "eth_chainId"]
    assert len(chain_checks) == 4
    token_calls = [req for req in rpc_client if req["json"]["method"] == "eth_call"]
    assert len(token_calls) == 2
    assert {call["json"]["params"][0]["to"] for call in token_calls} == set(evm._ETH_TOKENS.values())
    assert all(call["json"]["params"][0]["data"].startswith("0x70a08231") for call in token_calls)
    assert all(ADDR.lower()[2:] in call["json"]["params"][0]["data"] for call in token_calls)


def test_bad_provider_chain_only_removes_that_coin(monkeypatch, rpc_client):
    async def fetch(did):
        return owner()
    monkeypatch.setattr(evm, "fetch_privy_user", fetch)
    original = evm._rpc
    async def mismatch(client, url, method, params, rid):
        if "bsc" in url and method == "eth_chainId":
            return 1
        return await original(client, url, method, params, rid)
    monkeypatch.setattr(evm, "_rpc", mismatch)
    result = asyncio.run(evm.get_multicoin_evm_balances(DID))
    assert result["assets"]["BNB"]["status"] == "unavailable"
    assert result["assets"]["BNB"]["balance"] is None
    assert result["assets"]["POL"]["balance"] == "4"
    assert result["assets"]["LINK"]["balance"] == "7"


def test_invalid_erc20_response_is_unavailable_not_zero(monkeypatch, rpc_client):
    async def fetch(did):
        return owner()
    monkeypatch.setattr(evm, "fetch_privy_user", fetch)
    original = evm._rpc
    async def fail_token(client, url, method, params, rid):
        if method == "eth_call" and rid == 3:
            raise ValueError("Malformed ERC-20 result")
        return await original(client, url, method, params, rid)
    monkeypatch.setattr(evm, "_rpc", fail_token)
    result = asyncio.run(evm.get_multicoin_evm_balances(DID))
    assert result["assets"]["LINK"]["status"] == "unavailable"
    assert result["assets"]["LINK"]["balance"] is None
    assert result["assets"]["UNI"]["balance"] == "9"


def test_wrong_or_external_owner_cannot_read_a_third_party_balance(monkeypatch):
    async def external(did):
        return owner(client="metamask")
    monkeypatch.setattr(evm, "fetch_privy_user", external)
    with pytest.raises(HTTPException) as info:
        asyncio.run(evm.get_multicoin_evm_balances(DID))
    assert info.value.status_code == 503


@pytest.fixture
def authorization(monkeypatch):
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY", public)
    now = int(time.time())
    token = jwt.encode({
        "iss": "privy.io", "aud": "cmw7sb7ty013o0gl55aqyzi3y",
        "sub": DID, "sid": "evm-real-balance", "iat": now, "exp": now + 600,
    }, private, algorithm="ES256")
    return {"Authorization": "Bearer " + token}


def test_endpoint_requires_user_login_and_is_readonly(monkeypatch, authorization):
    seen = []
    async def fake(did):
        seen.append(did)
        return {"mode": "REAL_MAINNET_BALANCES_READ_ONLY", "real_trading_enabled": False}
    monkeypatch.setattr(api, "get_multicoin_evm_balances", fake)
    c = TestClient(api.app)
    path = "/api/wallet/multicoin-balances"
    assert c.get(path).status_code == 401
    assert c.post(path, headers=authorization).status_code == 405
    r = c.get(path, headers=authorization)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "private, no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.json()["real_trading_enabled"] is False
    assert seen == [DID]
