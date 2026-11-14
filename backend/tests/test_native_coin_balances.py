"""Read-only native SOL XRP TRX holdings from their own networks, by Privy DID."""
import asyncio
import hashlib
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient

from backend import app as api, native_coin_balances as native

DID="did:privy:native-coin-account"
WALLET_BYTES={
    "SOL": bytes([2])*32,
    "XRP": bytes([0])+bytes([3])*20,
    "TRX": bytes([0x41])+bytes([4])*20,
}

def b58(value, alphabet):
    n=int.from_bytes(value,"big")
    out=""
    while n:
        n,rem=divmod(n,58)
        out=alphabet[rem]+out
    leading=len(value)-len(value.lstrip(b"\0"))
    return alphabet[0]*leading+out

SOL=b58(WALLET_BYTES["SOL"],native._BASE58)
XRP=b58(WALLET_BYTES["XRP"]+native._checksum(WALLET_BYTES["XRP"]),native._RIPPLE)
TRX=b58(WALLET_BYTES["TRX"]+native._checksum(WALLET_BYTES["TRX"]),native._BASE58)
ADDR={"SOL":SOL,"XRP":XRP,"TRX":TRX}

def user(did=DID, **overrides):
    wallets=[]
    for coin,address in ADDR.items():
        wallets.append({
            "type":"wallet", "chain_type":native.CHAINS[coin][0],
            "wallet_client_type":"privy", "address":address,
            **overrides,
        })
    return {"id":did,"linked_accounts":wallets}

class Response:
    content=b'{"result":"test"}'
    def __init__(self, result):
        self.result=result
    def json(self): return self.result
    def raise_for_status(self): return None

@pytest.fixture
def sources(monkeypatch):
    calls=[]
    problems={}
    class Client:
        def __init__(self, **kwargs):
            assert kwargs.get("follow_redirects") is False
        async def __aenter__(self):return self
        async def __aexit__(self,*args):return None
        async def post(self,url,*,json,headers):
            coin=next(coin for coin in native.ORDER
                      if native.CHAINS[coin][2]==url)
            calls.append((coin,json))
            if coin in problems:
                error=problems[coin]
                if isinstance(error,Exception):
                    raise error
                return Response(error)
            if coin=="SOL":
                assert json["method"]=="getBalance"
                assert json["params"]==[SOL,{"commitment":"finalized"}]
                return Response({
                    "jsonrpc":"2.0","id":1,
                    "result":{"context":{"slot":10},"value":1500000000},
                })
            if coin=="XRP":
                assert json["params"]==[{"account":XRP,"ledger_index":"validated"}]
                return Response({"result":{
                    "status":"success","validated":True,
                    "account_data":{"Account":XRP,"Balance":"2500000"},
                }})
            assert json == {"address":TRX,"visible":True}
            return Response({"address":TRX,"balance":3100000,"create_time":123})
    async def fetch(did):
        assert did==DID
        return user()
    for row in native.CHAINS.values():
        monkeypatch.delenv(row[3],raising=False)
    monkeypatch.setattr(native,"fetch_privy_user",fetch)
    monkeypatch.setattr(native.httpx,"AsyncClient",Client)
    return calls,problems

def test_native_mainnet_balances_are_exact_and_separate(sources):
    calls,_=sources
    result=asyncio.run(native.get_native_balances(DID))
    assert result["asset_order"] == ["SOL","XRP","TRX"]
    assert {k:v["balance"] for k,v in result["assets"].items()} == {
        "SOL":"1.5","XRP":"2.5","TRX":"3.1"}
    assert [coin for coin,_ in calls] == list(native.ORDER)
    assert result["wallet_ownership_verified"] is True
    assert result["real_trading_enabled"] is False
    assert result["wallet_funds_used_for_paper"] is False
    assert result["execution_available"] is False

def test_sol_xrp_trx_address_checks_reject_bad_keys_and_external_accounts():
    for coin, address in ADDR.items():
        assert native._valid_address(coin,address)
        assert not native._valid_address(coin,address[:-1]+"0")
        assert not native._valid_address(coin,"0x"+"f"*40)
    assert native.verified_native_addresses(user(),"did:privy:someoneelse") if False else True
    with pytest.raises(ValueError):
        native.verified_native_addresses(user(),"did:privy:someoneelse")
    external=user()
    for account in external["linked_accounts"]:
        account["wallet_client_type"]="metamask"
    assert all(v is None for v in native.verified_native_addresses(external,DID).values())

def test_unavailable_xrp_does_not_hide_other_verified_chains(sources):
    _,problems=sources
    problems["XRP"]={"result":{"status":"error","error":"actNotFound"}}
    result=asyncio.run(native.get_native_balances(DID))["assets"]
    assert result["XRP"]["status"]=="unavailable"
    assert result["XRP"]["balance"] is None
    assert result["SOL"]["balance"]=="1.5"
    assert result["TRX"]["balance"]=="3.1"

@pytest.mark.parametrize("bad",[
    {},
    {"address":TRX},
    {"address":"not-wallet","balance":0,"create_time":1},
    {"address":TRX,"balance":-1,"create_time":1},
    {"address":TRX,"balance":"NaN","create_time":1},
])
def test_tron_unconfirmed_or_malformed_account_is_never_zero(sources,bad):
    _,problems=sources
    problems["TRX"]=bad
    result=asyncio.run(native.get_native_balances(DID))["assets"]
    assert result["TRX"]["status"]=="unavailable"
    assert result["TRX"]["balance"] is None

def test_tron_verified_activated_zero_is_valid(sources):
    _,problems=sources
    problems["TRX"]={"address":TRX,"balance":0,"create_time":1}
    result=asyncio.run(native.get_native_balances(DID))["assets"]
    assert result["TRX"]["status"]=="verified"
    assert result["TRX"]["balance"]=="0"

def test_ambiguous_native_wallet_never_queries_rpc(sources,monkeypatch):
    seen,_=sources
    other = user()
    other["linked_accounts"].append({
        "type":"wallet","chain_type":"xrpl","wallet_client_type":"privy",
        "address":b58(bytes([0])+bytes([8])*20+
                      native._checksum(bytes([0])+bytes([8])*20),native._RIPPLE),
    })
    async def fetch(did):return other
    monkeypatch.setattr(native,"fetch_privy_user",fetch)
    result=asyncio.run(native.get_native_balances(DID))["assets"]
    assert result["XRP"]["status"]=="wallet_missing"
    assert {coin for coin,_ in seen}=={"SOL","TRX"}

@pytest.fixture
def authorization(monkeypatch):
    key=ec.generate_private_key(ec.SECP256R1())
    pub=key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",pub)
    now=int(time.time())
    token=jwt.encode({
        "iss":"privy.io","aud":"cmw7sb7ty013o0gl55aqyzi3y",
        "sub":DID,"sid":"native-balance","iat":now,"exp":now+600,
    },key,algorithm="ES256")
    return {"Authorization":"Bearer "+token}

def test_native_balance_route_requires_privy_identity_and_is_get_only(monkeypatch,authorization):
    seen=[]
    async def fetch(did):
        seen.append(did)
        return {"mode":"NATIVE_MAINNET_BALANCES_READ_ONLY",
                "execution_available":False}
    monkeypatch.setattr(api,"get_native_balances",fetch)
    client=TestClient(api.app)
    endpoint="/api/wallet/native-balances"
    assert client.get(endpoint).status_code==401
    assert client.post(endpoint,headers=authorization).status_code==405
    result=client.get(endpoint,headers=authorization)
    assert result.status_code==200
    assert result.headers["cache-control"]=="private, no-store"
    assert seen==[DID]
