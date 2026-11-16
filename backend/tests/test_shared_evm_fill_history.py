"""Authenticated MiniNex real EVM history shows only each coin's settled swaps."""
from datetime import datetime,timezone
from decimal import Decimal
import inspect

import pytest
from fastapi import HTTPException

from backend import shared_evm_fill_history as h
from backend.shared_evm_swap_receipts import _pair

DID="did:privy:owner"
OTHER="did:privy:other"
TX="0x"+"ab"*32
WALLET="0x"+"cd"*20


def record(asset,side="BUY",stable=None):
    stable=stable or ("USDT" if asset=="BNB" else "USDC")
    p=_pair(asset,stable,side)
    return {
        "asset":asset,"chain_id":p["chain_id"],
        "intent_key":"sig_012345678901234567890",
        "tx_hash":TX,"wallet_address":WALLET,
        "side":side,"stable":stable,
        "source_amount":Decimal("1.1"),
        "destination_amount":Decimal("2.2"),
        "network_fee_native":Decimal("0.0002"),
        "created_at":datetime(2026,11,16,tzinfo=timezone.utc),
    }


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI"])
@pytest.mark.parametrize("side",["BUY","SELL"])
def test_each_real_evm_coin_has_separate_finalized_buy_sell_history(asset,side):
    v=h._verified_fill_row(record(asset,side),asset)
    assert v["asset"]==asset
    assert v["side"]==side
    assert v["onchain_finalized"] is True
    assert v["spent_asset"]==(("USDT" if asset=="BNB" else "USDC") if side=="BUY" else asset)
    assert v["realized_net_usd"] is None
    assert v["performance_fee_paid"] is False
    assert v["native_coin_cycle_complete"] is False


class FakeDB:
    def __init__(self,items):
        self.items=items
        self.calls=[]
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def cursor(self,**kw):return self
    def execute(self,query,params):
        self.calls.append((query,params))
    def fetchall(self):
        _,(did,asset,limit)=self.calls[-1]
        return self.items.get((did,asset),[])[:limit]


def test_history_is_owner_and_asset_scoped_with_truncation(monkeypatch):
    fake=FakeDB({(DID,"UNI"):[record("UNI") for _ in range(3)],
                 (OTHER,"UNI"):[record("UNI")],
                 (DID,"AVAX"):[record("AVAX")]})
    monkeypatch.setattr(h,"_db_url",lambda:"postgresql://fake")
    monkeypatch.setattr(h,"_ensure_schema",lambda url:None)
    monkeypatch.setattr(h.psycopg,"connect",lambda *args,**kw:fake)
    own=h.get_owner_evm_fill_history(DID,asset="UNI",limit=2)
    assert len(own["fills"])==2
    assert own["history_truncated"] is True
    assert own["includes_simulation"] is False
    assert own["performance_fee_settled"] is False
    assert h.get_owner_evm_fill_history(OTHER,asset="UNI")["fills"]
    assert not h.get_owner_evm_fill_history(OTHER,asset="AVAX")["fills"]
    assert all("WHERE privy_did=%s AND asset=%s" in q for q,p in fake.calls)
    assert all(p[0] in (DID,OTHER) for q,p in fake.calls)


@pytest.mark.parametrize("change",[
    lambda x:x.update(asset="LINK"),
    lambda x:x.update(chain_id=56),
    lambda x:x.update(source_amount=Decimal("-1")),
    lambda x:x.update(network_fee_native=None),
    lambda x:x.update(created_at=datetime(2026,11,16)),
    lambda x:x.update(side="PAPER"),
])
def test_unverified_or_paper_fills_cannot_appear_in_live_history(change):
    row=record("UNI")
    change(row)
    with pytest.raises((ValueError,HTTPException)):
        h._verified_fill_row(row,"UNI")


def test_live_history_endpoint_requires_privy_bearer_and_no_write():
    from backend.app import app
    r=next(r for r in app.routes
           if r.path=="/api/live/evm/verified-fills/{asset}")
    text=inspect.getsource(r.endpoint)
    assert "verify_privy_bearer" in text
    assert "get_owner_evm_fill_history" in text
    assert "send_privy" not in text
    assert all("verified-fills" not in r.path or "GET" in r.methods
               for r in app.routes)
