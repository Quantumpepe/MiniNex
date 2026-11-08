"""Paginated explorer history and verified archival must fail closed."""
import time
from datetime import datetime, timezone
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend.blockchain_history import (
    CONTRACTS, encode_cursor, parse_token, validate_request,
)
from backend.privy_wallet_verification import address_in_privy_user
from backend.verified_sync import checked_entry, synchronize_one_page

BTC="bc1q"+"a"*38
ETH="0x"+"a"*40
HASH="0x"+"f"*64


@pytest.fixture
def signed(monkeypatch):
    private=ec.generate_private_key(ec.SECP256R1())
    public=private.public_key().public_bytes(
        serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",public)
    def make(did="did:privy:alice"):
        now=int(time.time())
        return {"Authorization":"Bearer "+jwt.encode({
            "iss":"privy.io","aud":"cmw7sb7ty013o0gl55aqyzi3y",
            "sub":did,"sid":"history-test","iat":now,"exp":now+600,
        },private,algorithm="ES256")}
    return make


def test_token_contract_is_not_trusted_from_symbol_alone():
    base={
        "tx_hash":HASH,"log_index":8,"block_number":100,
        "timestamp":"2026-01-10T12:00:00Z",
        "from":{"hash":"0x"+"b"*40},"to":{"hash":ETH},
        "token":{"address_hash":CONTRACTS["USDC"],"symbol":"USDC","type":"ERC-20","decimals":"6"},
        "total":{"value":"12345000","decimals":"6"},
    }
    item=parse_token(base,ETH,"USDC")
    assert item["quantity"]=="12.345"
    assert item["asset"]=="USDC"
    assert item["direction"]=="IN"
    assert item["id"]==HASH+":8"
    assert parse_token({**base,"token":{**base["token"],"address_hash":"0x"+"c"*40}},ETH,"USDC") is None
    assert parse_token({**base,"total":{}},ETH,"USDC") is None


def test_page_cursor_roundtrip_and_rejection():
    page={"block_number":10,"index":3,"items_count":50}
    cursor=encode_cursor(page)
    assert validate_request("ETH",ETH,cursor)[2]==page
    assert validate_request("USDC",ETH,cursor)[2]==page
    assert validate_request("BTC",BTC,"a"*64)[2]=={"after_txid":"a"*64}
    for bad in ("hello", "a"*1000, "%%%"):
        with pytest.raises(HTTPException):
            validate_request("ETH",ETH,bad)
    with pytest.raises(HTTPException):
        validate_request("USDT",BTC)


def test_privy_account_binding_strict():
    account={
        "id":"did:privy:alice","linked_accounts":[
            {"type":"wallet","chain_type":"ethereum",
             "wallet_client_type":"privy","address":ETH},
            {"type":"wallet","chain_type":"bitcoin-segwit","address":BTC},
        ],
    }
    assert address_in_privy_user(account,did="did:privy:alice",asset="USDC",address=ETH)
    assert address_in_privy_user(account,did="did:privy:alice",asset="BTC",address=BTC)
    assert not address_in_privy_user(account,did="did:privy:bob",asset="ETH",address=ETH)
    assert not address_in_privy_user(account,did="did:privy:alice",asset="BTC",address=ETH)
    assert not address_in_privy_user({
        "id":"did:privy:alice","linked_accounts":[
            {"type":"wallet","chain_type":"ethereum",
             "wallet_client_type":"metamask","address":ETH}]},
        did="did:privy:alice",asset="ETH",address=ETH)


def test_archived_data_has_no_invented_prices_or_demo_data():
    item={
        "asset":"USDT","confirmed":True,"timestamp":"2026-01-10T12:00:00Z",
        "direction":"OUT","quantity":"10.25","tx_hash":HASH,"id":HASH+":1"
    }
    entry=checked_entry(item,"USDT")
    assert entry is not None
    assert entry[1]=="USDT" and entry[2]=="WITHDRAWAL"
    assert checked_entry({**item,"confirmed":False},"USDT") is None
    assert checked_entry({**item,"asset":"BTC"},"USDT") is None


def test_history_and_sync_require_auth_and_safe_wallet_checks(signed,monkeypatch):
    import backend.verified_sync as sync
    seen=[]
    async def fake_history(asset,address,cursor):
        return {"asset":asset,"address":address,"items":[],"next_cursor":None,
                "page_complete":True,"preview_only":True,"tax_complete":False}
    async def verify(did,asset,address):
        seen.append((did,asset,address))
        if did.endswith(":bob"):
            raise HTTPException(403,"Wallet not verified")
    monkeypatch.setattr(api,"fetch_history",fake_history)
    monkeypatch.setattr(sync,"fetch_history",fake_history)
    monkeypatch.setattr(sync,"verify_wallet_address",verify)
    monkeypatch.setattr(sync,"store_confirmed_page",lambda *a: 0)
    client=TestClient(api.app)
    path=f"/api/blockchain/history/USDC?address={ETH}"
    store=f"/api/blockchain/sync/ETH?address={ETH}"
    assert client.get(path).status_code==401
    assert client.post(store).status_code==401
    assert client.get(path,headers=signed()).status_code==200
    ok=client.post(store,headers=signed())
    assert ok.status_code==200
    assert ok.json()["tax_complete"] is False
    assert ok.json()["wallet_ownership_verified"] is True
    assert client.post(store,headers=signed("did:privy:bob")).status_code==403
    assert seen==[("did:privy:alice","ETH",ETH),("did:privy:bob","ETH",ETH)]
