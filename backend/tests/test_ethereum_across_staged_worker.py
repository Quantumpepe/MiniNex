"""Worker lifecycle tests: no real signer, network, wallet or transaction."""
import asyncio
from datetime import datetime,timezone
import pytest
from backend import ethereum_across_staged_worker as m

DID="did:privy:original"
NOW=datetime(2026,11,25,tzinfo=timezone.utc)
@pytest.mark.parametrize("asset",["BNB","POL","ARB"])
def test_exit_cancels_only_unspent_buy_and_finishes_spent_order(monkeypatch,asset):
    scope={"settlement_policy":"ETHEREUM_ACROSS_STAGED_V1"}
    row={"asset":asset,"stable":"USDC","budget_revision":2,"started_by_owner_at":NOW,"session_status":"EXIT_PENDING"}
    order={**row,"order_id":"original","side":"BUY","stage":1,"scope_json":scope}
    seen=[]
    monkeypatch.setattr(m,"read_open_order",lambda *a:order)
    monkeypatch.setattr(m,"read_pending_step",lambda *a:None)
    monkeypatch.setattr(m,"cancel_unspent_buy_for_exit",lambda *a:seen.append("cancel") or True)
    async def audit(*a):seen.append("audit");return {"scope":scope}
    async def prepare(*a):seen.append("prepare");return {}
    async def submit(*a):seen.append("submit");return {"state":"SUBMITTED"}
    monkeypatch.setattr(m,"_audited",audit)
    monkeypatch.setattr(m,"_prepare",prepare)
    monkeypatch.setattr(m,"_submit",submit)
    def run():return asyncio.run(m.advance_across_order(DID,row=row,client=None))
    assert run()["state"]=="CANCELLED"
    assert seen==["cancel"]
    seen.clear();order["stage"]=2
    assert run()["state"]=="SUBMITTED"
    assert seen==["audit","prepare","submit"]
    seen.clear()
    row["session_status"]="PAUSED"
    assert run()["reason"]=="ORIGINAL_SESSION_PAUSED"
    assert seen==[]
    row["session_status"]="EXIT_PENDING"
    monkeypatch.setattr(m,"read_pending_step",lambda *a:{"state":"SOURCE_FINALIZED"})
    assert run()["reason"]=="ORIGINAL_STAGED_STEP_PENDING"
    assert seen==[]

def test_bridge_worker_uses_chain_discovery_then_independent_proof(monkeypatch):
    seen=[]
    source="0x"+"1"*64;dest="0x"+"2"*64
    step={"kind":"BRIDGE","prepared":{"terms":{}},"tx_hash":source,"scan_block":100}
    async def reconcile(client,**kw):
        seen.append(("reconcile",kw.get("destination_hash")))
        return {"state":"BRIDGE_CREDIT_VERIFIED"} if kw.get("destination_hash") else {
            "state":"DESTINATION_PENDING","deposit_id":"7"}
    async def scan(client,**kw):
        seen.append(("scan",kw["deposit_id"],kw["start_block"]))
        return {"destination_hash":dest,"next_block":100}
    def save(step,proof,cursor=None):
        seen.append(("save",proof["state"],cursor));return True
    monkeypatch.setattr(m,"reconcile_staged_across_bridge",reconcile)
    monkeypatch.setattr(m,"find_staged_across_destination",scan)
    monkeypatch.setattr(m,"record_step_proof",save)
    assert asyncio.run(m.reconcile_one_across_step(None,step))
    assert seen==[("reconcile",None),("save","DESTINATION_PENDING",None),
                  ("scan","7",100),("reconcile",dest),("save","BRIDGE_CREDIT_VERIFIED",100)]
