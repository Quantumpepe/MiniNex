"""Native BTC/SOL/XRP SELL destination: original journal, THOR and ERC20 proof.

No private key, mainnet spend, fee assessment or live order in these tests.
"""
from copy import deepcopy
import asyncio
import inspect

import pytest
from fastapi import HTTPException

from backend import native_live_destination_finality as dest
from backend.live_eth_quotes import STABLES
from backend.storage import SCHEMA

DID="did:privy:original_native_owner"
WALLET="0x"+"a"*40
SENDER="0x"+"b"*40
TX="e"*64
BLOCK="c"*64
SOURCE={"BTC":"a"*64,"SOL":"2"*88,"XRP":"F"*64}


def original(asset="BTC",stable="USDC"):
    return {
        "privy_did":DID,"asset":asset,
        "intent_key":"native_"+"d"*64,
        "payload_sha256":"a"*64,"source_proof_sha256":"b"*64,
        "source_tx_hash":SOURCE[asset],
        "source_wallet":"original_"+asset,
        "destination_wallet":WALLET,"stable":stable,
        "side":"SELL","status":"SOURCE_FINALIZED",
    }


def protocol(row):
    return {
        "mode":"THORCHAIN_ORIGINAL_TRANSACTION_READ_ONLY",
        "asset":row["asset"],"source_tx_hash":row["source_tx_hash"],
        "source_wallet_original":True,"destination_owner_bound":True,
        "protocol_inbound_observed":True,"protocol_inbound_finalized":True,
        "protocol_reports_swap_finalized":True,
        "protocol_outbound_hashes":[TX],
        "destination_chain_receipts_verified":False,
        "realized_pnl_verified":False,"fee_assessed":False,
        "retry_permitted":False,"new_transactions_sent":0,
    }


def topic(address):
    return "0x"+"0"*24+address[2:].lower()


def transaction(row,amount=1234567):
    return {
        "transactionHash":"0x"+TX,
        "blockHash":"0x"+BLOCK,
        "blockNumber":"0x64",
        "status":"0x1",
        "logs":[{
            "address":STABLES[row["stable"]],
            "topics":[dest._TRANSFER_TOPIC,topic(SENDER),topic(WALLET)],
            "data":"0x"+format(amount,"064x"),
            "transactionHash":"0x"+TX,
            "blockHash":"0x"+BLOCK,"removed":False,
        }],
    }


def inspect(asset="BTC",stable="USDC",changes=None):
    row=original(asset,stable)
    proto=protocol(row)
    receipt=transaction(row)
    finalized={"number":"0x70","hash":"0x"+"f"*64}
    block={"number":"0x64","hash":"0x"+BLOCK}
    chain="0x1"
    if changes:
        fn=changes
        chain=fn(row,proto,receipt,finalized,block) or chain
    return dest.inspect_original_eth_stable_destination(
        row,proto,chain_id=chain,receipt=receipt,
        finalized=finalized,included_block=block,
    )


@pytest.mark.parametrize("asset,stable",[
    ("BTC","USDC"),("BTC","USDT"),
    ("SOL","USDC"),("SOL","USDT"),("XRP","USDC"),
])
def test_all_native_sell_coins_require_finalized_real_owner_erc20_credit(asset,stable):
    proof=inspect(asset,stable)
    assert proof["asset"]==asset
    assert proof["stable"]==stable
    assert proof["destination_tx_hash"]==TX
    assert proof["ethereum_finalized_token_credit"] is True
    assert proof["protocol_swap_finalized"] is True
    assert proof["native_sell_settlement_complete"] is True
    assert proof["native_buy_settlement_complete"] is False
    assert proof["destination_received_atomic"]=="1234567"
    assert proof["destination_received_amount"]=="1.234567"
    assert proof["destination_token_contract"]==STABLES[stable].lower()
    assert proof["destination_proof_sha256"]!=proof["original_order_sha256"]
    assert len(proof["destination_proof_sha256"])==64
    assert proof["trading_profit_verified"] is False
    assert proof["fee_assessed"] is False
    assert proof["can_submit"] is False
    assert proof["new_transactions_sent"]==0


@pytest.mark.parametrize("change",[
    lambda row,p,r,f,b: row.update(status="SUBMITTED"),
    lambda row,p,r,f,b: row.update(side="BUY"),
    lambda row,p,r,f,b: row.update(source_proof_sha256=None),
    lambda row,p,r,f,b: row.update(destination_wallet="0x"+"c"*40),
    lambda row,p,r,f,b: row.update(asset="DOGE"),
    lambda row,p,r,f,b: row.update(intent_key="native_"+("z"*64)),
    lambda row,p,r,f,b: row.update(stable="INVALID"),
    lambda row,p,r,f,b: p.update(source_tx_hash="c"*64),
    lambda row,p,r,f,b: p.update(destination_owner_bound=False),
    lambda row,p,r,f,b: p.update(protocol_inbound_finalized=False),
    lambda row,p,r,f,b: p.update(protocol_reports_swap_finalized=False),
    lambda row,p,r,f,b: p.update(destination_chain_receipts_verified=True),
    lambda row,p,r,f,b: p.update(fee_assessed=True),
    lambda row,p,r,f,b: p.update(protocol_outbound_hashes=[]),
    lambda row,p,r,f,b: p.update(protocol_outbound_hashes=[TX,"f"*64]),
    lambda row,p,r,f,b: p.update(protocol_outbound_hashes=["z"*64]),
    lambda row,p,r,f,b: r.update(transactionHash="0x"+"0"*64),
    lambda row,p,r,f,b: r.update(status="0x0"),
    lambda row,p,r,f,b: r.update(blockHash="0x"+"1"*64),
    lambda row,p,r,f,b: f.update(number="0x63"),
    lambda row,p,r,f,b: b.update(hash="0x"+"1"*64),
    lambda row,p,r,f,b: r["logs"][0].update(address=STABLES["USDT"]),
    lambda row,p,r,f,b: r["logs"][0].update(
        topics=[dest._TRANSFER_TOPIC,topic(SENDER),topic(SENDER)]),
    lambda row,p,r,f,b: r["logs"][0].update(
        topics=[dest._TRANSFER_TOPIC,topic(WALLET),topic(WALLET)]),
    lambda row,p,r,f,b: r["logs"][0].update(data="0x"+"0"*64),
    lambda row,p,r,f,b: r["logs"][0].update(data="0x0001"),
    lambda row,p,r,f,b: r["logs"][0].update(transactionHash="0x"+"b"*64),
    lambda row,p,r,f,b: r["logs"][0].update(removed=True),
    lambda row,p,r,f,b: r.update(logs=[]),
    lambda row,p,r,f,b: r["logs"].append(deepcopy(r["logs"][0])),
    lambda row,p,r,f,b: "0x89",
])
def test_wrong_chain_wallet_contract_finality_or_duplicate_never_claims_credit(change):
    with pytest.raises((ValueError,TypeError)):
        inspect(changes=change)


def test_xrp_requires_confirmed_usdc_not_an_unsupported_usdt():
    with pytest.raises(ValueError):
        inspect("XRP","USDT")


def test_ethereum_receipt_canonical_stablecoin_transfer_is_only_valid_proof():
    row=original("SOL")
    p=protocol(row)
    r=transaction(row)
    r["logs"].append({
        "address":STABLES["USDT"],
        "topics":[dest._TRANSFER_TOPIC,topic(SENDER),topic(WALLET)],
        "data":"0x"+format(3000000,"064x"),
    })
    proof=dest.inspect_original_eth_stable_destination(
        row,p,chain_id="0x1",receipt=r,
        finalized={"number":"0x64"},
        included_block={"hash":"0x"+BLOCK,"number":"0x64"},
    )
    assert proof["destination_received_atomic"]=="1234567"


def test_original_destination_journal_schema_is_proof_bound():
    ddl="\n".join(SCHEMA)
    for part in (
        "mininex_live_native_order_intents",
        "destination_proof_sha256",
        "destination_received_atomic",
        "destination_finalized_at",
        "status != 'DESTINATION_FINALIZED'",
    ):
        assert part in ddl


class Cursor:
    def __init__(self,record):
        self.rows=iter([record,{"status":"DESTINATION_FINALIZED"}])
        self.queries=[]
    def execute(self,sql,args):
        self.queries.append((sql,args))
    def fetchone(self):return next(self.rows)


def test_durable_destination_finalization_changes_one_exact_original_only():
    row=original("XRP")
    proof=inspect("XRP")
    stored={
        "status":"SOURCE_FINALIZED","side":"SELL","stable":row["stable"],
        "source_tx_hash":row["source_tx_hash"],
        "payload_sha256":row["payload_sha256"],
        "source_proof_sha256":row["source_proof_sha256"],
        "destination_wallet":row["destination_wallet"],
        "destination_tx_hash":None,"destination_proof_sha256":None,
    }
    cur=Cursor(stored)
    assert dest._persist_native_destination_locked(
        cur,original=row,verified=proof)=="DESTINATION_FINALIZED"
    assert len(cur.queries)==2
    sql,args=cur.queries[-1]
    assert "status='DESTINATION_FINALIZED'" in sql
    assert "status='SOURCE_FINALIZED'" in sql
    assert "destination_proof_sha256 IS NULL" in sql
    assert (proof["destination_tx_hash"],proof["destination_proof_sha256"],
            proof["destination_received_atomic"])==args[:3]
    assert row["privy_did"]==args[3]
    assert row["source_proof_sha256"]==args[-1]


@pytest.mark.parametrize("alter",[
    lambda row:row.update(status="DESTINATION_FINALIZED"),
    lambda row:row.update(source_proof_sha256="f"*64),
    lambda row:row.update(destination_wallet="0x"+"b"*40),
    lambda row:row.update(destination_tx_hash=TX),
    lambda row:row.update(destination_proof_sha256="d"*64),
])
def test_durable_destination_never_overwrites_changed_or_completed_order(alter):
    row=original()
    proof=inspect()
    stored={
        "status":"SOURCE_FINALIZED","side":"SELL","stable":row["stable"],
        "source_tx_hash":row["source_tx_hash"],
        "payload_sha256":row["payload_sha256"],
        "source_proof_sha256":row["source_proof_sha256"],
        "destination_wallet":row["destination_wallet"],
        "destination_tx_hash":None,"destination_proof_sha256":None,
    }
    alter(stored)
    cur=Cursor(stored)
    with pytest.raises(HTTPException):
        dest._persist_native_destination_locked(cur,original=row,verified=proof)
    assert len(cur.queries)==1


@pytest.mark.parametrize("field,wrong",[
    ("ethereum_finalized_token_credit",False),
    ("original_source_proof_sha256","f"*64),
    ("source_tx_hash","f"*64),
    ("destination_proof_sha256",None),
    ("destination_received_atomic","0"),
    ("fee_assessed",True),
    ("trading_profit_verified",True),
    ("new_transactions_sent",1),
    ("retry_permitted",True),
])
def test_durable_destination_never_trusts_forged_or_unverified_results(field,wrong):
    row=original()
    proof={**inspect(),field:wrong}
    cur=Cursor({})
    with pytest.raises((ValueError,HTTPException)):
        dest._persist_native_destination_locked(cur,original=row,verified=proof)
    assert cur.queries==[]


def test_private_reader_binds_owner_before_thornode_or_rpc(monkeypatch):
    row=original()
    trace=[]
    class CursorReader:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def execute(self,sql,params):trace.append(("db",params))
        def fetchone(self):return row
    class DB:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kwargs):return CursorReader()
    monkeypatch.setattr(dest,"_db_url",lambda:"postgres://mock")
    monkeypatch.setattr(dest,"_ensure_schema",lambda _:None)
    monkeypatch.setattr(dest.psycopg,"connect",lambda *a,**kw:DB())
    async def fake_privy(did):return {"id":did,"linked_accounts":[]}
    monkeypatch.setattr(dest,"fetch_privy_user",fake_privy)
    monkeypatch.setattr(dest,"verified_addresses",lambda *a,**kw:{"ETH":"0x"+"b"*40})
    async def must_not_run(*args,**kwargs):
        pytest.fail("Wrong Privy destination must not contact THORChain")
    monkeypatch.setattr(dest,"get_private_original_protocol_status",must_not_run)
    with pytest.raises(HTTPException) as error:
        asyncio.run(dest.get_private_native_ethereum_destination(
            DID,asset="BTC",intent_key=row["intent_key"]))
    assert error.value.status_code==409
    assert trace==[("db",(DID,"BTC",row["intent_key"]))]


def test_only_authenticated_get_can_inspect_original_native_destination():
    from backend.app import app
    matches=[r for r in app.routes if
        r.path=="/api/live/native/settlement/{asset}/{intent_key}"]
    assert len(matches)==1
    assert matches[0].methods=={"GET"}
    source=inspect_source(matches[0].endpoint)
    assert "verify_privy_bearer" in source
    assert "get_private_native_ethereum_destination" in source
    assert "cache-control" in source.lower() or "Cache-Control" in source


def inspect_source(func):
    return inspect.getsource(func)
