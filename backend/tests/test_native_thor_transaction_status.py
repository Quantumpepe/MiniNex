"""THORChain protocol observation never substitutes destination-chain receipts."""
import asyncio

import pytest
from fastapi import HTTPException

from backend import native_thor_transaction_status as thor
from backend.manual_swap_live_quotes import THOR_ASSETS

DID="did:privy:thor_owner"
ADDR={"BTC":"bc1q"+"q"*38,"SOL":"7"*44,"XRP":"r"+"a"*33}
DEST="0x"+"a"*40
HASH={"BTC":"a"*64,"SOL":"3"*88,"XRP":"A"*64}


def original(asset="BTC"):
    return {
        "privy_did":DID,"asset":asset,
        "intent_key":"native_"+"d"*64,
        "source_tx_hash":HASH[asset],
        "source_wallet":ADDR[asset],
        "destination_wallet":DEST,
        "stable":"USDC","side":"SELL","status":"SOURCE_FINALIZED",
    }


def status(asset="BTC",finalized=True,out=False):
    item=original(asset)
    return {
        "tx":{
            "id":HASH[asset].upper() if asset!="SOL" else HASH[asset],
            "chain":asset,
            "from_address":ADDR[asset],
            "to_address":"vault_original",
            "coins":[{"asset":THOR_ASSETS[asset][0],"amount":"125000000"}],
            "memo":"=:"+THOR_ASSETS["USDC"][0]+":"+DEST+"/"+ADDR[asset]+":0/1/0",
        },
        "stages":{
            "inbound_observed":{"completed":True},
            "inbound_finalised":{"completed":finalized},
            "swap_finalised":{"completed":finalized},
        },
        "out_txs":[{"id":"E"*64,"chain":"ETH",
                    "to_address":DEST,
                    "coins":[{"asset":THOR_ASSETS["USDC"][0],
                              "amount":"100000000"}]}] if out else [],
    }


@pytest.mark.parametrize("asset",["BTC","SOL","XRP"])
def test_protocol_status_still_needs_real_receiving_chain_proof(asset):
    verified=thor.inspect_original_thorchain_status(
        original(asset),status(asset,True,True))
    assert verified["source_wallet_original"] is True
    assert verified["destination_owner_bound"] is True
    assert verified["protocol_inbound_finalized"] is True
    assert verified["protocol_reports_swap_finalized"] is True
    assert verified["protocol_outbound_hashes"]==["e"*64]
    assert verified["destination_chain_receipts_verified"] is False
    assert verified["realized_pnl_verified"] is False
    assert verified["fee_assessed"] is False
    assert verified["retry_permitted"] is False
    assert verified["new_transactions_sent"]==0


def test_incomplete_protocol_inbound_never_claims_native_swap_finalized():
    result=thor.inspect_original_thorchain_status(
        original("SOL"),status("SOL",False))
    assert result["protocol_inbound_observed"] is True
    assert result["protocol_inbound_finalized"] is False
    assert result["destination_chain_receipts_verified"] is False


@pytest.mark.parametrize("change",[
    lambda data:data["tx"].update(id="B"*64),
    lambda data:data["tx"].update(from_address=DEST),
    lambda data:data["tx"].update(chain="THOR"),
    lambda data:data["tx"].update(memo="=:ETH.USDC:"+ADDR["BTC"]+":0/1/0"),
    lambda data:data["tx"].update(memo="=:BTC.BTC:"+DEST+":0/1/0"),
    lambda data:data["tx"].update(coins=[{"asset":"BTC.BTC","amount":"0"}]),
    lambda data:data["stages"]["inbound_observed"].update(completed=False),
    lambda data:data["stages"]["inbound_finalised"].update(completed="true"),
    lambda data:data.update(out_txs=[{"id":"E"*64,"chain":"ETH",
                                    "to_address":"0x"+"b"*40,
                                    "coins":[{"asset":THOR_ASSETS["USDC"][0],
                                              "amount":"1"}]}]),
    lambda data:data.update(out_txs=[{"id":"E"*64,"chain":"ETH",
                                    "to_address":DEST,
                                    "coins":[{"asset":"ETH.USDT","amount":"1"}]}]),
])
def test_forged_protocol_source_or_wrong_destination_never_counts(change):
    data=status("BTC",True,True)
    change(data)
    with pytest.raises(ValueError):
        thor.inspect_original_thorchain_status(original("BTC"),data)


@pytest.mark.parametrize("state",[
    "SUBMITTED","RECONCILIATION_REQUIRED","SENDING","PREPARED","DESTINATION_FINALIZED",
])
def test_protocol_does_not_skip_original_native_chain_finality(state):
    row=original("XRP")
    row["status"]=state
    with pytest.raises(ValueError):
        thor.inspect_original_thorchain_status(row,status("XRP"))


def test_protocol_requires_precise_owner_order_key():
    assert thor._VALID_INTENT.fullmatch("native_"+"f"*64)
    assert not thor._VALID_INTENT.fullmatch("native_"+"f"*64+"?sender=another")
    assert not thor._VALID_INTENT.fullmatch("some_other_order_key")


def test_original_order_reader_is_owner_scoped():
    class Cursor:
        def execute(self,sql,args):
            assert "WHERE privy_did=%s AND asset=%s AND intent_key=%s" in sql
            assert args==(DID,"BTC","native_"+"d"*64)
        def fetchone(self):
            return original("BTC")
    row=thor._read_original_locked(
        Cursor(),did=DID,asset="BTC",intent_key="native_"+"d"*64)
    assert row["privy_did"]==DID


def test_unknown_owner_cannot_fetch_order_or_contact_thornode(monkeypatch):
    called=[]
    class Cursor:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def execute(self,sql,params):
            called.append(("db",params))
        def fetchone(self):return None
    class DB:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kwargs):return Cursor()
    monkeypatch.setattr(thor,"_db_url",lambda:"postgres://test")
    monkeypatch.setattr(thor,"_ensure_schema",lambda _:None)
    monkeypatch.setattr(thor.psycopg,"connect",lambda *a,**kw:DB())
    async def run():
        return await thor.get_private_original_protocol_status(
            DID,asset="BTC",intent_key="native_"+"d"*64)
    with pytest.raises(HTTPException) as err:
        asyncio.run(run())
    assert err.value.status_code==409
    assert called==[("db",(DID,"BTC","native_"+"d"*64))]


def test_known_owner_thornode_request_uses_only_original_persisted_hash(monkeypatch):
    trace=[]
    class Cursor:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def execute(self,sql,params):trace.append(params)
        def fetchone(self):return original("BTC")
    class DB:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kwargs):return Cursor()
    monkeypatch.setattr(thor,"_db_url",lambda:"postgres://test")
    monkeypatch.setattr(thor,"_ensure_schema",lambda _:None)
    monkeypatch.setattr(thor.psycopg,"connect",lambda *a,**kw:DB())
    class Response:
        content=b"{}"
        def raise_for_status(self):pass
        def json(self):return status("BTC",True,True)
    class Client:
        def __init__(self,**kw):
            assert kw["follow_redirects"] is False
        async def __aenter__(self):return self
        async def __aexit__(self,*args):return False
        async def get(self,url,**kwargs):
            trace.append(url)
            return Response()
    monkeypatch.setattr(thor.httpx,"AsyncClient",Client)
    result=asyncio.run(thor.get_private_original_protocol_status(
        DID,asset="BTC",intent_key="native_"+"d"*64))
    assert result["destination_chain_receipts_verified"] is False
    assert trace[-1]==thor._THOR_URL+HASH["BTC"]


def _native_buy_outbound(asset):
    if asset=="BTC":
        return "f"*64
    if asset=="XRP":
        return "F"*64
    alphabet="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n=int.from_bytes(bytes(range(1,65)),"big")
    result=""
    while n:
        n,i=divmod(n,58)
        result=alphabet[i]+result
    return result


def _native_buy_original(asset):
    return {
        "privy_did":DID,"asset":asset,"intent_key":"native_"+"a"*64,
        "source_tx_hash":"0x"+"e"*64,
        "source_wallet":DEST,"destination_wallet":ADDR[asset],
        "stable":"USDC","side":"BUY","status":"SOURCE_FINALIZED",
    }


def _native_buy_protocol(asset):
    source=_native_buy_original(asset)
    return {
        "tx":{
            "id":"E"*64,"chain":"ETH","from_address":DEST,
            "coins":[{"asset":THOR_ASSETS["USDC"][0],"amount":"200000000"}],
            "memo":"=:"+THOR_ASSETS[asset][0]+":"+source["destination_wallet"]+"/"+DEST+":0/1/0",
        },
        "stages":{
            "inbound_observed":{"completed":True},
            "inbound_finalised":{"completed":True},
            "swap_finalised":{"completed":True},
        },
        "out_txs":[{
            "id":_native_buy_outbound(asset),
            "chain":asset,"to_address":source["destination_wallet"],
            "coins":[{"asset":THOR_ASSETS[asset][0],"amount":"100000000"}],
        }],
    }


@pytest.mark.parametrize("asset",["BTC","SOL","XRP"])
def test_verified_native_buy_protocol_waits_for_real_native_destination(asset):
    record=thor.inspect_original_thorchain_status(
        _native_buy_original(asset),_native_buy_protocol(asset))
    assert record["asset"]==asset
    assert record["side"]=="BUY"
    assert record["protocol_reports_swap_finalized"] is True
    assert record["protocol_outbound_hashes"]==[_native_buy_outbound(asset).lower()
        if asset=="BTC" else _native_buy_outbound(asset)]
    assert record["protocol_outbound_units_1e8"]==["100000000"]
    assert record["destination_chain_receipts_verified"] is False
    assert record["realized_pnl_verified"] is False
    assert record["fee_assessed"] is False
    assert record["new_transactions_sent"]==0


@pytest.mark.parametrize("asset",["BTC","SOL","XRP"])
@pytest.mark.parametrize("tamper",[
    lambda r:r["tx"].update(id="B"*64),
    lambda r:r["tx"].update(from_address="0x"+"b"*40),
    lambda r:r["tx"].update(chain="BTC"),
    lambda r:r["tx"].update(memo="=:ETH.USDC:"+DEST+":0/1/0"),
    lambda r:r["out_txs"][0].update(to_address=DEST),
    lambda r:r["out_txs"][0].update(chain="ETH"),
    lambda r:r["out_txs"][0].update(id="not_a_valid_native_hash"),
    lambda r:r["out_txs"][0].update(coins=[{"asset":"BTC.BTC","amount":"0"}]),
    lambda r:r["stages"]["swap_finalised"].update(completed=False),
])
def test_wrong_original_buy_source_or_native_destination_rejected(asset,tamper):
    original=_native_buy_original(asset)
    raw=_native_buy_protocol(asset)
    tamper(raw)
    try:
        result=thor.inspect_original_thorchain_status(original,raw)
    except ValueError:
        return
    # THORChain 'swap_finalised' can be false during an otherwise valid partial
    # report; it is not yet proof of a native payout.
    assert raw["stages"]["swap_finalised"]["completed"] is False
    assert result["protocol_reports_swap_finalized"] is False
    assert result["destination_chain_receipts_verified"] is False

@pytest.mark.parametrize("asset",["BTC","SOL","XRP"])
def test_swap_refund_cannot_be_diverted_from_original_native_owner(asset):
    row=original(asset)
    observed=status(asset,True,True)
    observed["tx"]["memo"]=observed["tx"]["memo"].replace(
        "/"+ADDR[asset]+":","/"+ADDR["BTC"]+"other:")
    with pytest.raises(ValueError):
        thor.inspect_original_thorchain_status(row,observed)


@pytest.mark.parametrize("asset",["BTC","SOL","XRP"])
def test_native_original_refund_cannot_be_omitted_or_changed(asset):
    row=original(asset)
    observed=status(asset,True,True)
    observed["tx"]["memo"]=observed["tx"]["memo"].replace(
        "/"+ADDR[asset]+":",":")
    with pytest.raises(ValueError):
        thor.inspect_original_thorchain_status(row,observed)


@pytest.mark.parametrize("asset",["BTC","SOL","XRP"])
def test_eth_original_refund_cannot_be_diverted_from_buyer(asset):
    row=_native_buy_original(asset)
    observed=_native_buy_protocol(asset)
    observed["tx"]["memo"]=observed["tx"]["memo"].replace(
        "/"+DEST+":","/0x"+"b"*40+":")
    with pytest.raises(ValueError):
        thor.inspect_original_thorchain_status(row,observed)


def test_native_destination_address_is_case_sensitive():
    original_address=ADDR["SOL"]
    assert not thor._valid_memo_destination(
        "=:SOL.SOL:"+original_address.lower()+"/"+DEST,
        "SOL.SOL",original_address,DEST,
        source_chain="ETH",target_chain="SOL")
