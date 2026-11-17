"""A single EVM quote is reserved and submitted without a second quote."""
import asyncio
import inspect

import httpx
import pytest
from fastapi import HTTPException

from backend import shared_evm_strategy_execution as run
from backend.shared_evm_coin_budget_reserve import _cancel_unsubmitted_locked

DID="did:privy:owner"
RUN="owner_session_20261117_01"
EVENT="market_event_00000001"
KEY=run.derive_evm_strategy_signal_key(
    did=DID,asset="LINK",side="BUY",stable="USDC",
    strategy_run_id=RUN,signal_event_id=EVENT)
SHA="a"*64
ASSETS={"BNB":"USDT","POL":"USDC","AVAX":"USDC",
        "LINK":"USDC","UNI":"USDC"}


def use_mocks(monkeypatch,*,asset="LINK",fail=False):
    actions=[]
    quote={"asset":asset,"verified":True}
    async def prepare(*args,**kwargs):
        actions.append("onchain_quote")
        return quote
    def digest(payload):
        assert payload is quote
        actions.append("quote_digest")
        return SHA
    def reserve(**kw):
        actions.append("reserve")
        assert kw["quote_sha256"]==SHA
        assert kw["asset"]==asset
        assert kw["intent_key"]==run.derive_evm_strategy_signal_key(
            did=DID,asset=asset,side="BUY",stable=ASSETS[asset],
            strategy_run_id=RUN,signal_event_id=EVENT)
        return {"state":"RESERVED"}
    async def send(**kw):
        actions.append("privy_send")
        assert kw["prepared_quote"] is quote
        if fail:
            raise HTTPException(503,"Privy submission outcome uncertain")
        return {"state":"SUBMITTED","tx_hash":"0x"+"ab"*32}
    def cancel(**kw):
        actions.append("cancel_if_unclaimed")
        assert kw["quote_sha256"]==SHA
        return not fail
    monkeypatch.setattr(run,"prepare_owner_evm_swap",prepare)
    monkeypatch.setattr(run,"_fingerprint_prepared_quote",digest)
    monkeypatch.setattr(run,"reserve_internal_evm_coin_budget",reserve)
    monkeypatch.setattr(run,"submit_internal_privy_evm_order",send)
    monkeypatch.setattr(run,"cancel_internal_unsubmitted_evm_reservation",cancel)
    return actions


async def execute(asset):
    async with httpx.AsyncClient() as client:
        return await run.execute_internal_evm_strategy_signal(
            did=DID,asset=asset,strategy_run_id=RUN,
            signal_event_id=EVENT,side="BUY",stable=ASSETS[asset],
            amount="10",amount_usd="100",
            budget_revision=3,client=client)


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI"])
def test_five_coins_quote_once_reserve_then_send_identical_quote(monkeypatch,asset):
    actions=use_mocks(monkeypatch,asset=asset)
    assert asyncio.run(execute(asset))["state"]=="SUBMITTED"
    assert actions==[
        "onchain_quote","quote_digest","reserve","privy_send",
    ]


def test_ambiguous_send_never_requotes_or_automatically_retries(monkeypatch):
    actions=use_mocks(monkeypatch,fail=True)
    with pytest.raises(HTTPException):
        asyncio.run(execute("LINK"))
    assert actions==[
        "onchain_quote","quote_digest","reserve","privy_send",
        "cancel_if_unclaimed",
    ]
    assert actions.count("privy_send")==1


def test_failed_reservation_never_invokes_privy_send(monkeypatch):
    actions=use_mocks(monkeypatch)
    def reject(**kw):
        actions.append("reserve_rejected")
        raise HTTPException(409,"Owner budget no longer available")
    monkeypatch.setattr(run,"reserve_internal_evm_coin_budget",reject)
    with pytest.raises(HTTPException):
        asyncio.run(execute("LINK"))
    assert actions==[
        "onchain_quote","quote_digest","reserve_rejected",
    ]


class Cursor:
    def __init__(self,items):
        self.rows=iter(items)
        self.queries=[]
    def execute(self,sql,args):
        self.queries.append((sql,args))
    def fetchone(self):
        return next(self.rows)


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI"])
def test_never_submitted_order_may_release_just_its_coin_budget(asset):
    cur=Cursor([
        {"privy_did":DID},None,{"state":"CANCELLED"},
    ])
    released=_cancel_unsubmitted_locked(
        cur,did=DID,asset=asset,key=KEY,quote_sha256=SHA)
    assert released is True
    assert len(cur.queries)==3
    assert "FOR UPDATE" in cur.queries[0][0]
    assert "mininex_live_evm_auto_broadcasts" in cur.queries[1][0]
    assert "AND state='RESERVED'" in cur.queries[2][0]
    assert cur.queries[2][1]==(DID,asset,KEY,SHA)


@pytest.mark.parametrize("status",[
    "SENDING","SUBMITTED","RECONCILIATION_REQUIRED",
    "FINALIZED_SUCCESS","FINALIZED_REVERTED",
])
def test_original_broadcast_never_releases_user_budget(status):
    cur=Cursor([
        {"privy_did":DID},{"status":status},
    ])
    released=_cancel_unsubmitted_locked(
        cur,did=DID,asset="POL",key=KEY,quote_sha256=SHA)
    assert released is False
    assert len(cur.queries)==2
    assert not any("UPDATE mininex_live_evm_budget_reservations" in q
                   for q,args in cur.queries)


def test_cancelled_reservation_cannot_replay_original_signal():
    cur=Cursor([{"privy_did":DID},None,None])
    assert _cancel_unsubmitted_locked(
        cur,did=DID,asset="UNI",key=KEY,quote_sha256=SHA) is False
    assert "state='RESERVED'" in cur.queries[-1][0]


def test_coordinator_is_not_exposed_as_user_api_or_automatic_scheduler():
    from backend.app import app
    assert all("strategy-execute" not in r.path for r in app.routes)
    src=inspect.getsource(run)
    assert "onchain_quote" not in src  # no mocked price shortcut
    assert "submit_internal_privy_evm_order" in src
    assert "reserve_internal_evm_coin_budget" in src


def test_original_signal_identity_stable_across_trade_terms():
    args=dict(did=DID,asset="LINK",strategy_run_id=RUN,signal_event_id=EVENT)
    buy=run.derive_evm_strategy_signal_key(**args,side="BUY",stable="USDC")
    sell=run.derive_evm_strategy_signal_key(**args,side="SELL",stable="USDT")
    assert buy==sell
    assert buy.startswith("sig_")
    assert run.derive_evm_strategy_signal_key(
        **{**args,"signal_event_id":"market_event_00000002"},
        side="BUY",stable="USDC")!=buy
    assert run.derive_evm_strategy_signal_key(
        **{**args,"asset":"UNI"},
        side="BUY",stable="USDC")!=buy


@pytest.mark.parametrize("run_id,event_id",[
    ("short","market_event_00000001"),
    ("owner_session_20261117_01","short"),
    ("bad event containing spaces","market_event_00000001"),
    ("owner_session_20261117_01","!invalid!"),
])
def test_invalid_strategy_provenance_refused_before_quote(monkeypatch,run_id,event_id):
    actions=use_mocks(monkeypatch)
    async def attempt():
        async with httpx.AsyncClient() as client:
            return await run.execute_internal_evm_strategy_signal(
                did=DID,asset="LINK",strategy_run_id=run_id,
                signal_event_id=event_id,side="BUY",stable="USDC",
                amount="10",amount_usd="100",budget_revision=3,
                client=client)
    with pytest.raises(ValueError):
        asyncio.run(attempt())
    assert actions==[]
