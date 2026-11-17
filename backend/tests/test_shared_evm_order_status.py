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
CHAIN={"BNB":56,"POL":137,"AVAX":43114,"LINK":1,"UNI":1}


def row(asset,stage="SUBMITTED",stable=None):
    return {
        "asset":asset,"chain_id":CHAIN[asset],
        "intent_key":"sig_abcdef01234567890123456789",
        "side":"BUY","stable":stable or (
            "USDT" if asset=="BNB" else "USDC"),
        "status":stage,
        "tx_hash":TX if stage not in ("SENDING","RECONCILIATION_REQUIRED") else None,
        "created_at":WHEN,"updated_at":WHEN,
    }


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI"])
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
        did,asset,limit=self.calls[-1][1]
        return self.items.get((did,asset),[])[:limit]


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
    assert all("WHERE privy_did=%s AND asset=%s" in q for q,a in fake.calls)


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
