"""Read-only canonical recovery of a once-only THOR stablecoin allowance."""
import asyncio
from decimal import Decimal
import hashlib
import json

import httpx
import pytest

from backend import native_thor_approval_unknown_reconcile as m
from backend import native_thor_buy_approval_send as send
from backend.tests.test_native_thor_buy_approval_plan import plan,NOW

WALLET_ID="native_owner_approval_wallet"
OWNER="did:privy:original_owner"
HASH="0x"+"ab"*32
BLOCK="0x"+"bc"*32
HEIGHT=102


def source(asset="BTC",stable="USDC",allowance=0):
    pr=send.bound_original_thor_approval(
        plan(asset=asset,stable=stable,allowance=allowance),
        asset=asset,stable=stable,wallet_id=WALLET_ID,now=NOW)
    key=send._approval_key(
        did=OWNER,asset=asset,stable=stable,signal="strategy_123",
        revision=5,prepared=pr)
    nonce=42
    wire=hashlib.sha256(json.dumps(
        [pr["privy_url"],pr["privy_body"],key,nonce],
        sort_keys=True,separators=(",",":"),ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    row=dict(
        privy_did=OWNER,asset=asset,stable=stable,side="BUY",chain_id=1,
        approval_mode=pr["mode"],wallet_address=pr["wallet"],
        wallet_id=WALLET_ID,token_address=pr["token"],
        router_address=pr["router"],amount_atomic=Decimal(50_000_000),
        idempotency_key=key,request_digest="a"*64,
        status="RECONCILIATION_REQUIRED",tx_hash=None,
        owner_eth_pending_nonce=Decimal(nonce),
        owner_eth_observed_block_number=Decimal(100),
        owner_eth_scan_next_block=None,
        owner_eth_request_wire_sha256=wire)
    tx=dict(
        hash=HASH,**{"from":pr["wallet"],"to":pr["token"]},
        input=pr["source_unsigned_tx"]["data"],
        nonce=hex(nonce),chainId="0x1",value="0x0",
        blockNumber=hex(HEIGHT),blockHash=BLOCK)
    return row,tx


@pytest.mark.parametrize("asset,stable,allowance,mode",[
    ("BTC","USDC",0,"THOR_FINITE"),
    ("SOL","USDT",0,"THOR_FINITE"),
    ("SOL","USDT",10_000_000,"THOR_USDT_ZERO"),
    ("XRP","USDC",0,"THOR_FINITE"),
])
def test_exact_original_approval_recovered_by_owner_nonce_and_privy_digest(
    asset,stable,allowance,mode,
):
    row,tx=source(asset,stable,allowance)
    assert row["approval_mode"]==mode
    assert m._original_thor_approval_candidate(
        row,tx,block_number=HEIGHT,block_hash=BLOCK)==HASH


def test_different_nonce_or_wallet_ignored_without_touching_original_order():
    row,tx=source()
    assert m._original_thor_approval_candidate(
        row,{**tx,"nonce":"0x2b"},
        block_number=HEIGHT,block_hash=BLOCK) is None
    assert m._original_thor_approval_candidate(
        row,{**tx,"from":"0x"+"f"*40},
        block_number=HEIGHT,block_hash=BLOCK) is None


@pytest.mark.parametrize("tamper",[
    ("tx","input","0x095ea7b3"+"f"*128),
    ("tx","to","0x"+"f"*40),
    ("tx","value","0x1"),
    ("tx","chainId","0x38"),
    ("tx","blockNumber","0x60"),
    ("row","wallet_id","different_privy_wallet"),
    ("row","amount_atomic",Decimal(1)),
    ("row","owner_eth_request_wire_sha256","f"*64),
    ("row","token_address","0x"+"f"*40),
    ("row","router_address","0x"+"f"*40),
    ("row","approval_mode","ROUTER02_FINITE"),
    ("row","owner_eth_scan_next_block",Decimal(99)),
])
def test_changed_original_approval_never_passes_same_nonce_proof(tamper):
    row,tx=source()
    table,key,value=tamper
    if table=="tx":tx[key]=value
    else:row[key]=value
    with pytest.raises(ValueError):
        m._original_thor_approval_candidate(
            row,tx,block_number=HEIGHT,block_hash=BLOCK)


def test_unknown_thor_approval_reverted_receipt_is_terminal_not_spendable(monkeypatch):
    row,tx=source("SOL","USDT",10_000_000)
    writes=[]
    async def rpc(client,method,params,req_id):
        if method=="eth_chainId":return "0x1"
        if method=="eth_getBlockByNumber":
            if params==["finalized",False]:
                return {"number":hex(HEIGHT)}
            if params==[hex(HEIGHT),False]:
                return {"hash":BLOCK,"number":hex(HEIGHT)}
            n=int(params[0],16)
            return {"number":hex(n),"hash":BLOCK,
                    "transactions":[tx] if n==HEIGHT else []}
        if method=="eth_getTransactionReceipt":
            return {"transactionHash":HASH,"blockHash":BLOCK,
                    "blockNumber":hex(HEIGHT),"status":"0x0"}
        raise AssertionError("Only immutable Ethereum chain reads are allowed")
    def persist(original,*,tx_hash,block_number,reverted):
        writes.append((tx_hash,block_number,reverted))
        return True
    monkeypatch.setattr(m,"_read_only_original_eth_rpc",rpc)
    monkeypatch.setattr(m,"_persist_unknown_thor_finality",persist)
    async def run():
        async with httpx.AsyncClient() as client:
            return await m._recover_unknown_thor_blocks(row,client=client)
    observed=asyncio.run(run())
    assert observed=={"state":"FINALIZED_REVERTED"}
    assert writes==[(HASH,HEIGHT,True)]


def test_missing_unknown_allowance_source_advances_only_finalized_cursor(monkeypatch):
    row,tx=source()
    written=[]
    async def rpc(client,method,params,req_id):
        if method=="eth_chainId":return "0x1"
        if method=="eth_getBlockByNumber":
            if params==["finalized",False]:return {"number":"0x66"}
            return {"number":params[0],"hash":BLOCK,"transactions":[]}
        raise AssertionError("No send or receipt by hash when source is absent")
    def cursor(original,after):
        written.append(after)
        return True
    monkeypatch.setattr(m,"_read_only_original_eth_rpc",rpc)
    monkeypatch.setattr(m,"_persist_unknown_thor_cursor",cursor)
    async def run():
        async with httpx.AsyncClient() as client:
            return await m._recover_unknown_thor_blocks(row,client=client)
    assert asyncio.run(run())=={"state":"SCANNED_NO_MATCH"}
    assert written==[103]


def test_recovery_does_not_broadcast_or_infer_approval_from_pending_view():
    row,tx=source()
    bad={**row,"status":"FINALIZED_SUCCESS"}
    with pytest.raises(ValueError):
        m._original_unknown_thor_approval_row(bad)
    with pytest.raises(ValueError):
        m._original_unknown_thor_approval_row({
            **row,"owner_eth_pending_nonce":Decimal("42.5")})
