"""Native BUY conversion is bound to one real finalized owner swap, no retries."""
import asyncio
from datetime import datetime,timezone,timedelta
from decimal import Decimal
import time

import httpx
import pytest
from fastapi import HTTPException

from backend import shared_evm_native_buy_completion as native
from backend.shared_evm_swap_receipts import _pair
from backend.storage import SCHEMA

DID="did:privy:owner"
KEY="sig_abcdef01234567890123456789"
WALLET="0x"+"ab"*20
WALLET_ID="wallet_12345678"
TX="0x"+"cd"*32
ASSETS={"BNB":"USDT","POL":"USDC","AVAX":"USDC"}
DIGEST="f"*64


def record(asset):
    stable=ASSETS[asset]
    return {"asset":asset,"stable":stable,"chain_id":_pair(asset,stable,"BUY")["chain_id"],
            "wallet_address":WALLET,"wallet_id":WALLET_ID,
            "policy_sha256":"a"*64,"budget_revision":5,
            "status":"GRANT_VERIFIED"}


def fill(asset,amount="1.5"):
    return {"side":"BUY","stable":ASSETS[asset],
            "chain_id":_pair(asset,ASSETS[asset],"BUY")["chain_id"],
            "tx_hash":TX,"destination_amount":Decimal(amount)}


class Cursor:
    def __init__(self,rows):
        self.rows=iter(rows)
        self.sql=[]
    def execute(self,q,args):
        self.sql.append((q,args))
    def fetchone(self):
        return next(self.rows)


def rows(asset):
    p=record(asset)
    return [
        {"status":"ACTIVE","chain_id":p["chain_id"],
         "stable":p["stable"],"wallet_address":WALLET,
         "budget_revision":5},
        {"privy_did":DID},
        {"status":"FINALIZED_SUCCESS","side":"BUY","stable":p["stable"],
         "chain_id":p["chain_id"],"wallet_address":WALLET,
         "tx_hash":TX},
        fill(asset),
        {"pending":0},{"pending":0},
    ]


def policy(asset):
    return {"rules":[{}, {}, {}, {"conditions":[]} ]}


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
def test_real_buy_weth9_wei_quantity_is_exact(asset):
    assert native.actual_buy_fill_wei(fill(asset),asset=asset,
                                      stable=ASSETS[asset])==1500000000000000000


@pytest.mark.parametrize("bad",["0","-1","NaN","0.0000000000000000001"])
def test_native_amount_cannot_be_zero_negative_or_inexact(bad):
    with pytest.raises(ValueError):
        native.actual_buy_fill_wei(fill("BNB",bad),asset="BNB",stable="USDT")


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
def test_native_original_buy_claim_checks_real_swap_policy_budget_nonce(
    monkeypatch,asset
):
    p=record(asset)
    monkeypatch.setattr(native,"_read_grant_evidence_locked",
                        lambda cur,**kw:p)
    monkeypatch.setattr(native,"validate_pinned_evm_policy",
                        lambda *a,**kw:policy(asset))
    monkeypatch.setattr(native,"_lte",lambda rule,field:str(10**20))
    cur=Cursor(rows(asset))
    native._post_buy_claim_locked(
        cur,did=DID,asset=asset,key=KEY,record=p,
        amount_wei=1500000000000000000,digest=DIGEST)
    assert len(cur.sql)==7
    assert "FOR UPDATE" in cur.sql[0][0]
    assert "mininex_live_evm_verified_fills" in cur.sql[3][0]
    assert "mininex_live_evm_auto_broadcasts" in cur.sql[4][0]
    assert "mininex_ethereum_settlement_submissions" in cur.sql[4][0]
    assert "mininex_avax_staged_steps" in cur.sql[4][0]
    assert "mininex_live_evm_router_approval_sends" in cur.sql[4][0]
    assert "mininex_live_evm_native_sell_intents" in cur.sql[4][0]
    assert "mininex_live_evm_native_auto_steps" in cur.sql[5][0]
    assert "INSERT INTO mininex_live_evm_native_auto_steps" in cur.sql[6][0]
    assert "ON CONFLICT" not in cur.sql[6][0]


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
@pytest.mark.parametrize("mutation,query_count",[
    (lambda x:x[0].update(status="PAUSED"),1),
    (lambda x:x[2].update(status="SUBMITTED"),3),
    (lambda x:x[2].update(side="SELL"),3),
    (lambda x:x[3].update(destination_amount=Decimal("2")),4),
    (lambda x:x[4].update(pending=1),5),
    (lambda x:x[5].update(pending=1),6),
])
def test_no_native_send_for_wrong_owner_cycle_or_pending_nonce(
    monkeypatch,asset,mutation,query_count
):
    p=record(asset)
    monkeypatch.setattr(native,"_read_grant_evidence_locked",
                        lambda cur,**kw:p)
    monkeypatch.setattr(native,"validate_pinned_evm_policy",
                        lambda *a,**kw:policy(asset))
    monkeypatch.setattr(native,"_lte",lambda rule,field:str(10**20))
    r=rows(asset)
    mutation(r)
    cur=Cursor(r)
    with pytest.raises(HTTPException):
        native._post_buy_claim_locked(
            cur,did=DID,asset=asset,key=KEY,record=p,
            amount_wei=1500000000000000000,digest=DIGEST)
    assert len(cur.sql)==query_count
    assert all("INSERT INTO mininex_live_evm_native_auto_steps" not in q
               for q,args in cur.sql)


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
def test_native_convert_key_different_from_swap_and_chain(asset):
    key=native.original_native_step_key(did=DID,asset=asset,key=KEY)
    assert key.startswith("native_")
    assert key!=KEY
    assert native.original_native_step_key(did=DID,asset=asset,key=KEY)==key
    others=[a for a in ASSETS if a!=asset]
    assert all(native.original_native_step_key(
        did=DID,asset=a,key=KEY)!=key for a in others)


def test_sent_native_receipt_or_ambiguous_state_cannot_be_claimed_twice():
    cur=Cursor([{"status":"SUBMITTED"}])
    result=native._persist_native_result_locked(
        cur,did=DID,asset="BNB",key=KEY,digest=DIGEST,tx_hash=TX)
    assert result=="SUBMITTED"
    assert "AND status='SENDING'" in cur.sql[0][0]
    cur=Cursor([{"status":"RECONCILIATION_REQUIRED"}])
    assert native._persist_native_result_locked(
        cur,did=DID,asset="BNB",key=KEY,digest=DIGEST,
        tx_hash=None)=="RECONCILIATION_REQUIRED"


def test_missing_owner_remote_privy_grant_prevents_real_native_send(monkeypatch):
    async def audit(*args):
        return {"asset":"BNB","owner_wallet_current":False}
    async def reject(*args,**kwargs):
        pytest.fail("Rejected owner native send must not invoke onchain API")
    monkeypatch.setattr(native,"get_owner_evm_remote_grant_audit",audit)
    monkeypatch.setattr(native,"check_evm_source_allowance",reject)
    async def task():
        async with httpx.AsyncClient() as client:
            return await native.submit_internal_native_evm_buy_completion(
                did=DID,asset="BNB",intent_key=KEY,client=client)
    with pytest.raises(HTTPException):
        asyncio.run(task())


def test_native_ledger_foreign_keys_and_no_public_send_route():
    ddl="\n".join(SCHEMA)
    assert "CREATE TABLE IF NOT EXISTS mininex_live_evm_native_auto_steps" in ddl
    assert "REFERENCES mininex_live_evm_auto_broadcasts" in ddl
    assert "UNIQUE(chain_id,tx_hash)" in ddl
    from backend.app import app
    assert all("native-auto-send" not in route.path for route in app.routes)
