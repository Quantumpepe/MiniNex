"""Lost/ambiguous original Ethereum BUY: recover signed source, never re-send."""
import asyncio
from copy import deepcopy
from decimal import Decimal

import httpx
import pytest

from backend import native_eth_buy_unknown_reconcile as m
from backend import native_eth_buy_privy_dispatch as send
from backend.tests.test_native_eth_buy_privy_dispatch import ready,NOW


WALLET_ID="native_test_wallet001"
TX_HASH="0x"+"ab"*32
BLOCK_HASH="0x"+"cd"*32
HEIGHT=102


def source(asset="BTC",stable="USDC",amount="50"):
    order,quote,preflight=ready(asset,stable,amount)
    url,body,nonce,digest=send.bound_original_native_buy_rpc(
        order,quote,preflight,wallet_id=WALLET_ID,now=NOW)
    row={
        "asset":asset,"stable":stable,"side":"BUY",
        "status":"RECONCILIATION_REQUIRED",
        "chain":"ethereum-mainnet","privy_did":"did:privy:owner",
        "intent_key":"native_"+"e"*64,
        "source_wallet":quote["source_wallet"],
        "destination_wallet":quote["destination_wallet"],
        "payload_sha256":order["payload_sha256"],
        "input_amount":Decimal(amount),
        "source_tx_hash":None,
        "source_eth_pending_nonce":Decimal(nonce),
        "source_rpc_request_sha256":digest,
        "source_eth_observed_block_number":Decimal(100),
        "source_eth_scan_next_block":None,
    }
    tx={
        "hash":TX_HASH,"from":quote["source_wallet"],"to":quote["inbound_router"],
        "input":preflight["unsigned_transaction"]["data"],
        "nonce":hex(nonce),"chainId":"0x1","value":"0x0",
        "blockNumber":hex(HEIGHT),"blockHash":BLOCK_HASH,
    }
    return row,tx


@pytest.mark.parametrize("asset,stable,amount",[
    ("BTC","USDC","50"),("SOL","USDT","0.01"),("XRP","USDC","50")])
def test_original_privy_rpc_digest_reconstructed_from_finalized_source(
    asset,stable,amount,
):
    row,tx=source(asset,stable,amount)
    result=m._original_eth_buy_candidate(
        row,tx,wallet_id=WALLET_ID,block_number=HEIGHT,
        block_hash=BLOCK_HASH)
    assert result==TX_HASH
    assert m._original_unknown_eth_buy_row(row)["nonce_exact"]==42
    assert m._original_unknown_eth_buy_row(row)["next_block"]==100


@pytest.mark.parametrize("field,value",[
    ("to","0x"+"1"*40),("input","0xdeadbeef"),
    ("chainId","0x38"),("value","0x1"),
    ("blockHash","0x"+"d"*64),("blockNumber","0x1"),
    ("hash","not_a_hash"),
])
def test_same_nonce_changed_eth_source_never_recovered(field,value):
    row,tx=source()
    tx[field]=value
    with pytest.raises(ValueError):
        m._original_eth_buy_candidate(
            row,tx,wallet_id=WALLET_ID,block_number=HEIGHT,
            block_hash=BLOCK_HASH)


def test_other_wallet_or_other_nonce_does_not_fake_original_source():
    row,tx=source()
    assert m._original_eth_buy_candidate(
        row,{**tx,"from":"0x"+"f"*40},
        wallet_id=WALLET_ID,block_number=HEIGHT,
        block_hash=BLOCK_HASH) is None
    assert m._original_eth_buy_candidate(
        row,{**tx,"nonce":"0x2b"},
        wallet_id=WALLET_ID,block_number=HEIGHT,
        block_hash=BLOCK_HASH) is None
    with pytest.raises(ValueError):
        m._original_eth_buy_candidate(
            {**row,"source_rpc_request_sha256":"a"*64},
            tx,wallet_id=WALLET_ID,block_number=HEIGHT,
            block_hash=BLOCK_HASH)


def test_input_or_owner_mutation_not_accepted_as_ours():
    row,tx=source()
    for change in [
        {"destination_wallet":"wrong"},
        {"input_amount":Decimal("42")},
        {"source_eth_pending_nonce":Decimal("42.1")},
        {"source_eth_observed_block_number":Decimal(0)},
        {"source_eth_scan_next_block":Decimal(99)},
        {"status":"SUBMITTED"},
        {"source_tx_hash":TX_HASH},
        {"chain":"solana-mainnet"},
    ]:
        with pytest.raises(ValueError):
            m._original_eth_buy_candidate(
                {**row,**change},tx,wallet_id=WALLET_ID,
                block_number=HEIGHT,block_hash=BLOCK_HASH)


def test_readonly_rpc_cannot_send_even_with_backend_http_client():
    async def attempt():
        async with httpx.AsyncClient() as client:
            await m._read_only_original_eth_rpc(
                client,"eth_sendTransaction",[],1)
    with pytest.raises(ValueError):
        asyncio.run(attempt())


def test_finalized_original_source_recovered_once_with_no_broadcast(monkeypatch):
    row,tx=source()
    seen=[]
    async def rpc(client,method,params,req_id):
        seen.append((method,params))
        if method=="eth_chainId":return "0x1"
        if method=="eth_getBlockByNumber":
            if params==["finalized",False]:
                return {"number":hex(HEIGHT),"hash":BLOCK_HASH}
            if params==[hex(HEIGHT),False]:
                return {"number":hex(HEIGHT),"hash":BLOCK_HASH}
            if params[1] is True:
                n=int(params[0],16)
                return {"number":hex(n),"hash":BLOCK_HASH,
                        "transactions":[tx] if n==HEIGHT else []}
        if method=="eth_getTransactionReceipt":
            return {"transactionHash":TX_HASH,"blockHash":BLOCK_HASH,
                    "blockNumber":hex(HEIGHT),"status":"0x1"}
        raise AssertionError("Unrecognized original Ethereum read")
    def recovered(original,*,source_tx_hash,block_number):
        assert source_tx_hash==TX_HASH
        assert block_number==HEIGHT
        seen.append(("persist-original","SUBMITTED"))
        return True
    def forbidden(*args,**kwargs):
        raise AssertionError("Do not advance past a recognized exact source")
    monkeypatch.setattr(m,"_read_only_original_eth_rpc",rpc)
    monkeypatch.setattr(m,"_persist_original_recovered_eth_buy",recovered)
    monkeypatch.setattr(m,"_persist_eth_recovery_cursor",forbidden)
    async def run():
        async with httpx.AsyncClient() as client:
            return await m._recover_original_eth_buy_blocks(
                row,wallet_id=WALLET_ID,client=client)
    result=asyncio.run(run())
    assert result["state"]=="RECOVERED_ORIGINAL_ETH_SOURCE"
    assert result["scanned_blocks"]==3
    assert seen[-1]==("persist-original","SUBMITTED")
    assert all(name.startswith("eth_") or name=="persist-original"
               for name,_ in seen)


def test_no_source_in_finalized_blocks_advances_cursor_not_wallet_nonce(monkeypatch):
    row,tx=source()
    calls=[]
    async def rpc(client,method,params,req_id):
        calls.append(method)
        if method=="eth_chainId":return "0x1"
        if method=="eth_getBlockByNumber":
            if params==["finalized",False]:return {"number":"0x66"}
            return {"number":params[0],"hash":BLOCK_HASH,"transactions":[]}
        raise AssertionError("No other read should happen")
    def cursor(original,after):
        assert after==103
        assert original["nonce_exact"]==42
        return True
    monkeypatch.setattr(m,"_read_only_original_eth_rpc",rpc)
    monkeypatch.setattr(m,"_persist_eth_recovery_cursor",cursor)
    async def run():
        async with httpx.AsyncClient() as client:
            return await m._recover_original_eth_buy_blocks(
                row,wallet_id=WALLET_ID,client=client)
    result=asyncio.run(run())
    assert result=={"state":"SCANNED_NO_MATCH","scanned_blocks":3}
    assert calls.count("eth_getBlockByNumber")==4


def test_reverted_finalized_source_never_marked_success(monkeypatch):
    row,tx=source()
    async def rpc(client,method,params,req_id):
        if method=="eth_chainId":return "0x1"
        if method=="eth_getBlockByNumber":
            if params==["finalized",False]:
                return {"number":hex(HEIGHT)}
            if params==[hex(HEIGHT),False]:
                return {"hash":BLOCK_HASH,"number":hex(HEIGHT)}
            n=int(params[0],16)
            return {"number":hex(n),"hash":BLOCK_HASH,
                    "transactions":[tx] if n==HEIGHT else []}
        if method=="eth_getTransactionReceipt":
            return {"transactionHash":TX_HASH,"blockHash":BLOCK_HASH,
                    "blockNumber":hex(HEIGHT),"status":"0x0"}
        raise AssertionError("Unexpected Ethereum RPC")
    def forbidden(*args,**kwargs):
        raise AssertionError("Finalized revert cannot credit source")
    monkeypatch.setattr(m,"_read_only_original_eth_rpc",rpc)
    monkeypatch.setattr(m,"_persist_original_recovered_eth_buy",forbidden)
    async def run():
        async with httpx.AsyncClient() as client:
            return await m._recover_original_eth_buy_blocks(
                row,wallet_id=WALLET_ID,client=client)
    with pytest.raises(ValueError):
        asyncio.run(run())
