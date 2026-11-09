"""Read-only Privy BTC/ETH/USDC/USDT mainnet balances cannot grant execution."""
import asyncio
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api, wallet_balances as w
from backend.blockchain_history import CONTRACTS

DID="did:privy:alice"
BTC="bc1q"+"a"*38
ETH="0x"+"a"*40
WEI=10**18


def record(*,btc=BTC,eth=ETH):
    rows=[]
    if btc:
        rows.append({"type":"wallet","chain_type":"bitcoin-segwit","address":btc})
    if eth:
        rows.append({"type":"wallet","chain_type":"ethereum","address":eth,
                     "wallet_client_type":"privy"})
    return {"id":DID,"linked_accounts":rows}


def test_linked_wallet_selection_is_strict_and_does_not_trust_extra_accounts():
    u=record()
    u["linked_accounts"] += [
        {"type":"wallet","chain_type":"ethereum","wallet_client_type":"metamask",
         "address":"0x"+"b"*40},
        {"type":"wallet","chain_type":"polygon","wallet_client_type":"privy",
         "address":"0x"+"b"*40},
    ]
    assert w.verified_addresses(u,DID)=={"BTC":BTC,"ETH":ETH.lower()}
    assert w.verified_addresses(record(eth=None),DID)=={"BTC":BTC,"ETH":None}
    with pytest.raises(ValueError):
        w.verified_addresses(u,"did:privy:bob")
    with pytest.raises(ValueError):
        w.verified_addresses({"id":DID,"linked_accounts":None},DID)
    u["linked_accounts"].append({"type":"wallet","chain_type":"ethereum",
                                 "wallet_client_type":"privy","address":"0x"+"b"*40})
    with pytest.raises(ValueError):
        w.verified_addresses(u,DID)


def test_btc_confirmed_satoshis_excludes_unconfirmed_or_mempool():
    sample={"chain_stats":{"funded_txo_sum":150_000_000,
                           "spent_txo_sum":25_000_000},
            "mempool_stats":{"funded_txo_sum":500_000_000,
                             "spent_txo_sum":0}}
    assert w.parse_btc_balance(sample)=="1.25"
    with pytest.raises(ValueError):
        w.parse_btc_balance({"chain_stats":{"funded_txo_sum":10,"spent_txo_sum":20}})
    with pytest.raises(ValueError):
        w.parse_btc_balance({"chain_stats":{"funded_txo_sum":None,"spent_txo_sum":0}})


def test_eth_mainnet_wei_not_valued_as_usd():
    assert w.parse_eth_balance({"coin_balance":str(2*WEI)})=="2"
    assert w.parse_eth_balance({"balance":str(WEI//2)})=="0.5"
    with pytest.raises(ValueError):
        w.parse_eth_balance({"coin_balance":float("nan")})
    with pytest.raises(ValueError):
        w.parse_eth_balance({})


def test_token_contract_and_decimals_take_precedence_over_spoofed_symbols():
    payload=[
        {"token":{"address_hash":CONTRACTS["USDC"],"symbol":"USDT",
                  "type":"ERC-20","decimals":"6"},"value":"123456789"},
        {"token":{"address":CONTRACTS["USDT"],"symbol":"USDC",
                  "type":"ERC-20","decimals":"6"},"value":"9900000"},
        {"token":{"address_hash":"0x"+"f"*40,"symbol":"USDC",
                  "type":"ERC-20","decimals":"6"},"value":"9999999999999"},
    ]
    assert w.parse_stable_balances(payload)=={"USDC":"123.456789","USDT":"9.9"}
    assert w.parse_stable_balances([])=={"USDC":"0","USDT":"0"}
    with pytest.raises(ValueError):
        w.parse_stable_balances(payload+[payload[0]])
    with pytest.raises(ValueError):
        w.parse_stable_balances([{**payload[0],"token":{**payload[0]["token"],"decimals":"18"}}])
    with pytest.raises(ValueError):
        w.parse_stable_balances({"items":payload})


@pytest.mark.parametrize("value",[-1,True,1.3,"1.2","1e10","NaN",None,10**80])
def test_rejects_invalid_onchain_units(value):
    with pytest.raises(ValueError):
        w._units(value,w.WEI)


@pytest.fixture
def signed(monkeypatch):
    private=ec.generate_private_key(ec.SECP256R1())
    public=private.public_key().public_bytes(
        serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",public)
    def make(did=DID):
        now=int(time.time())
        token=jwt.encode({
            "iss":"privy.io","aud":"cmw7sb7ty013o0gl55aqyzi3y",
            "sub":did,"sid":"wallet-balances-test","iat":now,"exp":now+600,
        },private,algorithm="ES256")
        return {"Authorization":"Bearer "+token}
    return make


def test_balance_route_requires_login_and_never_accepts_client_wallet(signed,monkeypatch):
    seen=[]
    async def owned(did):
        seen.append(did)
        return {"mode":"VERIFIED_READ_ONLY","wallet_ownership_verified":True,
                "real_trading_enabled":False,"automatic_signing_enabled":False,
                "wallet_funds_used_for_paper":False,"balances":{}}
    monkeypatch.setattr(api,"get_wallet_balances",owned)
    client=TestClient(api.app)
    path="/api/wallet/balances"
    assert client.get(path).status_code==401
    assert client.get(path,headers={"Authorization":"Bearer fake"}).status_code==401
    data=client.get(path+"?address=0x"+("f"*40),
                    headers=signed()).json()
    assert data["mode"]=="VERIFIED_READ_ONLY"
    assert seen==[DID]
    assert data["automatic_signing_enabled"] is False
    assert client.post(path,headers=signed()).status_code==405
    assert client.get(path,headers=signed("did:privy:bob")).status_code==200
    assert seen==[DID,"did:privy:bob"]


def test_missing_privy_secret_returns_503_and_never_fabricates_balances(monkeypatch):
    from backend.privy_wallet_verification import fetch_privy_user
    monkeypatch.delenv("MININEX_PRIVY_APP_SECRET",raising=False)
    with pytest.raises(HTTPException) as err:
        asyncio.run(fetch_privy_user(DID))
    assert err.value.status_code==503


def test_verified_mainnet_balances_can_be_read_without_any_signing(monkeypatch):
    async def user(did):
        assert did==DID
        return record()
    monkeypatch.setattr(w,"fetch_privy_user",user)
    visited=[]
    class Reply:
        def __init__(self,payload):self.payload=payload
        def raise_for_status(self):pass
        def json(self):return self.payload
    class Client:
        def __init__(self,**kwargs):
            assert kwargs["follow_redirects"] is False
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,url,**kwargs):
            visited.append(url)
            if url.startswith(w.BTC_API):
                return Reply({"chain_stats":{"funded_txo_sum":230000000,
                                             "spent_txo_sum":100000000}})
            if url.endswith("/token-balances"):
                return Reply([
                    {"token":{"address_hash":CONTRACTS["USDC"],"type":"ERC-20",
                              "decimals":"6"},"value":"4100000"},
                    {"token":{"address_hash":CONTRACTS["USDT"],"type":"ERC-20",
                              "decimals":"6"},"value":"2400000"},
                ])
            return Reply({"coin_balance":str(15*10**17)})
    monkeypatch.setattr(w.httpx,"AsyncClient",Client)
    data=asyncio.run(w.get_wallet_balances(DID))
    assert len(visited)==3
    assert data["balances"]["BTC"]["balance"]=="1.3"
    assert data["balances"]["ETH"]["balance"]=="1.5"
    assert data["balances"]["USDC"]["balance"]=="4.1"
    assert data["balances"]["USDT"]["balance"]=="2.4"
    assert data["wallet_ownership_verified"] is True
    assert data["real_trading_enabled"] is False
    assert data["automatic_signing_enabled"] is False
    assert data["wallet_funds_used_for_paper"] is False
    assert data["pricing"]=="NATIVE_UNITS_ONLY"
    assert data["wallets"]["BTC"]==BTC
    assert data["wallets"]["ETH"]==ETH
    assert all(r["status"]=="verified" for r in data["balances"].values())


def test_missing_linked_native_btc_wallet_is_unknown_not_zero(monkeypatch):
    async def user(did):return record(btc=None)
    monkeypatch.setattr(w,"fetch_privy_user",user)
    class Reply:
        def __init__(self,p):self.p=p
        def raise_for_status(self):pass
        def json(self):return self.p
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,url,**kwargs):
            assert not url.startswith(w.BTC_API)
            if url.endswith("/token-balances"):return Reply([])
            return Reply({"coin_balance":"0"})
    monkeypatch.setattr(w.httpx,"AsyncClient",Client)
    result=asyncio.run(w.get_wallet_balances(DID))
    assert result["balances"]["BTC"]["status"]=="wallet_missing"
    assert result["balances"]["BTC"]["balance"] is None
    assert result["balances"]["ETH"]["balance"]=="0"
    assert result["balances"]["USDC"]["balance"]=="0"
    assert result["balances"]["USDT"]["balance"]=="0"


def test_invalid_provider_responses_fail_closed_no_zero_balance(monkeypatch):
    async def user(did):return record()
    monkeypatch.setattr(w,"fetch_privy_user",user)
    class Reply:
        def raise_for_status(self):pass
        def json(self):return {"unexpected":"shape"}
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,url,**kwargs):return Reply()
    monkeypatch.setattr(w.httpx,"AsyncClient",Client)
    with pytest.raises(HTTPException) as err:
        asyncio.run(w.get_wallet_balances(DID))
    assert err.value.status_code==503
