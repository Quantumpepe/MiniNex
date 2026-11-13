"""Read-only Uniswap ETH unsigned swap plan: no approvals, signatures, sends."""
import asyncio
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import live_eth_swap_plan as sp
from backend.live_eth_quotes import ETH_NATIVE, STABLES

DID="did:privy:eth-swap-plan"
WALLET="0x"+"a"*40


def _word(value):
    return value.to_bytes(32, "big")


def _blob(data):
    return _word(len(data)) + data + b"\x00" * (-len(data) % 32)


def router_calldata(*, commands=b"\x00", inputs=(b"\x01" * 32,), deadline=None):
    """Canonical execute(bytes,bytes[],uint256) test ABI, with no actual trade."""
    if deadline is None:
        deadline = int(time.time()) + 120
    commands_blob = _blob(commands)
    encoded = [_blob(value) for value in inputs]
    cursor = 32 * len(encoded)
    offsets = []
    for value in encoded:
        offsets.append(_word(cursor))
        cursor += len(value)
    input_blob = _word(len(encoded)) + b"".join(offsets) + b"".join(encoded)
    header = _word(96) + _word(96 + len(commands_blob)) + _word(deadline)
    return "0x3593564c" + (header + commands_blob + input_blob).hex()


CALLDATA=router_calldata()


def record():
    return {"id":DID,"linked_accounts":[{
        "type":"wallet","chain_type":"ethereum",
        "wallet_client_type":"privy","address":WALLET,
    }]}


def funds(eth="1.25",usdc="1000",usdt="500"):
    return {
        "wallets":{"ETH":WALLET},
        "balances":{
            "ETH":{"status":"verified","balance":eth},
            "USDC":{"status":"verified","balance":usdc},
            "USDT":{"status":"verified","balance":usdt},
        },
    }


def tx(side="BUY",amount="100000000"):
    return {
        "to":sp._ROUTER,"from":WALLET,"chainId":1,
        "data":CALLDATA,"value":"0" if side=="BUY" else amount,
        "gasLimit":"300000","maxFeePerGas":"4000000000",
        "maxPriorityFeePerGas":"1000000000",
    }


def quote(payload):
    if payload["tokenIn"]==ETH_NATIVE:
        output_amount="100000000"
        min_amount="99500000"
    else:
        output_amount="210000000000000000"
        min_amount="208950000000000000"
    return {
        "routing":"CLASSIC",
        "quote":{
            "input":{"token":payload["tokenIn"],"amount":payload["amount"]},
            "output":{"token":payload["tokenOut"],"amount":output_amount,
                      "minimumAmount":min_amount,"recipient":payload["swapper"]},
            "swapper":payload["swapper"],
        },
        "permitData":None,
    }


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("MININEX_UNISWAP_API_KEY","private-test-key")
    async def user(did):
        assert did==DID
        return record()
    async def balances(did):
        assert did==DID
        return funds()
    monkeypatch.setattr(sp,"fetch_privy_user",user)
    monkeypatch.setattr(sp,"get_wallet_balances",balances)


@pytest.fixture
def headers(monkeypatch):
    secret=ec.generate_private_key(ec.SECP256R1())
    pub=secret.public_key().public_bytes(
        serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",pub)
    now=int(time.time())
    token=jwt.encode({
        "iss":"privy.io","aud":"cmw7sb7ty013o0gl55aqyzi3y",
        "sub":DID,"sid":"eth-swap-plan","iat":now,"exp":now+600,
    },secret,algorithm="ES256")
    return {"Authorization":"Bearer "+token}


def test_eth_unsigned_tx_accepts_only_exact_mainnet_router_and_funded_gas():
    good=sp.inspect_uniswap_swap_tx(
        tx(),wallet=WALLET,side="BUY",input_atomic="100000000",eth_balance="1")
    assert good["tx_outer_fields_verified"] is True
    assert good["router_envelope_verified"] is True
    assert good["router_deadline_verified"] is True
    assert good["router_command_count"] == 1
    assert good["router_command_contents_verified"] is False
    assert good["router_commands_decoded"] is False
    assert good["safe_to_sign"] is False
    assert "data" not in good
    assert good["router"]==sp._ROUTER
    good=sp.inspect_uniswap_swap_tx(
        tx("SELL","100000000000000000"),wallet=WALLET,side="SELL",
        input_atomic="100000000000000000",eth_balance="1.5")
    assert good["safe_to_sign"] is False


@pytest.mark.parametrize("changes",[
    {"from":"0x"+"b"*40},
    {"to":"0x"+"b"*40},
    {"chainId":56},
    {"chainId":"1"},
    {"data":"0x12345678"+"0"*192},
    {"data":"0x3593564c00"},
    {"data":"0x3593564c"+"g"*192},
    {"value":"100000000"},
    {"gasLimit":"3000000"},
    {"maxFeePerGas":"400000000000"},
    {"maxPriorityFeePerGas":"5000000000"},
])
def test_unsafe_swap_transaction_fails_before_any_signing(changes):
    bad={**tx(),**changes}
    with pytest.raises(ValueError):
        sp.inspect_uniswap_swap_tx(
            bad,wallet=WALLET,side="BUY",input_atomic="100000000",
            eth_balance="1.25")


def test_native_sale_must_spend_exact_input_and_leave_gas_reserve():
    with pytest.raises(ValueError):
        sp.inspect_uniswap_swap_tx(
            tx("SELL","500000000000000000"),wallet=WALLET,
            side="SELL",input_atomic="100000000000000000",eth_balance="1")
    with pytest.raises(ValueError):
        sp.inspect_uniswap_swap_tx(
            tx("SELL","100000000000000000"),wallet=WALLET,
            side="SELL",input_atomic="100000000000000000",eth_balance="0.1")
    with pytest.raises(ValueError):
        sp.inspect_uniswap_swap_tx(
            tx(),wallet=WALLET,side="BUY",input_atomic="100000000",
            eth_balance="NaN")


def test_real_quote_approval_and_swap_stays_unsigned(monkeypatch,configured):
    actions=[]
    original=httpx.AsyncClient
    def mock_client(*args,**kwargs):
        def respond(request):
            assert kwargs["follow_redirects"] is False
            assert request.headers["x-api-key"]=="private-test-key"
            assert request.headers["x-universal-router-version"]=="2.1.2"
            body=json.loads(request.content)
            if str(request.url).endswith("/check_approval"):
                actions.append("check_approval")
                assert body["walletAddress"]==WALLET
                assert body["token"]==STABLES["USDC"]
                assert body["amount"]=="100000000"
                assert body["chainId"]==1
                return httpx.Response(200,json={"approval":None,"cancel":None})
            if str(request.url).endswith("/quote"):
                actions.append("quote")
                assert body["type"]=="EXACT_INPUT"
                assert body["swapper"]==body["recipient"]==WALLET
                assert body["tokenInChainId"]==body["tokenOutChainId"]==1
                assert body["amount"]=="100000000"
                return httpx.Response(200,json=quote(body))
            if str(request.url).endswith("/swap"):
                actions.append("swap")
                assert body["simulateTransaction"] is True
                assert body["swapSource"]=="WALLET"
                return httpx.Response(200,json={"swap":tx()})
            raise AssertionError("Unexpected provider call")
        return original(*args,transport=httpx.MockTransport(respond),**kwargs)
    monkeypatch.setattr(sp.httpx,"AsyncClient",mock_client)
    result=asyncio.run(sp.make_eth_swap_plan(
        DID,side="BUY",stable="USDC",amount="100"))
    assert actions==["check_approval","quote","swap"]
    assert result["mode"]=="LIVE_ETH_SWAP_PLAN_ONLY"
    assert result["state"]=="UNSIGNED_TX_REVIEW_REQUIRED"
    assert result["transaction_built"] is True
    assert result["safe_to_sign"] is False
    assert result["execution_enabled"] is False
    assert result["can_submit"] is False
    assert result["automatic_signing_enabled"] is False
    assert "calldata" not in str(result).lower() or "review" in str(result).lower()
    assert CALLDATA not in str(result)
    assert "private-test-key" not in str(result)


def test_missing_token_approval_never_sends_approval(monkeypatch,configured):
    actions=[]
    original=httpx.AsyncClient
    def mock_client(*args,**kwargs):
        def respond(request):
            body=json.loads(request.content)
            if str(request.url).endswith("/check_approval"):
                actions.append("check_approval")
                return httpx.Response(200,json={
                    "approval":{"to":"0x"+"b"*40},"cancel":None})
            if str(request.url).endswith("/quote"):
                actions.append("quote")
                return httpx.Response(200,json=quote(body))
            raise AssertionError("Uniswap swap must not be built if approval missing")
        return original(*args,transport=httpx.MockTransport(respond),**kwargs)
    monkeypatch.setattr(sp.httpx,"AsyncClient",mock_client)
    result=asyncio.run(sp.make_eth_swap_plan(
        DID,side="BUY",stable="USDC",amount="50"))
    assert actions==["check_approval","quote"]
    assert result["state"]=="APPROVAL_OR_PERMIT_REQUIRED"
    assert result["erc20_approval_required"] is True
    assert result["transaction_built"] is False
    assert result["execution_enabled"] is False
    assert "0x"+"b"*40 not in str(result)


def test_missing_uniswap_key_prevents_provider_calls(monkeypatch):
    monkeypatch.delenv("MININEX_UNISWAP_API_KEY",raising=False)
    with pytest.raises(HTTPException) as err:
        asyncio.run(sp.make_eth_swap_plan(
            DID,side="SELL",stable="USDT",amount="0.1"))
    assert err.value.status_code==503


def test_invalid_provider_approval_fails_closed(monkeypatch,configured):
    original=httpx.AsyncClient
    def mock_client(*args,**kwargs):
        def respond(request):
            return httpx.Response(200,json={"approval":None})
        return original(*args,transport=httpx.MockTransport(respond),**kwargs)
    monkeypatch.setattr(sp.httpx,"AsyncClient",mock_client)
    with pytest.raises(HTTPException) as err:
        asyncio.run(sp.make_eth_swap_plan(
            DID,side="BUY",stable="USDC",amount="10"))
    assert err.value.status_code==502


def test_private_swap_plan_api_requires_auth_and_rejects_override(headers):
    client=TestClient(api.app)
    req={"side":"BUY","stable":"USDC","amount":"25"}
    assert client.post("/api/live/eth/swap-plan",json=req).status_code==401
    assert client.post("/api/live/eth/swap-plan",headers=headers,
        json={**req,"recipient":"0x"+"b"*40}).status_code==422
    assert client.post("/api/live/eth/swap-plan",headers=headers,
        json={**req,"chainId":56}).status_code==422
    assert client.post("/api/live/eth/swap-plan",headers=headers,
        json={**req,"side":"LEVERAGED_SHORT"}).status_code==422
    assert client.get("/api/live/eth/swap-plan",headers=headers).status_code==405
    assert not any(route.path=="/api/live/eth/sign-and-send" for route in api.app.routes)


def test_router_envelope_rejects_expired_future_and_partial_revert_commands():
    for calldata in (
        router_calldata(deadline=int(time.time()) - 1),
        router_calldata(deadline=int(time.time()) + 10_000),
        router_calldata(commands=b"\x80"),
        router_calldata(commands=b"\x40"),
        router_calldata(commands=b"\x00\x0b", inputs=(b"\x01" * 32,)),
        "0x3593564c" + "0" * 192,
    ):
        with pytest.raises(ValueError):
            sp.inspect_uniswap_swap_tx(
                {**tx(), "data": calldata},
                wallet=WALLET, side="BUY", input_atomic="100000000",
                eth_balance="1.25",
            )


def test_router_envelope_rejects_noncanonical_offsets_padding_and_trailing_data():
    def changed(start, new_bytes):
        raw = bytearray.fromhex(CALLDATA[10:])
        raw[start:start + len(new_bytes)] = new_bytes
        return "0x3593564c" + raw.hex()

    # Malformed top-level input offset and dynamic bytes[] element offset.
    raw = bytearray.fromhex(CALLDATA[10:])
    input_array_start = int.from_bytes(raw[32:64], "big")
    poisoned = [
        changed(32, _word(96)),
        changed(input_array_start + 32, _word(0)),
        changed(96 + 32 + 1, b"\x01"),  # Nonzero command padding
        CALLDATA + "00" * 32,  # Unaccounted trailing instructions
        CALLDATA[:-2],  # Truncated ABI alignment
    ]
    for calldata in poisoned:
        with pytest.raises(ValueError):
            sp.inspect_uniswap_swap_tx(
                {**tx(), "data": calldata},
                wallet=WALLET, side="BUY", input_atomic="100000000",
                eth_balance="1.25",
            )


def test_multi_command_valid_envelope_never_becomes_signable():
    preview = sp.inspect_uniswap_swap_tx(
        {**tx(), "data": router_calldata(
            commands=b"\x00\x0b", inputs=(b"\x11" * 32, b"\x22" * 64))},
        wallet=WALLET, side="BUY", input_atomic="100000000",
        eth_balance="1.25",
    )
    assert preview["router_command_count"] == 2
    assert preview["router_envelope_verified"] is True
    assert preview["router_command_contents_verified"] is False
    assert preview["safe_to_sign"] is False
    assert preview["calldata_security_review_complete"] is False
    assert "data" not in preview
