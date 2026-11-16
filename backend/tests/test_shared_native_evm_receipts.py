"""One finalized WETH9-native conversion verifier for all four EVM native coins."""
from copy import deepcopy
import asyncio

import pytest
from fastapi import HTTPException

from backend import shared_native_evm_receipts as v
from backend.shared_evm_swap_receipts import _pair

WALLET="0x"+"ab"*20
HASH="0x"+"cd"*32
BLOCK="0x"+"ef"*32
AMOUNT=10**17
STABLE={"ETH":"USDC","BNB":"USDT","POL":"USDC","AVAX":"USDC"}


def sample(asset,operation="DEPOSIT",status="0x1"):
    pair=_pair(asset,STABLE[asset],"SELL")
    wrapped=pair["source_token"]
    native=operation=="DEPOSIT"
    tx={
        "hash":HASH,"from":WALLET,"to":wrapped,
        "chainId":hex(pair["chain_id"]),
        "value":hex(AMOUNT if native else 0),
        "input":"0xd0e30db0" if native else "0x2e1a7d4d"+f"{AMOUNT:064x}",
        "blockHash":BLOCK,"blockNumber":"0x64",
    }
    event=v._DEPOSIT_TOPIC if native else v._WITHDRAW_TOPIC
    receipt={
        "transactionHash":HASH,"from":WALLET,"to":wrapped,
        "blockHash":BLOCK,"blockNumber":"0x64",
        "status":status,"gasUsed":"0x5208",
        "effectiveGasPrice":"0x3b9aca00",
        "logs":[{"address":wrapped,
                 "topics":[event,"0x"+"0"*24+WALLET[2:]],
                 "data":"0x"+f"{AMOUNT:064x}"}]
                if status=="0x1" else [],
    }
    return {
        "asset":asset,"operation":operation,"wallet":WALLET,
        "chain_id":hex(pair["chain_id"]),
        "transaction":tx,"receipt":receipt,"latest":"0x78",
        "finalized":{"number":"0x6e"},
    }


@pytest.mark.parametrize("asset",["ETH","BNB","POL","AVAX"])
@pytest.mark.parametrize("operation",["DEPOSIT","WITHDRAW"])
def test_one_actual_event_verifier_for_native_evm_networks(asset,operation):
    kwargs=sample(asset,operation)
    r=v.inspect_finalized_native_evm_conversion(HASH,**kwargs)
    assert r["asset"]==asset
    assert r["chain_id"]==_pair(asset,STABLE[asset],"SELL")["chain_id"]
    assert r["state"]=="FINALIZED_NATIVE_CONVERSION"
    assert r["confirmed_native_conversion"] is True
    assert r["amount_native"]=="0.1"
    assert r["network_fee_native"]=="0.000021"
    assert r["wallet_address"]==WALLET
    assert r["full_native_trade_cycle_settled"] is False
    assert r["linked_trade_order"] is False
    assert r["execution_enabled"] is False


@pytest.mark.parametrize("asset",["ETH","BNB","POL","AVAX"])
def test_pending_included_and_reverted_never_claim_complete_trade(asset):
    kwargs=sample(asset)
    kwargs["finalized"]={"number":"0x63"}
    included=v.inspect_finalized_native_evm_conversion(HASH,**kwargs)
    assert included["state"]=="INCLUDED_SUCCESS"
    assert included["confirmed_native_conversion"] is False
    kwargs=sample(asset)
    kwargs["receipt"]=None
    kwargs["transaction"].update(blockHash=None,blockNumber=None)
    pending=v.inspect_finalized_native_evm_conversion(HASH,**kwargs)
    assert pending["state"]=="PENDING"
    kwargs=sample(asset,status="0x0")
    reverted=v.inspect_finalized_native_evm_conversion(HASH,**kwargs)
    assert reverted["state"]=="FINALIZED_REVERTED"
    assert reverted["confirmed_native_conversion"] is False


@pytest.mark.parametrize("mutation",[
    lambda k:k.update(chain_id="0x1"),
    lambda k:k["transaction"].update(**{"from":"0x"+"aa"*20}),
    lambda k:k["transaction"].update(to="0x"+"aa"*20),
    lambda k:k["transaction"].update(input="0xdeadbeef"),
    lambda k:k["transaction"].update(value="0x0"),
    lambda k:k["receipt"].update(blockHash="0x"+"ee"*32),
    lambda k:k["receipt"].update(status="0x2"),
    lambda k:k["receipt"]["logs"][0].update(data="0x"+f"{AMOUNT+1:064x}"),
    lambda k:k["receipt"]["logs"][0].update(address="0x"+"aa"*20),
    lambda k:k["receipt"]["logs"].append(deepcopy(k["receipt"]["logs"][0])),
    lambda k:k.update(finalized={"number":"0x90"}),
])
def test_native_conversion_wrong_chain_amount_receipt_or_event_rejected(mutation):
    kwargs=sample("AVAX")
    mutation(kwargs)
    with pytest.raises(ValueError):
        v.inspect_finalized_native_evm_conversion(HASH,**kwargs)


@pytest.mark.parametrize("bad",["","0xdeadbeef","0x"+"zz"*32,None])
def test_invalid_hash_rejected_before_owner_lookup(bad):
    with pytest.raises(HTTPException) as e:
        asyncio.run(v.check_user_native_evm_conversion(
            "did:privy:owner",bad,asset="POL",operation="DEPOSIT"))
    assert e.value.status_code==422


def test_read_only_native_conversion_api_requires_privy_bearer():
    import inspect
    from backend.app import app
    r=next(r for r in app.routes if
      r.path=="/api/live/evm/native-conversion-receipt/{asset}/{tx_hash}")
    body=inspect.getsource(r.endpoint)
    assert "verify_privy_bearer" in body
    assert "check_user_native_evm_conversion" in body
    assert r.methods=={"GET"}
