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


def test_native_destination_keyset_page_rotates_without_skipping_removed_rows(monkeypatch):
    """Already settled rows disappearing cannot shift later rows past OFFSET."""
    from datetime import datetime,timezone,timedelta
    start=datetime(2026,11,21,tzinfo=timezone.utc)
    history=[
        original("BTC","BUY")|{
            "intent_key":"native_"+f"{i:064x}",
            "source_finalized_at":start+timedelta(minutes=i),
        } for i in range(5)
    ]
    queries=[]
    class DB:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kw):return self
        def execute(self,sql,args):
            assert "status='SOURCE_FINALIZED'" in sql
            assert "source_proof_sha256 IS NOT NULL" in sql
            assert "ORDER BY source_finalized_at" in sql
            queries.append((sql,args))
            self.after=args[:-1] if len(args)>1 else None
            self.limit=args[-1]
        def fetchall(self):
            candidates=history
            if self.after is not None:
                candidates=[
                    row for row in history if (
                        row["source_finalized_at"],row["privy_did"],
                        row["asset"],row["intent_key"])>tuple(self.after)
                ]
            return candidates[:self.limit]
    monkeypatch.setattr(finality,"_db_url",lambda:"postgres://test")
    monkeypatch.setattr(finality,"_ensure_schema",lambda db:None)
    monkeypatch.setattr(finality.psycopg,"connect",lambda *a,**kw:DB())
    monkeypatch.setattr(finality,"_WORK_PAGE_CURSOR",None)
    first=finality.original_native_sources_awaiting_destination(2)
    assert first==history[:2]
    # These two outgoing transfers were finalized; the DB predicate
    # no longer returns them. The keyset must still see every later order.
    del history[:2]
    second=finality.original_native_sources_awaiting_destination(2)
    assert second==history[:2]
    del history[:2]
    third=finality.original_native_sources_awaiting_destination(2)
    assert third==history[:1]
    fourth=finality.original_native_sources_awaiting_destination(2)
    assert fourth==history[:1]  # wrap at end to recheck pending
    assert len(queries)==5
    assert len(queries[0][1])==1
    assert len(queries[1][1])==5
    assert len(queries[2][1])==5
    assert len(queries[3][1])==5
    assert len(queries[4][1])==1


def test_native_buy_fallback_to_verified_original_eth_stable_refund(monkeypatch):
    row=original("BTC","BUY")
    monkeypatch.setattr(finality,"original_native_sources_awaiting_destination",
                        lambda limit:[row])
    async def missing_payout(*args,**kw):
        raise ValueError("No BTC mainnet payout")
    async def ethereum_refund(did,*,asset,intent_key):
        assert did==DID and asset=="BTC" and intent_key==KEY
        return {"mode":"ORIGINAL_NATIVE_BUY_ETH_STABLE_REFUND_FINALITY"}
    saved=[]
    def persist(*,original,proof,kind):
        saved.append((original["asset"],kind))
        return "REFUNDED_VERIFIED"
    monkeypatch.setattr(finality,"get_private_original_native_buy_destination",missing_payout)
    monkeypatch.setattr(finality,"get_private_original_native_buy_refund",ethereum_refund)
    monkeypatch.setattr(finality,"_persist_original_verified_destination",persist)
    result=asyncio.run(finality.reconcile_original_native_destinations_once())
    assert result["refunds_verified"]==1
    assert result["destination_finalized"]==0
    assert result["new_transactions_sent"]==0
    assert saved==[("BTC","BUY_REFUND")]
