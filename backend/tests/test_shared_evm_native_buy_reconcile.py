"""Native EVM BUY settlement is original-hash, owner and chain bound."""
import asyncio
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException
from backend import shared_evm_native_buy_reconcile as rec
from backend.shared_evm_swap_receipts import _pair

DID="did:privy:owner"
KEY="sig_abcdef01234567890123456789"
SWAP="0x"+"ab"*32
NATIVE="0x"+"cd"*32
WALLET="0x"+"ef"*20
ASSETS={"BNB":"USDT","POL":"USDC","AVAX":"USDC"}


class Cursor:
    def __init__(self,rows):
        self.rows=iter(rows)
        self.sql=[]
    def execute(self,q,args):
        self.sql.append((q,args))
    def fetchone(self):
        return next(self.rows)


def proof(asset,success=True):
    pair=_pair(asset,ASSETS[asset],"BUY")
    return dict(mode="SHARED_FINALIZED_NATIVE_EVM_CONVERSION",
                asset=asset,chain_id=pair["chain_id"],network=pair["network"],
                wallet_address=WALLET,tx_hash=NATIVE,operation="WITHDRAW",
                transaction_finalized=True,
                confirmed_native_conversion=success,
                amount_native="1.5" if success else None,
                state="FINALIZED_NATIVE_CONVERSION" if success
                      else "FINALIZED_REVERTED")


def pending_row(asset):
    return dict(privy_did=DID,asset=asset,intent_key=KEY,operation="WITHDRAW",
                chain_id=_pair(asset,ASSETS[asset],"BUY")["chain_id"],
                wallet_address=WALLET,amount_wei=Decimal(1500000000000000000),
                status="SUBMITTED",tx_hash=NATIVE)


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
@pytest.mark.parametrize("success", [True,False])
def test_actual_native_receipt_is_bound_to_original_saved_send(asset,success):
    cur=Cursor([pending_row(asset),
                {"status":"FINALIZED_SUCCESS" if success
                 else "FINALIZED_REVERTED"}])
    state=rec._finalize_native_step_locked(
        cur,did=DID,asset=asset,key=KEY,proof=proof(asset,success))
    assert state==("FINALIZED_SUCCESS" if success else "FINALIZED_REVERTED")
    assert len(cur.sql)==2
    assert "FOR UPDATE" in cur.sql[0][0]
    assert "AND status='SUBMITTED'" in cur.sql[1][0]


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
@pytest.mark.parametrize("tamper", [
    lambda p:p.update(wallet_address="0x"+"aa"*20),
    lambda p:p.update(tx_hash=SWAP),
    lambda p:p.update(amount_native="1.6"),
    lambda p:p.update(chain_id=1),
    lambda p:p.update(operation="DEPOSIT"),
    lambda p:p.update(transaction_finalized=False),
])
def test_wrong_wallet_chain_amount_or_unconfirmed_native_never_finalizes(asset,tamper):
    row=pending_row(asset)
    cur=Cursor([row])
    p=proof(asset)
    tamper(p)
    with pytest.raises((ValueError,HTTPException)):
        rec._finalize_native_step_locked(
            cur,did=DID,asset=asset,key=KEY,proof=p)
    assert not any("UPDATE mininex_live_evm_native_auto_steps" in q
                   for q,args in cur.sql)


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
def test_native_completion_never_retries_on_nonfinal_receipt(monkeypatch,asset):
    calls=[]
    async def check(did,tx_hash,**kw):
        calls.append(("receipt",tx_hash,kw["asset"]))
        return {"state":"INCLUDED_SUCCESS","transaction_finalized":False}
    monkeypatch.setattr(rec,"check_user_native_evm_conversion",check)
    async def run():
        return await rec.reconcile_original_native_step(pending_row(asset))
    assert asyncio.run(run())=="AWAITING_FINALITY"
    assert calls==[("receipt",NATIVE,asset)]


def test_passive_queries_scan_exact_original_hashes_and_not_ambiguous_sends():
    import inspect
    source=inspect.getsource(rec.pending_native_steps)
    assert "status='SUBMITTED' AND tx_hash IS NOT NULL" in source
    assert "'RECONCILIATION_REQUIRED'" not in source
    source=inspect.getsource(rec.original_native_buys_without_step)
    assert "n.intent_key IS NULL" in source
    assert "b.status='FINALIZED_SUCCESS' AND b.side='BUY'" in source
    assert "s.status='ACTIVE'" in source
    assert "r.state='RECONCILIATION_REQUIRED'" in source
    assert "LEFT JOIN mininex_live_evm_native_auto_steps" in source


def test_coordinator_first_finalizes_then_settles_then_sends(monkeypatch):
    order=[]
    async def pending(*args):
        order.append("pending")
        return []
    async def settled(*args):
        order.append("completed")
        return []
    async def without(*args):
        order.append("new")
        return []
    monkeypatch.setattr(rec,"pending_native_steps",lambda limit:order.append("pending") or [])
    monkeypatch.setattr(rec,"completed_native_buys_needing_cycle",
                        lambda limit:order.append("completed") or [])
    monkeypatch.setattr(rec,"original_native_buys_without_step",
                        lambda limit:order.append("new") or [])
    async def run():
        async with httpx.AsyncClient() as client:
            return await rec.reconcile_native_buy_cycle_once(client=client)
    result=asyncio.run(run())
    assert result["new_native_sends"]==0
    assert order==["pending","completed","new"]
