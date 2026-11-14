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
    (0,{"max_order_usd":0}),
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
