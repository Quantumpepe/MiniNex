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
        "LINK":"USDC","UNI":"USDC","ARB":"USDC"}


def use_mocks(monkeypatch,*,asset="LINK",fail=False):
    actions=[]
    quote={"asset":asset,"verified":True,"source_balance_atomic":"100000000"}
    async def prepare(*args,**kwargs):
        actions.append("onchain_quote")
        return quote
    def digest(payload):
        assert payload is quote
        actions.append("quote_digest")
        return SHA
    def reserve(**kw):
        actions.append("reserve")
        assert kw["amount_usd"]=="100.000000"
        assert kw["quote_sha256"]==SHA
        assert kw["asset"]==asset
        if asset in ("ETH","LINK","QNT","UNI"):
            assert kw["ethereum_funding"]["input_atomic"]==10_000_000
            assert kw["ethereum_funding"]["balance_atomic"]==100_000_000
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
    async def prices(*args):
        actions.append("reference_prices")
        return {"fresh":True}
    def usd(**kw):
        actions.append("verified_quote_usd")
        assert kw["prepared"] is quote
        assert kw["prices"]=={"fresh":True}
        return "100.000000"
    monkeypatch.setattr(run,"_price",lambda *args: __import__("decimal").Decimal("1"))
    monkeypatch.setattr(run,"fetch_evm_policy_reference_rates",prices)
    monkeypatch.setattr(run,"verified_evm_quote_usd_notional",usd)
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
            amount="10",
            budget_revision=3,client=client)


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI","ARB"])
def test_five_coins_quote_once_reserve_then_send_identical_quote(monkeypatch,asset):
    actions=use_mocks(monkeypatch,asset=asset)
    assert asyncio.run(execute(asset))["state"]=="SUBMITTED"
    assert actions==[
        "onchain_quote","quote_digest","reference_prices",
        "verified_quote_usd","reserve","privy_send",
    ]


def test_ambiguous_send_never_requotes_or_automatically_retries(monkeypatch):
    actions=use_mocks(monkeypatch,fail=True)
    with pytest.raises(HTTPException):
        asyncio.run(execute("LINK"))
    assert actions==[
        "onchain_quote","quote_digest","reference_prices",
        "verified_quote_usd","reserve","privy_send",
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
        "onchain_quote","quote_digest","reference_prices",
        "verified_quote_usd","reserve_rejected",
    ]


class Cursor:
    def __init__(self,items):
        self.rows=iter(items)
        self.queries=[]
    def execute(self,sql,args):
        self.queries.append((sql,args))
    def fetchone(self):
        return next(self.rows)


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI","ARB"])
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
                amount="10",budget_revision=3,
                client=client)
    with pytest.raises(ValueError):
        asyncio.run(attempt())
    assert actions==[]


def _prepared_quote(asset,side,stable,amount):
    from backend.shared_evm_execution import EvmPair,unsigned_exact_input_single
    from backend.shared_evm_swap_receipts import _pair
    from backend.live_eth_quotes import _quantity
    pair=_pair(asset,stable,side)
    _,units=_quantity(amount,pair["source_decimals"])
    tx=unsigned_exact_input_single(
        pair=EvmPair(asset,pair["chain_id"],pair["network"],
                     pair["source_token"],pair["output_token"],
                     pair["router"],500),
        wallet="0x"+"ab"*20,amount_in=int(units),min_out=12345)
    return dict(asset=asset,side=side,stable=stable,
                chain_id=pair["chain_id"],network=pair["network"],
                input_amount=amount,
                source_token_address=pair["source_token"],
                output_token_address=pair["output_token"],
                wallet_address="0x"+"ab"*20,
                unsigned_transaction=tx)


@pytest.mark.parametrize("asset,stable",[
    ("BNB","USDT"),("POL","USDC"),("AVAX","USDC"),
    ("LINK","USDC"),("UNI","USDC"),("ARB","USDC"),
])
@pytest.mark.parametrize("side",["BUY","SELL"])
def test_real_calldata_determines_budget_usd_not_strategy_guess(asset,stable,side):
    from backend.shared_evm_policy_reference_rates import _IDS
    from decimal import Decimal,ROUND_CEILING
    import time
    now=int(time.time())
    amount="100" if side=="BUY" else "2"
    quote=_prepared_quote(asset,side,stable,amount)
    prices={_IDS[name]:{"usd":float(rate),"last_updated_at":now}
            for name,rate in [(asset,"7.137"),(stable,"1.001")]}
    amount_usd=run.verified_evm_quote_usd_notional(
        prepared=quote,asset=asset,side=side,stable=stable,
        amount=amount,prices=prices,now=now)
    expected=(Decimal(amount)*Decimal("1.001") if side=="BUY"
              else Decimal(amount)*Decimal("7.137"))
    assert Decimal(amount_usd)==expected.quantize(
        Decimal("0.000001"),rounding=ROUND_CEILING)


@pytest.mark.parametrize("tamper",[
    lambda q:q.update(asset="UNI"),
    lambda q:q.update(chain_id=56),
    lambda q:q.update(stable="USDT"),
    lambda q:q.update(input_amount="0.01"),
    lambda q:q.update(wallet_address="0x"+"ee"*20),
    lambda q:q.update(source_token_address="0x"+"ee"*20),
])
def test_quote_terms_tampering_cannot_reduce_reserved_budget(tamper):
    from backend.shared_evm_policy_reference_rates import _IDS
    import time
    now=int(time.time())
    quote=_prepared_quote("LINK","BUY","USDC","100")
    tamper(quote)
    rates={_IDS["USDC"]:{"usd":1.0,"last_updated_at":now}}
    with pytest.raises(ValueError):
        run.verified_evm_quote_usd_notional(
            prepared=quote,asset="LINK",side="BUY",stable="USDC",
            amount="100",prices=rates,now=now)


def test_missing_fresh_usd_rate_cannot_reserve_or_send(monkeypatch):
    actions=use_mocks(monkeypatch)
    def reject(**kwargs):
        actions.append("bad_reference")
        raise ValueError("Missing fresh USD rate")
    monkeypatch.setattr(run,"verified_evm_quote_usd_notional",reject)
    with pytest.raises(ValueError):
        asyncio.run(execute("LINK"))
    assert actions==["onchain_quote","quote_digest",
                     "reference_prices","bad_reference"]
