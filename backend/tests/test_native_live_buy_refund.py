"""Actual Ethereum-mainnet stablecoin refund to original BTC/SOL/XRP BUY owner."""
import pytest
from fastapi import HTTPException
from backend import native_live_buy_refund as m
from backend.live_eth_quotes import STABLES
from backend.eth_router02_receipts import _TRANSFER_TOPIC

DID="did:privy:buyer"
ETH="0x"+"a"*40
TX="0x"+"b"*64
OUT="c"*64
BLOCK="0x"+"d"*64
KEY="native_"+"1"*64

def original():
    return dict(privy_did=DID,asset="BTC",intent_key=KEY,
        side="BUY",stable="USDC",input_amount="100",
        status="SOURCE_FINALIZED",source_wallet=ETH,
        destination_wallet="bc1qowner",
        source_tx_hash=TX,payload_sha256="2"*64,
        source_proof_sha256="3"*64)

def protocol():
    return dict(mode="THORCHAIN_ORIGINAL_TRANSACTION_READ_ONLY",
        asset="BTC",side="BUY",source_tx_hash=TX,
        source_wallet_original=True,destination_owner_bound=True,
        protocol_inbound_observed=True,protocol_inbound_finalized=True,
        refund_original_source_chain="ETH",refund_independently_finalized=False,
        destination_chain_receipts_verified=False,
        realized_pnl_verified=False,fee_assessed=False,
        retry_permitted=False,new_transactions_sent=0,
        protocol_outbound_hashes=[],
        protocol_refund_hashes=[OUT],
        protocol_refund_units_1e8=["9900000000"])

def ethereum():
    value=99000000
    sender="b"*40
    log=dict(address=STABLES["USDC"],
        topics=[_TRANSFER_TOPIC,"0x"+"0"*24+sender,
                "0x"+"0"*24+"a"*40],
        data="0x"+format(value,"064x"),
        transactionHash="0x"+OUT,blockHash=BLOCK,
        removed=False)
    return dict(transactionHash="0x"+OUT,status="0x1",
        blockHash=BLOCK,blockNumber="0x64",logs=[log])


@pytest.mark.parametrize("change",[
    lambda o,p,r: p.update(protocol_outbound_hashes=["a"*64]),
    lambda o,p,r: p.update(protocol_refund_units_1e8=["9900000001"]),
    lambda o,p,r: p.update(protocol_refund_hashes=["a"*64]),
    lambda o,p,r: r.update(status="0x0"),
    lambda o,p,r: r["logs"][0].update(address=STABLES["USDT"]),
    lambda o,p,r: r["logs"][0].update(data="0x"+format(99999999,"064x")),
    lambda o,p,r: r["logs"][0]["topics"].__setitem__(2,"0x"+"0"*24+"f"*40),
    lambda o,p,r: o.update(status="PREPARED"),
    lambda o,p,r: o.update(input_amount="98"),
])
def test_refund_needs_original_amount_protocol_hash_and_real_wallet_credit(change):
    o,p,r=original(),protocol(),ethereum()
    change(o,p,r)
    with pytest.raises((ValueError,TypeError,KeyError)):
        m.inspect_original_eth_buy_refund(
            o,p,chain_id="0x1",receipt=r,
            finalized={"number":"0x65"},
            included_block={"number":"0x64","hash":BLOCK})


def test_eth_original_buy_refund_requires_finalized_owner_erc20_credit():
    proof=m.inspect_original_eth_buy_refund(
        original(),protocol(),chain_id="0x1",receipt=ethereum(),
        finalized={"number":"0x65"},
        included_block={"number":"0x64","hash":BLOCK})
    assert proof["refund_amount_atomic"]=="99000000"
    assert proof["source_chain_refund_finalized"] is True
    assert proof["native_buy_settlement_complete"] is False
    assert proof["fee_assessed"] is False
    assert proof["new_transactions_sent"]==0


def test_durable_original_buy_refund_cannot_be_booked_twice():
    proof=m.inspect_original_eth_buy_refund(
        original(),protocol(),chain_id="0x1",receipt=ethereum(),
        finalized={"number":"0x65"},
        included_block={"number":"0x64","hash":BLOCK})
    class Cursor:
        def __init__(self):self.calls=[]
        def execute(self,sql,args):
            self.calls.append((sql,args))
        def fetchone(self):
            return dict(status="DESTINATION_FINALIZED")
    cur=Cursor()
    with pytest.raises(HTTPException):
        m._persist_original_native_buy_refund_locked(
            cur,original=original(),verified=proof)
    assert len(cur.calls)==1
