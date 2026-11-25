"""Durable live order status is owner-only, asset-scoped and never paper."""
from datetime import datetime,timezone
import inspect

import pytest
from fastapi import HTTPException

from backend import shared_evm_order_status as status

DID="did:privy:owner"
OTHER="did:privy:other"
TX="0x"+"ab"*32
WHEN=datetime(2026,11,17,12,0,tzinfo=timezone.utc)
CHAIN={"BNB":56,"POL":137,"AVAX":43114,"LINK":1,"UNI":1,"ARB":42161}


def row(asset,stage="SUBMITTED",stable=None,side="BUY",native=False,fee=False,paid=False):
    return {
        "asset":asset,"chain_id":CHAIN[asset],
        "intent_key":"sig_abcdef01234567890123456789",
        "side":side,"stable":stable or (
            "USDT" if asset=="BNB" else "USDC"),
        "status":stage,
        "tx_hash":TX if stage not in ("SENDING","RECONCILIATION_REQUIRED") else None,
        "created_at":WHEN,"updated_at":WHEN,
        "native_cycle_verified":native,"hwm_fee_recorded":fee,
        "fee_payment_verified":paid,
    }


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI","ARB"])
@pytest.mark.parametrize("stage",[
    "SENDING","SUBMITTED","RECONCILIATION_REQUIRED",
    "FINALIZED_SUCCESS","FINALIZED_REVERTED",
])
def test_five_coins_have_correct_isolated_pending_and_final_state(asset,stage):
    r=status._verified_order_row(row(asset,stage),asset)
    assert r["asset"]==asset
    assert r["chain_id"]==CHAIN[asset]
    assert r["pending_or_ambiguous"]==(
        stage in ("SENDING","SUBMITTED","RECONCILIATION_REQUIRED"))
    assert r["swap_erc20_leg_finalized"]==(stage=="FINALIZED_SUCCESS")
    assert r["automatic_retry_allowed"] is False
    assert r["realized_net_pnl_usd"] is None
    assert r["fee_transferred"] is False
    assert r["native_trade_cycle_settled"] is False
    assert r["five_percent_hwm_accounting_recorded"] is False


@pytest.mark.parametrize("mutate",[
    lambda r:r.update(asset="LINK"),
    lambda r:r.update(chain_id=56),
    lambda r:r.update(status="PAPER"),
    lambda r:r.update(status="FINALIZED_SUCCESS",tx_hash=None),
    lambda r:r.update(tx_hash="0xdead"),
    lambda r:r.update(created_at=datetime(2026,11,17)),
])
def test_invalid_or_cross_coin_orders_never_appear(mutate):
    r=row("UNI")
    mutate(r)
    with pytest.raises(ValueError):
        status._verified_order_row(r,"UNI")


class DB:
    def __init__(self,items):self.items=items;self.calls=[]
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def cursor(self,**kwargs):return self
    def execute(self,query,args):self.calls.append((query,args))
    def fetchall(self):
        query,(did,asset,limit)=self.calls[-1]
        family=("staged" if "_staged_orders o" in query else
                "cross" if "mininex_ethereum_settlement_submissions s" in query else None)
        key=(did,asset,family) if family else (did,asset)
        return self.items.get(key,[])[:limit]


def test_query_scopes_privy_owner_and_coin_and_never_shows_simulation(monkeypatch):
    fake=DB({
        (DID,"UNI"):[row("UNI") for _ in range(3)],
        (OTHER,"UNI"):[row("UNI")],
        (DID,"AVAX"):[row("AVAX")],
    })
    monkeypatch.setattr(status,"_db_url",lambda:"postgres://test")
    monkeypatch.setattr(status,"_ensure_schema",lambda url:None)
    monkeypatch.setattr(status.psycopg,"connect",lambda *a,**kw:fake)
    r=status.get_owner_live_evm_order_status(DID,asset="UNI",limit=2)
    assert len(r["orders"])==2
    assert r["history_truncated"] is True
    assert r["includes_simulation"] is False
    assert not status.get_owner_live_evm_order_status(OTHER,asset="AVAX")["orders"]
    assert all(
        "WHERE b.privy_did=%s AND b.asset=%s" in q
        or "WHERE s.privy_did=%s AND q.asset=%s" in q
        or ("WHERE o.privy_did=%s AND " in q and "=%s AND o.state" in q)
        for q,a in fake.calls)
    assert all(a[0] in (DID,OTHER) and a[1] in ("UNI","AVAX") for q,a in fake.calls)


def test_real_order_status_api_is_read_only_and_auth_required():
    from backend.app import app
    route=next(r for r in app.routes
        if r.path=="/api/live/evm/order-status/{asset}")
    source=inspect.getsource(route.endpoint)
    assert "verify_privy_bearer" in source
    assert route.methods=={"GET"}
    assert "get_owner_live_evm_order_status" in source
    assert "sign_privy" not in source


@pytest.mark.parametrize("asset",["ETH","DOGE","BTC","ADA"])
def test_non_additional_evm_assets_use_separate_existing_ledgers(asset):
    with pytest.raises(HTTPException):
        status.get_owner_live_evm_order_status(DID,asset=asset)


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
def test_completed_native_trade_and_fee_are_shown_only_after_verified_ledgers(asset):
    actual=row(asset,stage="FINALIZED_SUCCESS",side="SELL",
               native=True,fee=True)
    result=status._verified_order_row(actual,asset)
    assert result["native_trade_cycle_settled"] is True
    assert result["five_percent_hwm_accounting_recorded"] is True
    assert result["fee_transferred"] is False


@pytest.mark.parametrize("asset",["LINK","UNI","ARB"])
def test_erc20_sells_can_have_recorded_hwm_without_native_conversion(asset):
    actual=row(asset,stage="FINALIZED_SUCCESS",side="SELL",fee=True)
    result=status._verified_order_row(actual,asset)
    assert result["native_trade_cycle_settled"] is False
    assert result["five_percent_hwm_accounting_recorded"] is True


@pytest.mark.parametrize("alter",[
    lambda r:r.update(native_cycle_verified="true"),
    lambda r:r.update(hwm_fee_recorded=1),
    lambda r:r.update(status="SUBMITTED",native_cycle_verified=True),
    lambda r:r.update(status="FINALIZED_REVERTED",hwm_fee_recorded=True),
    lambda r:r.update(side="BUY",hwm_fee_recorded=True),
])
def test_unproved_or_invalid_completion_flags_are_rejected(alter):
    actual=row("AVAX",stage="FINALIZED_SUCCESS")
    alter(actual)
    with pytest.raises(ValueError):
        status._verified_order_row(actual,"AVAX")


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI","ARB"])
def test_only_verified_owner_payment_recognized_in_real_order_history(asset):
    result=status._verified_order_row(row(
        asset,stage="FINALIZED_SUCCESS",side="SELL",
        fee=True,paid=True,native=asset in ("BNB","POL","AVAX")),asset)
    assert result["five_percent_hwm_accounting_recorded"] is True
    assert result["fee_transferred"] is True


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI","ARB"])
def test_unassessed_or_fake_fee_payment_rejected(asset):
    with pytest.raises(ValueError):
        status._verified_order_row(row(
            asset,stage="FINALIZED_SUCCESS",side="SELL",
            fee=False,paid=True),asset)


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","ARB"])
@pytest.mark.parametrize("side",["BUY","SELL"])
def test_cross_chain_order_uses_actual_source_chain_and_parent_id(asset,side):
    r=row(asset,stage="FINALIZED_SUCCESS",side=side)
    r.update(ethereum_settlement_order=True,chain_id=1 if side=="BUY" else CHAIN[asset],
             intent_key="original-parent")
    parsed=status._verified_order_row(r,asset)
    assert parsed["ethereum_settlement_order"] is True
    assert parsed["intent_key"]=="original-parent"
    assert parsed["chain_id"]==r["chain_id"]
    with pytest.raises(ValueError):
        status._verified_order_row(dict(r,chain_id=CHAIN[asset] if side=="BUY" else 1),asset)


def test_staged_parent_appears_once_if_it_finishes_during_history_read(monkeypatch):
    active=dict(row("POL"),chain_id=1,ethereum_settlement_order=True,intent_key="original-parent")
    complete=dict(active,status="FINALIZED_SUCCESS")
    other=dict(active,intent_key="different-parent")
    fake=DB({(DID,"POL","staged"):[active],
             (DID,"POL","cross"):[complete,other],
             (OTHER,"POL","staged"):[dict(active,intent_key="other-owner")]})
    monkeypatch.setattr(status,"_db_url",lambda:"postgres://test")
    monkeypatch.setattr(status,"_ensure_schema",lambda url:None)
    monkeypatch.setattr(status.psycopg,"connect",lambda *a,**kw:fake)
    result=status.get_owner_live_evm_order_status(DID,asset="POL")
    orders={o["intent_key"]:o for o in result["orders"]}
    assert len(result["orders"])==2
    assert orders["original-parent"]["status"]=="FINALIZED_SUCCESS"
    assert orders["different-parent"]["status"]=="SUBMITTED"
    assert all(o["chain_id"]==1 for o in orders.values())


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","ARB"])
def test_cross_chain_order_history_executes_real_postgres_queries_in_ci(asset):
    """Exercise the real history joins with synthetic ledger rows, no signing."""
    import os
    import uuid
    import psycopg
    from psycopg.types.json import Jsonb
    from backend.ethereum_settlement_plan import ETHEREUM_STABLES,CROSS_COIN_CONTRACTS
    if os.getenv("CI","").lower()!="true" or not os.getenv("DATABASE_URL"):
        pytest.skip("Isolated PostgreSQL CI database required")
    db=status._db_url()
    status._ensure_schema(db)
    did="did:privy:history_ci_"+uuid.uuid4().hex
    order=uuid.uuid4()
    parent=uuid.uuid4()
    tx="0x"+uuid.uuid4().hex+uuid.uuid4().hex
    parent_tx="0x"+uuid.uuid4().hex+uuid.uuid4().hex
    dest_tx="0x"+uuid.uuid4().hex+uuid.uuid4().hex
    wallet="0x"+"ab"*20
    table="mininex_avax_staged" if asset=="AVAX" else "mininex_across_staged"
    chain=CHAIN[asset]
    try:
        with psycopg.connect(db) as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO mininex_accounts(privy_did) VALUES (%s)",(did,))
                columns="order_id,privy_did,side,stable,wallet_address,wallet_id,budget_revision,scope_json,started_by_owner_at,original_input_atomic,stage,remaining_atomic"
                params=(order,did,"BUY","USDC",wallet,"wallet_test",1,Jsonb({}),WHEN,1_000_000,2,900_000)
                if asset!="AVAX":
                    columns+=",asset"
                    params+=(asset,)
                cur.execute(f"INSERT INTO {table}_orders({columns}) VALUES ("+",".join(["%s"]*len(params))+")",params)
                cur.execute(f"""INSERT INTO {table}_steps
                    (step_id,order_id,privy_did,stage,kind,chain_id,prepared,request_sha256,state,tx_hash)
                    VALUES (%s,%s,%s,1,'BRIDGE',1,%s,%s,'FINALIZED',%s)""",
                    (uuid.uuid4(),order,did,Jsonb({}),"a"*64,tx))
                cur.execute("""INSERT INTO mininex_ethereum_settlement_quotes
                    (quote_id,privy_did,wallet_address,asset,side,stable,source_chain_id,destination_chain_id,
                     source_token,destination_token,input_atomic,minimum_output_atomic,issued_at,expires_at)
                    VALUES (%s,%s,%s,%s,'SELL','USDC',%s,1,%s,%s,1000,100,1,31)""",
                    (parent,did,wallet,asset,chain,CROSS_COIN_CONTRACTS[asset],ETHEREUM_STABLES["USDC"]))
                cur.execute("""INSERT INTO mininex_ethereum_settlement_submissions
                    (quote_id,privy_did,intent_key,source_chain_id,request_sha256,state,source_tx_hash)
                    VALUES (%s,%s,%s,%s,%s,'SUBMITTED',%s)""",
                    (parent,did,"ci_"+parent.hex,chain,"b"*64,parent_tx))
                cur.execute("""INSERT INTO mininex_ethereum_settlement_finality
                    (quote_id,privy_did,source_chain_id,destination_chain_id,source_tx_hash,destination_tx_hash,
                     bridge_deposit_id,received_atomic,source_gas_atomic,outcome)
                    VALUES (%s,%s,%s,1,%s,%s,%s,120,1,'FINALIZED')""",
                    (parent,did,chain,parent_tx,dest_tx,parent.int))
        result=status.get_owner_live_evm_order_status(did,asset=asset)
        orders={r["intent_key"]:r for r in result["orders"]}
        assert set(orders)=={str(order),str(parent)}
        assert orders[str(order)]["status"]=="SUBMITTED"
        assert orders[str(order)]["tx_hash"]==tx
        assert orders[str(order)]["chain_id"]==1
        assert orders[str(parent)]["status"]=="FINALIZED_SUCCESS"
        assert orders[str(parent)]["chain_id"]==chain
        assert orders[str(parent)]["tx_hash"]==parent_tx
        assert not status.get_owner_live_evm_order_status(did,asset="LINK")["orders"]
        assert not status.get_owner_live_evm_order_status("did:privy:history_ci_missing",asset=asset)["orders"]
    finally:
        with psycopg.connect(db) as conn:
            with conn.cursor() as cur:
                for name in (table+"_steps",table+"_orders",
                        "mininex_ethereum_settlement_finality","mininex_ethereum_settlement_submissions",
                        "mininex_ethereum_settlement_quotes","mininex_accounts"):
                    cur.execute(f"DELETE FROM {name} WHERE privy_did=%s",(did,))
