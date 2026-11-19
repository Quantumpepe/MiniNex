"""No external LINK/UNI wallet transfers may be hidden in fee accounting."""
import asyncio
from copy import deepcopy

import httpx
import pytest

from backend.shared_evm_wallet_token_audit import (
    validate_owner_token_transfer_audit, audit_owner_erc20_token_inventory,
    _TRANSFER_TOPIC,
)
from backend.shared_evm_swap_receipts import _pair

WALLET="0x"+"ab"*20
POOL="0x"+"cd"*20
BUY="0x"+"11"*32
SELL="0x"+"22"*32
UNIT=10**18


def sample(asset):
    pair=_pair(asset,"USDC","BUY")
    fills=[
        dict(mode="INDEPENDENT_FINALIZED_EVM_EXECUTION",
             onchain_finalized=True,asset=asset,wallet_address=WALLET,
             tx_hash=BUY,block_number=100,block_hash="0x"+"ef"*32,transaction_index=0,side="BUY",
             destination_amount="2",source_amount="100"),
        dict(mode="INDEPENDENT_FINALIZED_EVM_EXECUTION",
             onchain_finalized=True,asset=asset,wallet_address=WALLET,
             tx_hash=SELL,block_number=200,block_hash="0x"+"ef"*32,transaction_index=0,side="SELL",
             source_amount="1",destination_amount="75"),
    ]
    def log(tx, idx, sender, receiver, amount, block):
        return dict(address=pair["output_token"],
                    topics=[_TRANSFER_TOPIC,
                            "0x"+f"{int(sender,16):064x}",
                            "0x"+f"{int(receiver,16):064x}"],
                    data="0x"+f"{amount:064x}",
                    blockNumber=hex(block), transactionIndex="0x0",
                    logIndex=hex(idx), transactionHash=tx,
                    blockHash="0x"+"ef"*32,removed=False)
    incoming=[log(BUY,0,POOL,WALLET,2*UNIT,100)]
    outgoing=[log(SELL,0,WALLET,POOL,UNIT,200)]
    return fills,incoming,outgoing,log


@pytest.mark.parametrize("asset",["LINK","UNI","ARB"])
def test_source_complete_wallet_log_audit_matches_exact_fifo(asset):
    fills,inc,out,_=sample(asset)
    report=validate_owner_token_transfer_audit(
        asset=asset,wallet=WALLET,fills=fills,start_block=100,end_block=200,
        pre_balance_wei=0,ending_balance_wei=UNIT,incoming=inc,outgoing=out)
    assert report["matched_mininex_swap_transfers"]==2
    assert report["external_inventory_reconciled"] is True
    assert report["no_other_execution_costs_verified"] is False
    assert report["fee_assessed"] is False


@pytest.mark.parametrize("damage",[
    lambda f,i,o,l:i.append(l("0x"+"33"*32,2,POOL,WALLET,UNIT,150)),
    lambda f,i,o,l:i[0].update(data="0x"+f"{3*UNIT:064x}"),
    lambda f,i,o,l:o.clear(),
    lambda f,i,o,l:o[0].update(removed=True),
    lambda f,i,o,l:i[0].update(address="0x"+"aa"*20),
    lambda f,i,o,l:o[0].update(transactionHash="0x"+"33"*32),
    lambda f,i,o,l:o[0].update(blockHash="0x"+"aa"*32),
    lambda f,i,o,l:i[0].update(blockNumber="0xff"),
    lambda f,i,o,l:i.append(deepcopy(i[0])),
    lambda f,i,o,l:i[0].update(topics=[_TRANSFER_TOPIC,
        "0x"+f"{int(WALLET,16):064x}", "0x"+f"{int(WALLET,16):064x}"]),
    lambda f,i,o,l:f[1].update(onchain_finalized=False),
])
def test_unreported_external_transfer_breaks_full_inventory_provenance(damage):
    fills,inc,out,log=sample("LINK")
    damage(fills,inc,out,log)
    with pytest.raises((ValueError,TypeError,KeyError)):
        validate_owner_token_transfer_audit(
            asset="LINK",wallet=WALLET,fills=fills,start_block=100,end_block=200,
            pre_balance_wei=0,ending_balance_wei=UNIT,
            incoming=inc,outgoing=out)


def test_unknown_original_inventory_and_last_balance_cannot_pass():
    fills,inc,out,_=sample("UNI")
    for pre,post in ((UNIT,UNIT),(0,2*UNIT)):
        with pytest.raises(ValueError):
            validate_owner_token_transfer_audit(
                asset="UNI",wallet=WALLET,fills=fills,
                start_block=100,end_block=200,
                pre_balance_wei=pre,ending_balance_wei=post,
                incoming=inc,outgoing=out)


def test_complete_transfer_pages_and_historical_balances_queried_from_chain():
    fills,inc,out,_=sample("LINK")
    requests=[]
    async def rpc(req):
        body=__import__("json").loads(req.content)
        requests.append(body)
        method=body["method"]
        if method=="eth_chainId":
            response=hex(1)
        elif method=="eth_getBlockByNumber":
            response={"number":"0xc9"}
        elif method=="eth_getLogs":
            topics=body["params"][0]["topics"]
            response=inc if topics[1] is None else out
        elif method=="eth_call":
            block=body["params"][1]
            response="0x0" if block=="0x63" else hex(UNIT)
        else:
            raise AssertionError(method)
        return httpx.Response(200,json={
            "jsonrpc":"2.0","id":body["id"],"result":response})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(rpc)) as client:
            return await audit_owner_erc20_token_inventory(
                asset="LINK",wallet=WALLET,fills=fills,client=client)
    result=asyncio.run(run())
    assert result["matched_mininex_swap_transfers"]==2
    assert len([x for x in requests if x["method"]=="eth_getLogs"])==2
    assert len([x for x in requests if x["method"]=="eth_call"])==2
