"""Native source wallet gas and original archive state must conserve value."""
from __future__ import annotations

from copy import deepcopy
import pytest

from backend.shared_evm_native_gas_balance_proof import (
    verify_owner_native_balance_and_gas,
)

WALLET="0x"+"ab"*20
WRAPPED="0x"+"cd"*20
HASH="0x"+"ee"*32
TX_BUY="0x"+"11"*32
TX_SELL="0x"+"22"*32
UNIT=10**18
GAS=100000*10**9


def fixture():
    def event(tx,height,path,frm,to,value):
        return {
            "type":"call","transactionHash":tx,
            "blockNumber":height,"blockHash":HASH,
            "traceAddress":path,"transactionPosition":0,
            "action":{"from":frm,"to":to,"callType":"call","value":hex(value)},
        }
    traces=[
        event(TX_BUY,100,[],WALLET,WRAPPED,0),
        event(TX_BUY,100,[0],WRAPPED,WALLET,2*UNIT),
        event(TX_SELL,200,[],WALLET,WRAPPED,UNIT),
    ]
    def receipt(tx,height):
        return dict(transactionHash=tx,from_=WALLET,to=WRAPPED,
                    blockHash=HASH,blockNumber=hex(height),
                    transactionIndex="0x0",gasUsed="0x186a0",
                    effectiveGasPrice=hex(10**9),status="0x1")
    receipts={}
    for tx,height in ((TX_BUY,100),(TX_SELL,200)):
        r=receipt(tx,height)
        r["from"]=r.pop("from_")
        receipts[tx]=r
    kwargs={
        "wallet":WALLET,
        "traces":traces,"canonical_blocks":{100:HASH,200:HASH},
        "receipts":receipts,
        "start_block":100,"end_block":200,
        "balance_before":hex(10*UNIT),
        "balance_after":hex(11*UNIT-2*GAS),
    }
    return kwargs


def test_exact_native_balance_delta_includes_all_value_and_paid_gas():
    report=verify_owner_native_balance_and_gas(**fixture())
    assert report["observed_interval_native_balance_conserved"] is True
    assert report["original_gas_receipts_verified"] is True
    assert report["total_observed_native_in_wei"]==str(2*UNIT)
    assert report["total_observed_native_out_wei"]==str(UNIT)
    assert report["total_verified_sender_gas_wei"]==str(2*GAS)
    assert report["observed_wallet_sender_transaction_count"]==2
    assert report["wallet_lifetime_and_acquisition_basis_proven"] is False
    assert report["five_percent_hwm_writable"] is False


@pytest.mark.parametrize("damage",[
    lambda d:d.update(balance_after=hex(11*UNIT)),
    lambda d:d.update(balance_before="0x0"),
    lambda d:d["receipts"].pop(TX_BUY),
    lambda d:d["receipts"][TX_BUY].update(status="BAD"),
    lambda d:d["receipts"][TX_BUY].update(gasUsed="0x0"),
    lambda d:d["receipts"][TX_BUY].update(effectiveGasPrice="0x0"),
    lambda d:d["receipts"][TX_BUY].update(
        blockHash="0x"+"44"*32),
    lambda d:d["receipts"][TX_SELL].update(
        transactionIndex="0x1"),
    lambda d:d["canonical_blocks"].update({100:"0x"+"44"*32}),
    lambda d:d["traces"].append(deepcopy(d["traces"][1])),
    lambda d:d["traces"][1]["action"].update(
        value=hex(UNIT)),
    lambda d:d["traces"][2]["action"].update(
        callType="delegatecall"),
    lambda d:d["traces"][0].update(transactionHash=TX_SELL),
])
def test_native_wallet_gas_or_archive_mismatch_never_proves_balance(damage):
    args=fixture()
    damage(args)
    with pytest.raises((ValueError,TypeError)):
        verify_owner_native_balance_and_gas(**args)


def test_unrelated_user_transaction_gas_is_counted_and_not_silently_ignored():
    kwargs=fixture()
    new_tx="0x"+"ff"*32
    extra=deepcopy(kwargs["traces"][0])
    extra["transactionHash"]=new_tx
    extra["blockNumber"]=150
    kwargs["traces"].append(extra)
    kwargs["canonical_blocks"][150]=HASH
    new_receipt=deepcopy(kwargs["receipts"][TX_BUY])
    new_receipt.update(transactionHash=new_tx,blockNumber=hex(150))
    kwargs["receipts"][new_tx]=new_receipt
    kwargs["balance_after"]=hex(11*UNIT-3*GAS)
    report=verify_owner_native_balance_and_gas(**kwargs)
    assert report["total_verified_sender_gas_wei"]==str(3*GAS)
    assert report["five_percent_hwm_writable"] is False
    kwargs["receipts"].pop(new_tx)
    with pytest.raises(ValueError):
        verify_owner_native_balance_and_gas(**kwargs)


def test_archived_wallet_nonce_proves_all_sender_transactions_were_seen():
    original=fixture()
    original.update(sender_nonce_before="0x9",sender_nonce_after="0xb")
    proof=verify_owner_native_balance_and_gas(**original)
    assert proof["archived_sender_nonce_delta_verified"] is True
    assert proof["all_sender_transactions_in_observed_interval_reconciled"] is True
    assert proof["wallet_lifetime_and_acquisition_basis_proven"] is False
    assert proof["five_percent_hwm_writable"] is False


@pytest.mark.parametrize("before,after",[
    ("0x9","0xa"),  # one gas-paying original transaction hidden
    ("0x9","0xc"),  # a third zero-value send missed by trace_filter
    ("0xb","0x9"),  # reorg or wrong archival state
    ("0x9",None),   # incomplete archive response
    (None,"0xb"),
    ("0xz","0xb"),  # noncanonical nonce
])
def test_native_nonce_mismatch_cannot_certify_full_owner_sender_gas(before,after):
    original=fixture()
    original.update(sender_nonce_before=before,sender_nonce_after=after)
    with pytest.raises(ValueError):
        verify_owner_native_balance_and_gas(**original)
