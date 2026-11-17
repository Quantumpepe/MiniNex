"""One shared, owner-started EVM worker; never inherits PAPER sessions."""
import asyncio
from datetime import datetime,timezone
from decimal import Decimal

import httpx
import pytest

from backend import shared_evm_live_worker as worker
from backend.shared_evm_policy_reference_rates import _IDS
from backend.shared_evm_swap_receipts import _pair

DID="did:privy:owner"
START=datetime(2026,11,17,16,10,23,34210,tzinfo=timezone.utc)
ASSETS={"BNB":"USDT","POL":"USDC","AVAX":"USDC",
        "LINK":"USDC","UNI":"USDC"}


def session(asset="LINK"):
    return dict(privy_did=DID,asset=asset,stable=ASSETS[asset],
                chain_id=_pair(asset,ASSETS[asset],"BUY")["chain_id"],
                budget_revision=7,budget_usd=2000,
                mode="AUTO",horizon="15m",
                pending_buy_usd=Decimal("0"),started_by_owner_at=START)


def evidence(asset,side,source="2500",allowance="3000",destination="0"):
    pair=_pair(asset,ASSETS[asset],side)
    return {
        "mode":"SHARED_EVM_SOURCE_ALLOWANCE_READ_ONLY",
        "asset":asset,"chain_id":pair["chain_id"],
        "network":pair["network"],"side":side,
        "stable":ASSETS[asset],
        "source_token_address":pair["source_token"],
        "destination_token_address":pair["output_token"],
        "destination_balance":destination if side=="BUY" else None,
        "native_gas_balance_atomic":"0",
        "source_balance":source,"router_allowance":allowance,
        "wallet_address":"0x"+"ab"*20,
        "wallet_identity_verified":True,
        "native_gas_available":True,
        "router_allowance_sufficient":True,
        "source_funds_sufficient":True,
    }


def rates(asset,stable):
    import time
    now=int(time.time())
    return {_IDS[k]:{"usd":v,"last_updated_at":now}
            for k,v in ((asset,8.0),(stable,1.0))}


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI"])
def test_worker_requires_exact_opt_in_and_database(monkeypatch,asset):
    monkeypatch.delenv("MININEX_EVM_AUTO_WORKER_ENABLED",raising=False)
    monkeypatch.setenv("DATABASE_URL","postgres://test.invalid")
    assert worker.evm_auto_worker_enabled() is False
    monkeypatch.setenv("MININEX_EVM_AUTO_WORKER_ENABLED","explicit-live")
    monkeypatch.delenv("MININEX_LIVE_TRADING_ENABLED",raising=False)
    assert worker.evm_auto_worker_enabled() is False
    monkeypatch.setenv("MININEX_LIVE_TRADING_ENABLED","true")
    assert worker.evm_auto_worker_enabled() is True
    assert worker._trusted_live_session(session(asset))["asset"]==asset


@pytest.mark.parametrize("asset",["LINK","UNI"])
def test_buy_can_use_full_user_budget_without_system_trade_caps(asset):
    proof=evidence(asset,"BUY",source="5000",allowance="5000")
    got=worker.exact_user_coin_live_amount(
        asset=asset,side="BUY",stable=ASSETS[asset],
        budget_usd=2000,source_evidence=proof,
        prices=rates(asset,ASSETS[asset]),now=__import__("time").time().__int__())
    assert Decimal(got)==2000


@pytest.mark.parametrize("asset",["LINK","UNI"])
def test_appreciated_position_can_sell_all_owned_approved_tokens(asset):
    proof=evidence(asset,"SELL",source="2500",allowance="2400")
    got=worker.exact_user_coin_live_amount(
        asset=asset,side="SELL",stable=ASSETS[asset],
        budget_usd=100,source_evidence=proof,
        prices=rates(asset,ASSETS[asset]),now=int(__import__("time").time()))
    assert Decimal(got)==2400


@pytest.mark.parametrize("change",[
    lambda e:e.update(wallet_identity_verified=False),
    lambda e:e.update(native_gas_available=False),
    lambda e:e.update(asset="UNI"),
    lambda e:e.update(source_token_address="0x"+"ab"*20),
    lambda e:e.update(source_balance="-1"),
    lambda e:e.update(router_allowance="0"),
])
def test_missing_wallet_source_or_allowance_never_forms_live_order(change):
    proof=evidence("LINK","BUY")
    change(proof)
    assert worker.exact_user_coin_live_amount(
        asset="LINK",side="BUY",stable="USDC",
        budget_usd=100,source_evidence=proof,
        prices=rates("LINK","USDC"),now=int(__import__("time").time())) is None


def use_tick_mocks(monkeypatch,asset="LINK",action="BUY"):
    actions=[]
    def market_decision(market,*,asset,now):
        actions.append("verified_market")
        return {"action":action,"market_price":"8"}
    async def allowance(did,**kw):
        actions.append(("onchain_source",kw["amount"]))
        return evidence(asset,kw["side"],source="5000",allowance="5000")
    async def price(asset,stable):
        actions.append("usd_prices")
        snapshot=rates(asset,stable)
        for record in snapshot.values():
            record["last_updated_at"]=10000
        return snapshot
    async def execute(**kw):
        actions.append(("original_single_execution",kw["amount"]))
        assert kw["asset"]==asset
        assert kw["strategy_run_id"].startswith("owner_20261117")
        assert kw["signal_event_id"]=="market_166"
        assert kw["budget_revision"]==7
        return {"state":"SUBMITTED","tx_hash":"0x"+"cd"*32}
    monkeypatch.setattr(worker,"verified_live_evm_momentum",market_decision)
    monkeypatch.setattr(worker,"check_evm_source_allowance",allowance)
    monkeypatch.setattr(worker,"fetch_evm_policy_reference_rates",price)
    monkeypatch.setattr(worker,"execute_internal_evm_strategy_signal",execute)
    return actions


@pytest.mark.parametrize("asset",["LINK","UNI"])
@pytest.mark.parametrize("action",["BUY","SELL"])
def test_owner_started_live_token_tick_uses_real_wallet_and_single_signaling(
    monkeypatch,asset,action
):
    actions=use_tick_mocks(monkeypatch,asset=asset,action=action)
    async def tick():
        async with httpx.AsyncClient() as client:
            return await worker.execute_owner_evm_live_tick(
                session(asset),market=object(),client=client,now=10000)
    assert asyncio.run(tick())["state"]=="SUBMITTED"
    assert actions[0]=="verified_market"
    assert len([a for a in actions if isinstance(a,tuple) and
                a[0]=="original_single_execution"])==1
    assert len([a for a in actions if isinstance(a,tuple) and
                a[0]=="onchain_source"])==1


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
def test_native_evm_sell_never_uses_unwrapped_native_without_deposit(monkeypatch,asset):
    actions=use_tick_mocks(monkeypatch,asset=asset,action="SELL")
    async def tick():
        async with httpx.AsyncClient() as client:
            return await worker.execute_owner_evm_live_tick(
                session(asset),market=object(),client=client,now=10000)
    result=asyncio.run(tick())
    assert result["state"]=="WAIT"
    assert result["reason"]=="NATIVE_SELL_DEPOSIT_NOT_CONNECTED"
    assert actions==["verified_market"]


def test_unverified_market_signal_cannot_touch_wallet(monkeypatch):
    actions=use_tick_mocks(monkeypatch)
    def wait(*args,**kwargs):
        actions.append("WAIT")
        return {"action":"WAIT","reason":"LIVE_MARKET_DATA_UNVERIFIED"}
    monkeypatch.setattr(worker,"verified_live_evm_momentum",wait)
    async def tick():
        async with httpx.AsyncClient() as client:
            return await worker.execute_owner_evm_live_tick(
                session(),market=object(),client=client,now=10000)
    assert asyncio.run(tick())["state"]=="WAIT"
    assert actions==["WAIT"]


class DB:
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def cursor(self,**kwargs):return self
    def execute(self,q,args):self.sql=q;self.args=args
    def fetchall(self):return [session("LINK"),session("POL")]


def test_worker_scans_only_real_owner_started_current_granted_sessions(monkeypatch):
    db=DB()
    monkeypatch.setattr(worker,"evm_auto_worker_enabled",lambda:True)
    monkeypatch.setattr(worker,"_db_url",lambda:"postgres://test.invalid")
    monkeypatch.setattr(worker,"_ensure_schema",lambda url:None)
    monkeypatch.setattr(worker.psycopg,"connect",lambda *a,**kw:db)
    rows=worker.owner_started_evm_sessions(limit=10)
    assert len(rows)==2
    assert "s.status='ACTIVE'" in db.sql
    assert "g.status='GRANT_VERIFIED'" in db.sql
    assert "p.revision=s.budget_revision" in db.sql
    assert "s.started_by_owner_at IS NOT NULL" in db.sql
    assert "paper" not in db.sql.lower()


def test_worker_is_wired_to_lifespan_and_disabled_by_default(monkeypatch):
    from backend.app import evm_auto_live
    assert isinstance(evm_auto_live,worker.SharedEvmLiveWorker)
    monkeypatch.delenv("MININEX_EVM_AUTO_WORKER_ENABLED",raising=False)
    assert worker.evm_auto_worker_enabled() is False
    assert evm_auto_live._task is None


@pytest.mark.parametrize("asset",["LINK","UNI"])
def test_existing_verified_coin_position_is_deducted_from_buying_budget(asset):
    proof=evidence(asset,"BUY",source="10000",
                   allowance="10000",destination="150")
    result=worker.exact_user_coin_live_amount(
        asset=asset,side="BUY",stable=ASSETS[asset],
        budget_usd=2000,source_evidence=proof,
        prices=rates(asset,ASSETS[asset]),
        now=int(__import__("time").time()))
    assert Decimal(result)==800


@pytest.mark.parametrize("mutate",[
    lambda p:p.update(destination_token_address="0x"+"cd"*20),
    lambda p:p.update(destination_balance="-5"),
    lambda p:p.update(destination_balance="NaN"),
    lambda p:p.update(destination_balance=None),
])
def test_untrusted_target_coin_inventory_cannot_authorize_extra_buy(mutate):
    proof=evidence("LINK","BUY",source="5000",allowance="5000")
    mutate(proof)
    assert worker.exact_user_coin_live_amount(
        asset="LINK",side="BUY",stable="USDC",budget_usd=2000,
        source_evidence=proof,
        prices=rates("LINK","USDC"),
        now=int(__import__("time").time())) is None


def test_fully_allocated_coin_budget_does_not_buy_again_from_other_coin_cash():
    proof=evidence("LINK","BUY",source="5000",
                   allowance="5000",destination="250")
    assert worker.exact_user_coin_live_amount(
        asset="LINK",side="BUY",stable="USDC",budget_usd=2000,
        source_evidence=proof,
        prices=rates("LINK","USDC"),
        now=int(__import__("time").time())) is None


def test_outstanding_buy_reservations_reduce_available_user_budget():
    proof=evidence("LINK","BUY",source="5000",allowance="5000")
    amount=worker.exact_user_coin_live_amount(
        asset="LINK",side="BUY",stable="USDC",budget_usd=2000,
        pending_buy_usd=Decimal("750"),
        source_evidence=proof,prices=rates("LINK","USDC"),
        now=int(__import__("time").time()))
    assert Decimal(amount)==1250


@pytest.mark.parametrize("invalid",[None,"NaN","Infinity","-1","oops"])
def test_live_session_rejects_uncertain_reservations(invalid):
    r=session()
    r["pending_buy_usd"]=invalid
    with pytest.raises(ValueError):
        worker._trusted_live_session(r)


def test_5min_horizon_is_owner_supported_setting():
    r=session()
    r["horizon"]="5m"
    assert worker._trusted_live_session(r)["horizon"]=="5m"
