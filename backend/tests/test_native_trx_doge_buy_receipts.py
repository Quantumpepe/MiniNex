"""Original TRON/DOGE destination receipts: real owner credit, no paper fill."""
from copy import deepcopy
import pytest

from backend.native_coin_balances import _decode_b58,_BASE58
from backend.native_trx_doge_buy_receipts import (
    inspect_original_tron_credit,inspect_original_doge_credit,
)
from backend import native_live_buy_destination as native_buy
from backend import native_thor_transaction_status as thor
from backend.tests.test_native_trx_doge_buy_quote import (
    owner_coin,THOR_ASSETS,DID,ETH,
)

TX="f"*64


def trx_receipt(owner,amount):
    original=_decode_b58(owner,_BASE58)[:21].hex().upper()
    return {
        "transaction":{
            "txID":TX,
            "ret":[{"contractRet":"SUCCESS"}],
            "raw_data":{"contract":[{"type":"TransferContract",
                "parameter":{"value":{
                    "owner_address":"41"+"a"*40,
                    "to_address":original,
                    "amount":amount,
                }}}]},
        },
        "info":{"id":TX,"blockNumber":72000000,
                "receipt":{"result":"SUCCESS"}},
    }


def doge_receipt(owner,amount):
    return {
        "hash":TX,"block_hash":"e"*64,
        "block_height":6200000,"confirmations":6,
        "double_spend":False,"vin_sz":1,"vout_sz":2,
        "inputs":[{"addresses":["Dsource_native_pool_wallet"]}],
        "outputs":[{"addresses":[owner],"value":amount},
                   {"addresses":["Dchange_address"],"value":5}],
    }


@pytest.mark.parametrize("asset,units",[
    ("TRX",25_000_000),("DOGE",300_000_000),
])
def test_original_native_buy_requires_exact_final_chain_credit(asset,units):
    owner=owner_coin(asset)
    receipt=(trx_receipt(owner,units) if asset=="TRX"
             else doge_receipt(owner,units))
    if asset=="TRX":
        result=inspect_original_tron_credit(owner,TX,units,receipt)
        assert result["chain_finality"]=="TRON_SOLIDIFIED"
    else:
        result=inspect_original_doge_credit(owner,TX,units,receipt)
        assert result["chain_finality"]=="DOGE_SIX_CONFIRMED"
    assert result["credited_atomic"]==str(units)


def test_tron_rejects_wrong_owner_amount_unsolidified_or_failed_transfer():
    owner=owner_coin("TRX")
    receipt=trx_receipt(owner,25_000_000)
    for mutate in [
        lambda r:r["transaction"]["ret"][0].update(contractRet="REVERT"),
        lambda r:r["transaction"]["raw_data"]["contract"][0].update(
            type="TriggerSmartContract"),
        lambda r:r["transaction"]["raw_data"]["contract"][0]["parameter"]["value"].update(
            to_address="41"+"f"*40),
        lambda r:r["transaction"]["raw_data"]["contract"][0]["parameter"]["value"].update(
            amount=25_000_001),
        lambda r:r["info"].update(blockNumber=0),
        lambda r:r["info"].update(id="0"*64),
        lambda r:r["info"]["receipt"].update(result="FAILED"),
    ]:
        corrupted=deepcopy(receipt)
        mutate(corrupted)
        with pytest.raises(ValueError):
            inspect_original_tron_credit(owner,TX,25_000_000,corrupted)


def test_dogecoin_requires_all_native_outputs_and_six_confirmations():
    owner=owner_coin("DOGE")
    receipt=doge_receipt(owner,300_000_000)
    for mutate in [
        lambda r:r.update(confirmations=5),
        lambda r:r.update(double_spend=True),
        lambda r:r["inputs"][0].update(addresses=[owner]),
        lambda r:r.update(vout_sz=3),
        lambda r:r["outputs"][0].update(value=299_999_999),
        lambda r:r["outputs"][0].update(addresses=["Dother_address"]),
        lambda r:r["outputs"][0].pop("addresses"),
        lambda r:r.update(block_height=0),
        lambda r:r.update(hash="0"*64),
    ]:
        corrupted=deepcopy(receipt)
        mutate(corrupted)
        with pytest.raises(ValueError):
            inspect_original_doge_credit(owner,TX,300_000_000,corrupted)


@pytest.mark.parametrize("asset,units,proto_units",[
    ("TRX",25_000_000,"2500000000"),
    ("DOGE",300_000_000,"300000000"),
])
def test_original_native_buy_closes_only_when_protocol_and_chain_agree(
    asset,units,proto_units,
):
    owner=owner_coin(asset)
    original={
        "privy_did":DID,"asset":asset,
        "intent_key":"native_"+"a"*64,
        "payload_sha256":"a"*64,
        "source_proof_sha256":"b"*64,
        "source_tx_hash":"0x"+"e"*64,
        "source_wallet":ETH,"destination_wallet":owner,
        "side":"BUY","stable":"USDC",
        "status":"SOURCE_FINALIZED",
    }
    observed={
        "tx":{
            "id":"E"*64,"chain":"ETH","from_address":ETH,
            "memo":"=:"+THOR_ASSETS[asset][0]+":"+owner+"/"+ETH+":0/1/0",
            "coins":[{"asset":THOR_ASSETS["USDC"][0],"amount":"5000000000"}],
        },
        "stages":{
            "inbound_observed":{"completed":True},
            "inbound_finalised":{"completed":True},
            "swap_finalised":{"completed":True},
        },
        "out_txs":[{
            "id":TX.upper(),"chain":("TRON" if asset=="TRX" else "DOGE"),
            "to_address":owner,
            "coins":[{"asset":THOR_ASSETS[asset][0],"amount":proto_units}],
        }],
    }
    proto=thor.inspect_original_thorchain_status(original,observed)
    assert proto["destination_chain_receipts_verified"] is False
    receipt=(trx_receipt(owner,units) if asset=="TRX"
             else doge_receipt(owner,units))
    proved=native_buy.inspect_original_native_buy_destination(
        original,proto,receipt)
    assert proved["native_buy_settlement_complete"] is True
    assert proved["destination_wallet_original"] is True
    assert proved["destination_received_atomic"]==str(units)
    assert proved["trading_profit_verified"] is False
    assert proved["fee_assessed"] is False
    with pytest.raises(ValueError):
        native_buy.inspect_original_native_buy_destination(
            original,proto,
            trx_receipt(owner,units+1) if asset=="TRX"
            else doge_receipt(owner,units+1))
    with pytest.raises(ValueError):
        native_buy.inspect_original_native_buy_destination(
            original,{**proto,"protocol_outbound_hashes":["a"*64]},receipt)
