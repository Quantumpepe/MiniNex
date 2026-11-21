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
     {"pending":0},
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
    (3,{"pending":1}),
])
def test_no_start_without_user_approval(index,change):
    data=rows();data[index].update(change);cur=Cursor(data)
    with pytest.raises(HTTPException):
        mod._start_locked(cur,DID,"USDT",GRANT)
    assert not any("trading_paused=FALSE" in sql for sql in cur.sql)

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

def test_worker_disabled_without_explicit_deployment(monkeypatch):
    monkeypatch.delenv("MININEX_ETH_AUTO_WORKER_ENABLED",raising=False)
    monkeypatch.setenv("MININEX_LIVE_TRADING_ENABLED","true")
    assert not mod._worker_approved()

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
