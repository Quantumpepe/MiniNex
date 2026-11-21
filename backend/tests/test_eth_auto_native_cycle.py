"""Actual WETH9 native-cycle state transitions do not replay wallet sends."""
import asyncio
import pytest
from fastapi import HTTPException
from backend import eth_auto_native_cycle as n

DID="did:privy:live-user"
SOURCE="sig_abcdef0123456789012345"
HASH="0x"+"a"*64

def test_native_dedup_key_per_owner_and_operation():
    a=n.native_intent_key(DID,SOURCE,"WITHDRAW")
    assert a.startswith("native_")
    assert len(a)==63
    assert a==n.native_intent_key(DID,SOURCE,"WITHDRAW")
    assert a!=n.native_intent_key(DID,SOURCE,"DEPOSIT")

@pytest.mark.parametrize("amount,expected",[
 ("0.1",10**17),("0.000000000000000001",1),("1",10**18),
])
def test_exact_native_wei(amount,expected):
    assert n._atomic_wei(amount)==expected

@pytest.mark.parametrize("amount",["0","-1","NaN","0.0000000000000000001"])
def test_invalid_native_quantity(amount):
    with pytest.raises(ValueError):n._atomic_wei(amount)

def test_finalize_buy_weth_to_real_eth(monkeypatch):
    sent=[]
    async def receipt(did,hash,*,operation):
        assert operation=="WITHDRAW"
        return {"state":"FINALIZED_WETH9_CONVERSION",
                "transaction_finalized":True}
    def validated(**kwargs):
        return {"source_amount":"0.2","tx_hash":HASH}
    monkeypatch.setattr(n,"check_user_weth9_receipt",receipt)
    monkeypatch.setattr(n,"validated_finalized_leg",validated)
    monkeypatch.setattr(n,"save_verified_leg",
                        lambda did,leg:sent.append("record"))
    monkeypatch.setattr(n,"_finalize_native_record",
                        lambda *args:sent.append("confirmed"))
    r=asyncio.run(n.finalize_native_step({
        "privy_did":DID,"source_key":SOURCE,
        "operation":"WITHDRAW","stable":"USDC",
        "amount_wei":2*10**17,"tx_hash":HASH}))
    assert r=="CONFIRMED"
    assert sent==["record","confirmed"]

def test_included_native_is_not_posted_as_finished(monkeypatch):
    async def receipt(*args,**kwargs):
        return {"state":"INCLUDED_SUCCESS","transaction_finalized":False}
    monkeypatch.setattr(n,"check_user_weth9_receipt",receipt)
    monkeypatch.setattr(n,"save_verified_leg",
                        lambda *args:pytest.fail("not finalized"))
    r=asyncio.run(n.finalize_native_step({
        "privy_did":DID,"source_key":SOURCE,
        "operation":"WITHDRAW","stable":"USDT",
        "amount_wei":10**17,"tx_hash":HASH}))
    assert r=="PENDING"

def test_reverted_native_does_not_credit_eth(monkeypatch):
    async def receipt(*args,**kwargs):
        return {"state":"FINALIZED_REVERTED","transaction_finalized":True}
    monkeypatch.setattr(n,"check_user_weth9_receipt",receipt)
    monkeypatch.setattr(n,"save_verified_leg",
                        lambda *args:pytest.fail("no fill after revert"))
    finals=[]
    monkeypatch.setattr(n,"_finalize_native_record",
                        lambda *args:finals.append(args[-1]))
    r=asyncio.run(n.finalize_native_step({
        "privy_did":DID,"source_key":SOURCE,
        "operation":"DEPOSIT","stable":"USDC",
        "amount_wei":10**17,"tx_hash":HASH}))
    assert r=="REVERTED" and finals==["REVERTED"]

def test_live_buy_native_conversion_invokes_real_privy_sender_once(monkeypatch):
    calls=[]
    monkeypatch.setattr(n,"_native_claim_locked",lambda *args,**kwargs:True)
    # Native source validation and persist are covered by the worker integration.
    assert n.native_intent_key(DID,SOURCE,"WITHDRAW")!=(
           n.native_intent_key(DID,SOURCE,"DEPOSIT"))


@pytest.mark.parametrize("pending,expected",[
    (0,None),(1,409),(3,409),(-1,503),(None,503),
])
def test_eth_native_send_respects_shared_link_uni_wallet_nonce(pending,expected):
    class Cursor:
        def __init__(self):self.queries=[]
        def execute(self,query,params):self.queries.append((query,params))
        def fetchone(self):
            return {"pending":pending}
    cur=Cursor()
    if expected is None:
        n._assert_no_pending_ethereum_token_send(cur,DID)
    else:
        with pytest.raises(HTTPException) as error:
            n._assert_no_pending_ethereum_token_send(cur,DID)
        assert error.value.status_code==expected
    assert len(cur.queries)==1
    sql,args=cur.queries[0]
    assert "mininex_live_evm_auto_broadcasts" in sql
    assert "chain_id=1" in sql
    assert "RECONCILIATION_REQUIRED" in sql
    assert args==(DID,DID,DID,DID)
    assert "mininex_live_native_order_intents" in sql
    assert "mininex_live_evm_router_approval_sends" in sql
    assert "mininex_evm_auto_fee_sends" in sql


def test_eth_native_claim_checks_shared_nonce_inside_original_transaction():
    import inspect
    source=inspect.getsource(n._native_claim_locked)
    assert "_assert_no_pending_ethereum_token_send(cur,did)" in source
    assert source.index("_assert_no_pending_ethereum_token_send(cur,did)")<source.index(
        "INSERT INTO mininex_live_eth_auto_native_steps")


def test_native_eth_receipt_and_buy_completion_scans_rotate_all_owners(
    monkeypatch,
):
    submitted=[{"source_key":f"native_{i}"} for i in range(5)]
    ready=[{"source_key":f"buy_{i}"} for i in range(5)]
    queries=[]
    class DB:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kwargs):return self
        def execute(self,sql,params):
            self.rows=ready if "JOIN mininex_live_eth_verified_legs" in sql else submitted
            self.limit,self.offset=params
            queries.append((sql,params))
        def fetchall(self):return self.rows[self.offset:self.offset+self.limit]
    monkeypatch.setattr(n,"_db_url",lambda:"postgresql://test")
    monkeypatch.setattr(n,"_ensure_schema",lambda _:None)
    monkeypatch.setattr(n.psycopg,"connect",lambda *args,**kwargs:DB())
    monkeypatch.setattr(n,"_NATIVE_RECEIPT_SCAN_OFFSET",0)
    monkeypatch.setattr(n,"_NATIVE_BUY_SCAN_OFFSET",0)
    receipts=[n.submitted_native_steps(limit=2) for _ in range(4)]
    completions=[n.confirmed_buy_native_conversions(limit=2) for _ in range(4)]
    assert receipts==[submitted[:2],submitted[2:4],submitted[4:],submitted[:2]]
    assert completions==[ready[:2],ready[2:4],ready[4:],ready[:2]]
    assert [params for _,params in queries]==(
        [(2,0),(2,2),(2,4),(2,5),(2,0)]*2)
    assert all("LIMIT %s OFFSET %s" in sql for sql,_ in queries)
