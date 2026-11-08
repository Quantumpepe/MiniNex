"""Read-only public explorer transaction preview must never become a tax record."""
from datetime import datetime, timezone
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient

from backend import app as api
from backend.blockchain_preview import _btc_tx, _eth_tx, check_address

BTC="bc1q"+("a"*38)
ETH="0x"+("a"*40)
BTC_HASH="f"*64
ETH_HASH="0x"+("e"*64)


@pytest.fixture
def signed(monkeypatch):
    private=ec.generate_private_key(ec.SECP256R1())
    public=private.public_key().public_bytes(
        serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY", public)
    def make(did="did:privy:alice"):
        now=int(time.time())
        return {"Authorization": "Bearer "+jwt.encode({
            "iss":"privy.io","aud":"cmw7sb7ty013o0gl55aqyzi3y",
            "sub":did,"sid":"preview-test","iat":now,"exp":now+600
        },private,algorithm="ES256")}
    return make


def test_btc_incoming_and_outgoing_are_net_not_total_outputs():
    base={"txid":BTC_HASH,"status":{"confirmed":True,"block_time":1700000000}}
    recv=_btc_tx({**base,
        "vin":[{"prevout":{"scriptpubkey_address":"bc1q"+"b"*38,"value":150000000}}],
        "vout":[{"scriptpubkey_address":BTC,"value":100000000},
                {"scriptpubkey_address":"bc1q"+"b"*38,"value":40000000}]
    },BTC)
    assert recv["direction"]=="IN"
    assert recv["quantity"]=="1"
    assert recv["confirmed"] is True
    sent=_btc_tx({**base,
        "vin":[{"prevout":{"scriptpubkey_address":BTC,"value":100000000}}],
        "vout":[{"scriptpubkey_address":BTC,"value":30000000},
                {"scriptpubkey_address":"bc1q"+"b"*38,"value":69500000}]
    },BTC)
    assert sent["direction"]=="OUT"
    assert sent["quantity"]=="0.7"


def test_eth_native_transfer_is_not_token_or_contract_call():
    base={"hash":ETH_HASH,"from":{"hash":"0x"+"b"*40},"to":{"hash":ETH},
          "timestamp":"2026-01-08T13:20:11Z","block_number":1234,"status":"ok"}
    item=_eth_tx({**base,"value":str(2*10**18)},ETH)
    assert item["asset"]=="ETH"
    assert item["direction"]=="IN"
    assert item["quantity"]=="2"
    assert _eth_tx({**base,"value":"0"},ETH) is None
    assert _eth_tx({**base,"value":"123","status":"error"},ETH)["confirmed"] is False


def test_rejects_invalid_address_before_fetch_and_requires_privy(signed,monkeypatch):
    async def preview(asset,address):
        return {"asset":asset,"address":address,"items":[],"preview_only":True,
                "wallet_ownership_verified":False,"tax_complete":False,"page_complete":False}
    monkeypatch.setattr(api,"fetch_preview",preview)
    client=TestClient(api.app)
    for url in (f"/api/blockchain/preview/BTC?address={BTC}",f"/api/blockchain/preview/ETH?address={ETH}"):
        assert client.get(url).status_code==401
        assert client.get(url,headers=signed()).status_code==200
    for path in ("/api/blockchain/preview/BTC?address=hello",
                 "/api/blockchain/preview/ETH?address=0x123",
                 f"/api/blockchain/preview/SOL?address={ETH}"):
        assert client.get(path,headers=signed()).status_code in (404,422)


def test_preview_not_tax_confirmation():
    assert check_address("BTC",BTC)==BTC
    assert check_address("ETH",ETH)==ETH
    # There is deliberately no insert, write or ownership elevation in preview.
    from backend import blockchain_preview
    import inspect
    source=inspect.getsource(blockchain_preview)
    assert "mininex_verified_activity" not in source
    assert "INSERT INTO" not in source
    assert "wallet_ownership_verified" in source
