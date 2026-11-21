"""Owner-initiated ETH AUTO scheduling with full native stop and grant."""
import pytest
from fastapi import HTTPException
from backend import eth_auto_live_sessions as mod

DID="did:privy:liveuser"
GRANT={"user_privy_grant_verified":True,"policy_verified":True,
       "wallet_owned_by_user":True,"policy_id":"policy123456",
       "verified_wallet_address":"0x"+"a"*40}
class Cursor:
    def __init__(self,rows):
        self.rows=iter(rows);self.sql=[]
    def execute(self,s,args):
        self.sql.append(s)
    def fetchone(self):
        return next(self.rows)

def rows():
    return [
     {"trading_paused":True,"revision":3,"planned_capital_usd":200,
      "max_order_usd":100,"max_daily_turnover_usd":250},
     {"status":"GRANT_VERIFIED","policy_id":"policy123456",
      "wallet_address":"0x"+"a"*40,"limits_revision":3,
      "remote_policy_sha256":"c"*64,"candidate_sha256":"c"*64,
      "expires":10**11},
     {"status":"SIGNED_INTENT","revoked_at":None,"limits_revision":3},
    ]

def test_owner_granted_auto_session_can_start(monkeypatch):
    monkeypatch.setattr(mod.secrets,"token_hex",lambda n:"a"*32)
    cur=Cursor(rows())
    status=mod._start_locked(cur,DID,"USDC",GRANT)
    assert status["status"]=="ACTIVE"
    assert any("trading_paused=FALSE" in sql for sql in cur.sql)
    assert any("INSERT INTO mininex_live_eth_auto_sessions" in sql
               for sql in cur.sql)

@pytest.mark.parametrize("index,change",[
    (0,{"planned_capital_usd":0}),
    (1,{"status":"REMOTE_VERIFIED_UNATTACHED"}),
    (1,{"policy_id":"other"}),
    (2,{"status":"REVOKED"}),
])
def test_no_start_without_user_approval(index,change):
    data=rows();data[index].update(change);cur=Cursor(data)
    with pytest.raises(HTTPException):
        mod._start_locked(cur,DID,"USDT",GRANT)
    assert not any("trading_paused=FALSE" in sql for sql in cur.sql)

def test_owner_may_resume_while_old_source_waits_for_finality(monkeypatch):
    # Start/Resume doesn't resend the old source; the original order
    # remains reserved and the nonce/receipt worker owns its recovery.
    monkeypatch.setattr(mod.secrets,"token_hex",lambda n:"a"*32)
    cur=Cursor(rows())
    activated=mod._start_locked(cur,DID,"USDC",GRANT)
    assert activated["status"]=="ACTIVE"
    assert all("SELECT COUNT(*) AS pending" not in q for q in cur.sql)
    assert any("INSERT INTO mininex_live_eth_auto_sessions" in q
               for q in cur.sql)


def test_no_start_without_remote_wallet_grant():
    cur=Cursor(rows())
    with pytest.raises(HTTPException):
        mod._start_locked(cur,DID,"USDC",{
            **GRANT,"user_privy_grant_verified":False})
    assert cur.sql==[]

def test_pause_locks_out_future_trades():
    cur=Cursor([{"trading_paused":False}])
    paused=mod._pause_locked(cur,DID)
    assert paused["future_auto_orders_blocked"] is True
    assert any("trading_paused=TRUE" in sql for sql in cur.sql)

def test_eth_worker_respects_owner_session_without_extra_operator_flags(monkeypatch):
    monkeypatch.delenv("DATABASE_URL",raising=False)
    assert mod._worker_approved() is False
    monkeypatch.setenv("DATABASE_URL","postgresql://test.invalid/mininex")
    monkeypatch.delenv("MININEX_ETH_AUTO_WORKER_ENABLED",raising=False)
    monkeypatch.delenv("MININEX_LIVE_TRADING_ENABLED",raising=False)
    assert mod._worker_approved() is True
    monkeypatch.setenv("MININEX_ETH_AUTO_WORKER_ENABLED","disabled")
    monkeypatch.setenv("MININEX_LIVE_TRADING_ENABLED","false")
    assert mod._worker_approved() is True

def test_real_finalized_swap_unblocks_next_eth_auto_order(monkeypatch):
    import asyncio
    entry={"privy_did":DID,"intent_key":"sig_abcdef0123456789",
           "tx_hash":"0x"+"a"*64,"side":"BUY","stable":"USDC"}
    actions=[]
    async def evidence(*args,**kwargs):
        return {"state":"FINALIZED_ERC20_SWAP_LEG",
                "transaction_finalized":True}
    def validated(**kwargs):
        actions.append("validate")
        return {"tx_hash":entry["tx_hash"]}
    def persist(did,leg):actions.append("persist")
    def finalize(did,key,hash,state):
        actions.append(state)
        assert state=="CONFIRMED"
        return True
    monkeypatch.setattr(mod,"check_user_router02_receipt",evidence)
    monkeypatch.setattr(mod,"validated_finalized_leg",validated)
    monkeypatch.setattr(mod,"save_verified_leg",persist)
    monkeypatch.setattr(mod,"_finalize_order",finalize)
    assert asyncio.run(mod.reconcile_one_live_eth_order(entry))=="CONFIRMED"
    assert actions==["validate","persist","CONFIRMED"]

def test_pending_chain_transaction_never_creates_fill(monkeypatch):
    import asyncio
    entry={"privy_did":DID,"intent_key":"sig_abcdef0123456789",
           "tx_hash":"0x"+"a"*64,"side":"BUY","stable":"USDC"}
    async def evidence(*args,**kwargs):
        return {"state":"INCLUDED_SUCCESS","transaction_finalized":False}
    monkeypatch.setattr(mod,"check_user_router02_receipt",evidence)
    monkeypatch.setattr(mod,"save_verified_leg",
                        lambda *args:pytest.fail("premature fill"))
    assert asyncio.run(mod.reconcile_one_live_eth_order(entry))=="PENDING"

def test_reverted_chain_order_releases_pending_status_without_fake_trade(monkeypatch):
    import asyncio
    entry={"privy_did":DID,"intent_key":"sig_abcdef0123456789",
           "tx_hash":"0x"+"a"*64,"side":"SELL","stable":"USDT"}
    async def evidence(*args,**kwargs):
        return {"state":"FINALIZED_REVERTED","transaction_finalized":True}
    monkeypatch.setattr(mod,"check_user_router02_receipt",evidence)
    monkeypatch.setattr(mod,"save_verified_leg",
                        lambda *args:pytest.fail("revert cannot create fill"))
    states=[]
    monkeypatch.setattr(mod,"_finalize_order",
                        lambda did,key,hash,state:states.append(state))
    assert asyncio.run(mod.reconcile_one_live_eth_order(entry))=="REVERTED"
    assert states==["REVERTED"]


@pytest.mark.parametrize("quorum", [
    {},
    {"remote_quorum_public_key_verified": False},
    {"remote_quorum_get_succeeded": True},
])
def test_auto_start_does_not_unpause_without_current_privy_quorum(monkeypatch, quorum):
    import asyncio
    monkeypatch.setattr(mod, "_worker_approved", lambda: True)
    async def lookup():
        return quorum
    monkeypatch.setattr(mod, "check_privy_backend_quorum", lookup)
    async def grant(_did):
        pytest.fail("User grant must not be evaluated with unverified signer")
    monkeypatch.setattr(mod, "get_eth_user_grant_review", grant)
    monkeypatch.setattr(mod, "_db_url",
                        lambda: pytest.fail("No DB mutation without remote quorum"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(mod.start_eth_auto(DID, "USDC"))
    assert exc.value.status_code == 503


def test_auto_start_remote_verified_signer_reaches_owner_grant(monkeypatch):
    import asyncio
    monkeypatch.setattr(mod, "_worker_approved", lambda: True)
    calls = []
    async def lookup():
        calls.append("quorum")
        return {"remote_quorum_public_key_verified": True}
    async def grant(_did):
        calls.append("grant")
        raise HTTPException(409, "No user wallet signer grant")
    monkeypatch.setattr(mod, "check_privy_backend_quorum", lookup)
    monkeypatch.setattr(mod, "get_eth_user_grant_review", grant)
    monkeypatch.setattr(mod, "_db_url",
                        lambda: pytest.fail("No DB mutation without user grant"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(mod.start_eth_auto(DID, "USDC"))
    assert exc.value.status_code == 409
    assert calls == ["quorum", "grant"]


def test_eth_work_pages_rotate_instead_of_stranding_active_users_or_receipts(
    monkeypatch,
):
    sessions=[{"privy_did":f"did:privy:eth-{i}"} for i in range(5)]
    orders=[{"intent_key":f"order_{i}"} for i in range(5)]
    queries=[]
    class DB:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kwargs):return self
        def execute(self,sql,params):
            self.page=orders if "FROM mininex_live_order_intents" in sql else sessions
            self.limit,self.offset=params
            queries.append((sql,params))
        def fetchall(self):return self.page[self.offset:self.offset+self.limit]
    monkeypatch.setattr(mod,"_worker_approved",lambda:True)
    monkeypatch.setattr(mod,"_db_url",lambda:"postgresql://test")
    monkeypatch.setattr(mod,"_ensure_schema",lambda url:None)
    monkeypatch.setattr(mod.psycopg,"connect",lambda *a,**kw:DB())
    monkeypatch.setattr(mod,"_ETH_SESSION_PAGE_OFFSET",0)
    monkeypatch.setattr(mod,"_ETH_ORDER_PAGE_OFFSET",0)
    pages=[mod.active_sessions(limit=2) for _ in range(4)]
    assert pages==[sessions[:2],sessions[2:4],sessions[4:],sessions[:2]]
    checked=[mod.submitted_live_orders(limit=2) for _ in range(4)]
    assert checked==[orders[:2],orders[2:4],orders[4:],orders[:2]]
    assert [p for sql,p in queries]==(
        [(2,0),(2,2),(2,4),(2,5),(2,0)]*2)
    assert all("LIMIT %s OFFSET %s" in sql for sql,p in queries)


def test_eth_auto_independent_wallets_do_not_block_one_another(
    monkeypatch,
):
    import asyncio
    import httpx
    monkeypatch.setattr(mod,"_worker_approved",lambda:True)
    owner_a={"privy_did":"did:privy:owner-a","run_id":"run_a123456",
             "stable":"USDC"}
    repeat_owner_a={**owner_a,"run_id":"run_a654321"}
    owner_b={"privy_did":"did:privy:owner-b","run_id":"run_b123456",
             "stable":"USDT"}
    b_started=asyncio.Event()
    started=[]
    async def execute(did,*,market,run_id,stable,client):
        started.append(run_id)
        if run_id=="run_a123456":
            await asyncio.wait_for(b_started.wait(),timeout=3)
        if run_id=="run_b123456":
            b_started.set()
        return {"state":"WAIT"}
    monkeypatch.setattr(mod,"execute_live_eth_strategy_tick",execute)
    async def run():
        async with httpx.AsyncClient() as client:
            return await mod.run_eth_owner_session_batch(
                [owner_a,repeat_owner_a,owner_b],
                market=object(),client=client)
    result=asyncio.run(run())
    assert result=={
        "sessions_seen":3,"attempted":3,
        "failed":0,"wallet_groups":2}
    assert started==["run_a123456","run_b123456","run_a654321"]


def test_eth_pending_live_receipts_of_different_wallets_do_not_block(
    monkeypatch,
):
    import asyncio
    monkeypatch.setattr(mod,"_worker_approved",lambda:True)
    rows=[{"privy_did":"did:privy:one","intent_key":"sig_aaaaaaaa"},
          {"privy_did":"did:privy:one","intent_key":"sig_bbbbbbbb"},
          {"privy_did":"did:privy:two","intent_key":"sig_cccccccc"}]
    monkeypatch.setattr(mod,"submitted_live_orders",lambda:rows)
    newer=asyncio.Event()
    seen=[]
    async def verify(row):
        seen.append(row["intent_key"])
        if row["intent_key"]=="sig_aaaaaaaa":
            await asyncio.wait_for(newer.wait(),timeout=3)
        if row["intent_key"]=="sig_cccccccc":
            newer.set()
        return "CONFIRMED"
    monkeypatch.setattr(mod,"reconcile_one_live_eth_order",verify)
    assert asyncio.run(mod.reconcile_live_eth_orders())==3
    assert seen==["sig_aaaaaaaa","sig_cccccccc","sig_bbbbbbbb"]


def test_eth_native_receipts_do_not_wait_for_unrelated_owner(
    monkeypatch,
):
    import asyncio
    import httpx
    monkeypatch.setattr(mod,"_worker_approved",lambda:True)
    rows=[{"privy_did":"did:privy:one","source_key":"sig_aaa"},
          {"privy_did":"did:privy:two","source_key":"sig_bbb"}]
    monkeypatch.setattr(mod,"submitted_native_steps",lambda:rows)
    monkeypatch.setattr(mod,"confirmed_buy_native_conversions",lambda:[])
    other=asyncio.Event()
    seen=[]
    async def finalize(row):
        seen.append(row["source_key"])
        if row["source_key"]=="sig_aaa":
            await asyncio.wait_for(other.wait(),timeout=3)
        else:
            other.set()
        return "CONFIRMED"
    monkeypatch.setattr(mod,"finalize_native_step",finalize)
    async def run():
        async with httpx.AsyncClient() as client:
            return await mod.reconcile_and_resume_native_eth(client)
    assert asyncio.run(run())==2
    assert seen==["sig_aaa","sig_bbb"]
