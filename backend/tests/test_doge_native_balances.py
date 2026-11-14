"""Native Dogecoin holdings use the same Privy-bound wallet API as SOL/XRP/TRX."""
import asyncio
import json

import pytest

from backend import native_coin_balances as native

DID="did:privy:doge-owner"

def b58(data: bytes) -> str:
    value=int.from_bytes(data,"big")
    output=""
    while value:
        value,rem=divmod(value,58)
        output=native._BASE58[rem]+output
    return native._BASE58[0]*(len(data)-len(data.lstrip(b"\\0")))+output

def doge_address(byte: int=5) -> str:
    original=bytes([0x1e])+bytes([byte])*20
    return b58(original+native._checksum(original))

ADDRESS=doge_address()


class Response:
    def __init__(self,body):
        self.body=body
        self.content=json.dumps(body).encode()
    def raise_for_status(self):
        return None
    def json(self):
        return self.body


def record(*,did=DID,address=ADDRESS,client="privy"):
    return {"id":did,"linked_accounts":[{
        "type":"wallet","chain_type":"dogecoin",
        "wallet_client_type":client,"address":address,
    }]}


@pytest.fixture
def explorer(monkeypatch):
    seen=[]
    state={"response":{
        "address":ADDRESS,"balance":1234567890,
        "final_balance":2234567890,"unconfirmed_balance":1000000000,
    }}
    async def user(did):
        assert did==DID
        return record()
    class Client:
        def __init__(self,**kwargs):
            assert kwargs["follow_redirects"] is False
        async def __aenter__(self):return self
        async def __aexit__(self,*args):return None
        async def get(self,url,*,headers):
            seen.append((url,headers))
            return Response(state["response"])
        async def post(self,*args,**kwargs):
            raise AssertionError("DOGE should only use the confirmed balance GET endpoint")
    monkeypatch.delenv("MININEX_DOGE_EXPLORER_URL",raising=False)
    monkeypatch.setattr(native,"fetch_privy_user",user)
    monkeypatch.setattr(native.httpx,"AsyncClient",Client)
    return seen,state


def test_native_doge_p2pkh_validation_is_different_from_other_networks():
    assert ADDRESS.startswith("D")
    assert native._valid_address("DOGE",ADDRESS)
    assert not native._valid_address("DOGE",ADDRESS[:-1]+"1")
    assert not native._valid_address("DOGE","bc1q"+"a"*38)
    assert not native._valid_address("BTC",ADDRESS)
    assert not native._valid_address("DOGE","0x"+"a"*40)
    assert not native._valid_address("DOGE",doge_address(6)[:-2]+"00")


def test_confirmed_doge_balance_uses_koinu_not_unconfirmed(explorer):
    seen,_=explorer
    value=asyncio.run(native.get_native_balances(DID))
    assert value["asset_order"]==["SOL","XRP","TRX","DOGE"]
    assert value["assets"]["DOGE"]=={
        "asset":"DOGE","network":"dogecoin-mainnet",
        "status":"verified","balance":"12.3456789",
        "native":True,"wallet_funds_used_for_paper":False,
    }
    assert seen==[(
        "https://api.blockcypher.com/v1/doge/main/addrs/"+ADDRESS+"/balance",
        {"Accept":"application/json"},
    )]
    assert value["assets"]["SOL"]["status"]=="wallet_missing"
    assert value["execution_available"] is False


@pytest.mark.parametrize("body",[
    {},
    {"address":ADDRESS},
    {"address":ADDRESS,"balance":-1},
    {"address":ADDRESS,"balance":"1.5"},
    {"address":ADDRESS,"balance":True},
    {"address":ADDRESS,"balance":"NaN"},
    {"address":"not-owner","balance":0},
    {"address":ADDRESS,"balance":2**64},
])
def test_invalid_explorer_response_does_not_fabricate_zero(explorer,body):
    _,state=explorer
    state["response"]=body
    result=asyncio.run(native.get_native_balances(DID))
    row=result["assets"]["DOGE"]
    assert row["status"]=="unavailable" and row["balance"] is None


def test_real_confirmed_zero_is_allowed_when_wallet_matches(explorer):
    _,state=explorer
    state["response"]={"address":ADDRESS,"balance":0,"final_balance":1000}
    row=asyncio.run(native.get_native_balances(DID))["assets"]["DOGE"]
    assert row["status"]=="verified" and row["balance"]=="0"


def test_external_or_duplicate_doge_wallet_is_never_assumed_owned(explorer,monkeypatch):
    seen,_=explorer
    async def external(did):
        return record(client="metamask")
    monkeypatch.setattr(native,"fetch_privy_user",external)
    assets=asyncio.run(native.get_native_balances(DID))["assets"]
    assert assets["DOGE"]["status"]=="wallet_missing"
    assert seen==[]

    async def ambiguous(did):
        item=record()
        item["linked_accounts"].append({
            "type":"wallet","chain_type":"dogecoin",
            "wallet_client_type":"privy","address":doge_address(6),
        })
        return item
    monkeypatch.setattr(native,"fetch_privy_user",ambiguous)
    assets=asyncio.run(native.get_native_balances(DID))["assets"]
    assert assets["DOGE"]["status"]=="wallet_missing"
    assert seen==[]
