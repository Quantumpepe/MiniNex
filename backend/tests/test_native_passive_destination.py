"""Only verified original native chain payouts may complete a trade.

The original source is already finalized. No signing/broadcast/new orders.
"""
import asyncio
import pytest

from backend import native_passive_destination as finality

DID="did:privy:native_test"
KEY="native_"+"a"*64


def original(asset,side):
    return {"privy_did":DID,"asset":asset,"side":side,"intent_key":KEY,
            "status":"SOURCE_FINALIZED","stable":"USDC",
            "source_wallet":"owner-source","destination_wallet":"owner-dest",
            "source_tx_hash":"abc", "source_proof_sha256":"a"*64,
            "payload_sha256":"b"*64}


def test_owner_native_buy_sell_and_refund_require_original_finality(monkeypatch):
    rows=[original("BTC","BUY"),original("SOL","SELL"),original("XRP","SELL")]
    monkeypatch.setattr(finality,"original_native_sources_awaiting_destination",
                        lambda limit:rows)
    seen=[]
    async def buy(did,**kw):
        seen.append(("buy",kw["asset"]))
        return {"mode":"verified-buy"}
    async def sell(did,**kw):
        seen.append(("sell",kw["asset"]))
        if kw["asset"]=="XRP":
            raise ValueError("No SELL payout; check original refund")
        return {"mode":"verified-sell"}
    async def refund(did,**kw):
        seen.append(("refund",kw["asset"]))
        return {"mode":"verified-refund"}
    def save(*,original,proof,kind):
        seen.append(("saved",kind,original["asset"]))
        return "REFUNDED_VERIFIED" if kind=="REFUND" else "DESTINATION_FINALIZED"
    monkeypatch.setattr(finality,"get_private_original_native_buy_destination",buy)
    monkeypatch.setattr(finality,"get_private_native_ethereum_destination",sell)
    monkeypatch.setattr(finality,"get_private_original_native_sell_refund",refund)
    monkeypatch.setattr(finality,"_persist_original_verified_destination",save)
    result=asyncio.run(finality.reconcile_original_native_destinations_once())
    assert result=={
        "scanned":3,"destination_finalized":2,"refunds_verified":1,
        "unverified_or_unavailable":0,"new_transactions_sent":0,
        "fees_paid":False}
    assert sorted(x for x in seen if x[0]=="saved")==[
        ("saved","BUY","BTC"),("saved","REFUND","XRP"),("saved","SELL","SOL")]


def test_no_original_source_finality_never_calls_network_or_sends(monkeypatch):
    bad=original("BTC","BUY")
    bad["status"]="PREPARED"
    monkeypatch.setattr(finality,"original_native_sources_awaiting_destination",
                        lambda limit:[bad])
    async def network(*args,**kwargs):
        pytest.fail("Unconfirmed original source must not request payout proof")
    monkeypatch.setattr(finality,"get_private_original_native_buy_destination",network)
    result=asyncio.run(finality.reconcile_original_native_destinations_once())
    assert result["unverified_or_unavailable"]==1
    assert result["destination_finalized"]==0
    assert result["new_transactions_sent"]==0


def test_native_destination_original_owner_work_page_rotates(monkeypatch):
    history=[original("BTC","BUY") | {"intent_key":"native_"+str(i)*64}
             for i in range(5)]
    params=[]
    class DB:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kw):return self
        def execute(self,sql,args):
            self.limit,self.offset=args
            params.append(args)
            assert "status='SOURCE_FINALIZED'" in sql
            assert "source_proof_sha256 IS NOT NULL" in sql
        def fetchall(self):
            return history[self.offset:self.offset+self.limit]
    monkeypatch.setattr(finality,"_db_url",lambda:"postgres://test")
    monkeypatch.setattr(finality,"_ensure_schema",lambda db:None)
    monkeypatch.setattr(finality.psycopg,"connect",lambda *a,**kw:DB())
    monkeypatch.setattr(finality,"_WORK_PAGE_OFFSET",0)
    pages=[finality.original_native_sources_awaiting_destination(2)
           for _ in range(4)]
    assert pages==[history[:2],history[2:4],history[4:],history[:2]]
    assert params==[(2,0),(2,2),(2,4),(2,5),(2,0)]
