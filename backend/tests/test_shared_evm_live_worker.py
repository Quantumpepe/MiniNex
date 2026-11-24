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
ASSETS={"ETH":"USDC","BNB":"USDT","POL":"USDC","AVAX":"USDC",
        "LINK":"USDC","UNI":"USDC","ARB":"USDC"}


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
        "router_allowance_atomic":str(int(Decimal(allowance)*
                                      Decimal(10)**pair["source_decimals"])),
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


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI","ARB"])
def test_owner_session_no_longer_needs_operator_live_switch(monkeypatch,asset):
    monkeypatch.delenv("DATABASE_URL",raising=False)
    assert worker.evm_auto_worker_enabled() is False
    monkeypatch.delenv("MININEX_EVM_AUTO_WORKER_ENABLED",raising=False)
    monkeypatch.delenv("MININEX_LIVE_TRADING_ENABLED",raising=False)
    monkeypatch.setenv("DATABASE_URL","postgres://test.invalid")
    assert worker.evm_auto_worker_enabled() is True
    monkeypatch.setenv("MININEX_EVM_AUTO_WORKER_ENABLED","disabled")
    monkeypatch.setenv("MININEX_LIVE_TRADING_ENABLED","false")
    assert worker.evm_auto_worker_enabled() is True
    assert worker._trusted_live_session(session(asset))["asset"]==asset


@pytest.mark.parametrize("asset",["LINK","UNI","ARB"])
def test_buy_can_use_full_user_budget_without_system_trade_caps(asset):
    proof=evidence(asset,"BUY",source="5000",allowance="5000")
    got=worker.exact_user_coin_live_amount(
        asset=asset,side="BUY",stable=ASSETS[asset],
        budget_usd=2000,source_evidence=proof,
        prices=rates(asset,ASSETS[asset]),now=__import__("time").time().__int__())
    assert Decimal(got)==2000


@pytest.mark.parametrize("asset",["LINK","UNI","ARB"])
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
    def market_decision(market,*,asset,now,horizon):
        assert horizon=="15m"
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


@pytest.mark.parametrize("asset",["ETH"])
def test_native_evm_sell_never_runs_without_native_receipt_worker(monkeypatch,asset):
    monkeypatch.delenv("MININEX_EVM_PASSIVE_SETTLEMENT_ENABLED",raising=False)
    monkeypatch.delenv("MININEX_EVM_AUTO_WORKER_ENABLED",raising=False)
    monkeypatch.delenv("MININEX_LIVE_TRADING_ENABLED",raising=False)
    monkeypatch.delenv("DATABASE_URL",raising=False)
    actions=use_tick_mocks(monkeypatch,asset=asset,action="SELL")
    async def tick():
        async with httpx.AsyncClient() as client:
            return await worker.execute_owner_evm_live_tick(
                session(asset),market=object(),client=client,now=10000)
    result=asyncio.run(tick())
    assert result["state"]=="WAIT"
    assert result["reason"]=="NATIVE_CHAIN_SETTLEMENT_NOT_ACTIVE"
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


def test_worker_is_wired_to_lifespan_and_needs_database(monkeypatch):
    from backend.app import evm_auto_live
    assert isinstance(evm_auto_live,worker.SharedEvmLiveWorker)
    monkeypatch.delenv("DATABASE_URL",raising=False)
    assert worker.evm_auto_worker_enabled() is False
    assert evm_auto_live._task is None


@pytest.mark.parametrize("asset",["LINK","UNI","ARB"])
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


@pytest.mark.parametrize("asset",["ETH"])
def test_native_buy_requires_active_passive_unwrap_reconciler(monkeypatch,asset):
    monkeypatch.delenv("MININEX_EVM_PASSIVE_SETTLEMENT_ENABLED",raising=False)
    monkeypatch.delenv("MININEX_EVM_AUTO_WORKER_ENABLED",raising=False)
    monkeypatch.delenv("MININEX_LIVE_TRADING_ENABLED",raising=False)
    monkeypatch.delenv("DATABASE_URL",raising=False)
    actions=use_tick_mocks(monkeypatch,asset=asset,action="BUY")
    async def tick():
        async with httpx.AsyncClient() as client:
            return await worker.execute_owner_evm_live_tick(
                session(asset),market=object(),client=client,now=10000)
    result=asyncio.run(tick())
    assert result["reason"]=="NATIVE_CHAIN_SETTLEMENT_NOT_ACTIVE"
    assert actions==["verified_market"]


@pytest.mark.parametrize("asset",["ETH"])
@pytest.mark.parametrize("activation",["read-only","explicit-live"])
def test_native_buy_submits_real_swap_only_with_unwrap_worker_enabled(
    monkeypatch,asset,activation
):
    monkeypatch.delenv("MININEX_EVM_PASSIVE_SETTLEMENT_ENABLED",raising=False)
    monkeypatch.delenv("MININEX_EVM_AUTO_WORKER_ENABLED",raising=False)
    monkeypatch.delenv("MININEX_LIVE_TRADING_ENABLED",raising=False)
    monkeypatch.setenv("DATABASE_URL","postgresql://example.invalid/test")
    if activation=="read-only":
        monkeypatch.setenv("MININEX_EVM_PASSIVE_SETTLEMENT_ENABLED","explicit-readonly")
    else:
        monkeypatch.setenv("MININEX_EVM_AUTO_WORKER_ENABLED","explicit-live")
        monkeypatch.setenv("MININEX_LIVE_TRADING_ENABLED","true")
    actions=use_tick_mocks(monkeypatch,asset=asset,action="BUY")
    async def tick():
        async with httpx.AsyncClient() as client:
            return await worker.execute_owner_evm_live_tick(
                session(asset),market=object(),client=client,now=10000)
    result=asyncio.run(tick())
    assert result["state"]=="SUBMITTED"
    assert len([x for x in actions if isinstance(x,tuple)
                and x[0]=="original_single_execution"])==1


@pytest.mark.parametrize("asset",["ETH"])
def test_native_sell_starts_one_owner_deposit_from_actual_native_wallet(
    monkeypatch,asset
):
    monkeypatch.setenv("MININEX_EVM_PASSIVE_SETTLEMENT_ENABLED","explicit-readonly")
    monkeypatch.setenv("DATABASE_URL","postgresql://example.invalid/test")
    actions=use_tick_mocks(monkeypatch,asset=asset,action="SELL")
    async def zero_wrapped(did,**kw):
        actions.append("native_wallet_probe")
        return evidence(asset,"SELL",source="0",allowance="0")
    async def amount(**kw):
        actions.append("native_gas_and_policy")
        assert kw["asset"]==asset
        return 10**18
    async def deposit(**kw):
        actions.append("single_native_deposit")
        assert kw["strategy_run_id"].startswith("owner_20261117")
        assert kw["signal_event_id"]=="market_166"
        assert kw["native_amount_wei"]==10**18
        assert kw["budget_revision"]==7
        return {"state":"SUBMITTED","operation":"DEPOSIT"}
    monkeypatch.setattr(worker,"check_evm_source_allowance",zero_wrapped)
    monkeypatch.setattr(worker,"prepare_original_native_sell_amount",amount)
    monkeypatch.setattr(worker,"submit_internal_native_evm_sell_deposit",deposit)
    async def tick():
        async with httpx.AsyncClient() as client:
            return await worker.execute_owner_evm_live_tick(
                session(asset),market=object(),client=client,now=10000)
    result=asyncio.run(tick())
    assert result["state"]=="SUBMITTED"
    assert actions==["verified_market","native_wallet_probe",
                     "native_gas_and_policy","single_native_deposit"]


@pytest.mark.parametrize("asset",["ETH"])
def test_native_sell_existing_wrapped_balance_requires_original_deposit_proof(
    monkeypatch,asset
):
    monkeypatch.setenv("MININEX_EVM_PASSIVE_SETTLEMENT_ENABLED","explicit-readonly")
    monkeypatch.setenv("DATABASE_URL","postgresql://example.invalid/test")
    actions=use_tick_mocks(monkeypatch,asset=asset,action="SELL")
    async def should_not_wrap(**kwargs):
        pytest.fail("Existing wrapped source balance must not be deposited again")
    monkeypatch.setattr(worker,"prepare_original_native_sell_amount",should_not_wrap)
    async def tick():
        async with httpx.AsyncClient() as client:
            return await worker.execute_owner_evm_live_tick(
                session(asset),market=object(),client=client,now=10000)
    result=asyncio.run(tick())
    assert result["state"]=="WAIT"
    assert result["reason"]=="ORIGINAL_NATIVE_SELL_CYCLE_REQUIRED"
    assert not any(isinstance(action,tuple) and
                   action[0]=="original_single_execution" for action in actions)


def test_session_query_failure_does_not_strand_prior_onchain_settlements(monkeypatch):
    """Passive finalized original swaps/fees are not gated by ACTIVE session scans."""
    calls=[]
    monkeypatch.setattr(worker,"evm_auto_worker_enabled",lambda:True)
    def unavailable(*args,**kwargs):
        raise ValueError("Active session database query temporarily unavailable")
    monkeypatch.setattr(worker,"owner_started_evm_sessions",unavailable)
    count=0
    async def one_cycle(seconds):
        nonlocal count
        count+=1
        if count>1:raise asyncio.CancelledError()
    monkeypatch.setattr(worker.asyncio,"sleep",one_cycle)
    async def approvals(*,client):calls.append("approvals")
    async def swaps():calls.append("swaps")
    async def native_buy(*,client):calls.append("native_buy")
    async def native_sell(*,client):calls.append("native_sell")
    async def historical(*,client):calls.append("historical")
    async def treasury_finality():calls.append("treasury_finality")
    async def fee_batch(*,client):calls.append("fee_batch")
    monkeypatch.setattr(worker,"reconcile_original_router_approvals_once",approvals)
    monkeypatch.setattr(worker,"reconcile_submitted_evm_swaps_once",swaps)
    monkeypatch.setattr(worker,"reconcile_native_buy_cycle_once",native_buy)
    monkeypatch.setattr(worker,"reconcile_native_sell_once",native_sell)
    monkeypatch.setattr(worker,"reconcile_verified_erc20_fees_once",historical)
    monkeypatch.setattr(worker,"reconcile_auto_fee_receipts_once",treasury_finality)
    monkeypatch.setattr(worker,"run_owner_approved_auto_fee_batch",fee_batch)
    async def run():
        await worker.SharedEvmLiveWorker(market=object())._run()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())
    assert calls==["approvals","swaps","native_buy","native_sell","historical",
                   "treasury_finality","fee_batch"]


def test_shared_live_session_scan_rotates_across_all_six_evm_assets(monkeypatch):
    """A stable first page of LIVE wallets must not starve newer coin sessions."""
    all_sessions=[session(asset) for asset in
                  ("BNB","POL","AVAX","LINK","UNI","ARB")]
    queries=[]
    class Cursor:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def execute(self,sql,params):
            self.size,self.offset=params
            queries.append((sql,params))
        def fetchall(self):return all_sessions[self.offset:self.offset+self.size]
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kw):return Cursor()
    monkeypatch.setattr(worker,"evm_auto_worker_enabled",lambda:True)
    monkeypatch.setattr(worker,"_EVM_SESSION_PAGE_OFFSET",0)
    monkeypatch.setattr(worker,"_db_url",lambda:"postgres://test")
    monkeypatch.setattr(worker,"_ensure_schema",lambda _:None)
    monkeypatch.setattr(worker.psycopg,"connect",lambda *args,**kwargs:Connection())
    pages=[worker.owner_started_evm_sessions(limit=2) for _ in range(4)]
    assert pages==[all_sessions[:2],all_sessions[2:4],all_sessions[4:6],
                   all_sessions[:2]]
    assert [args for sql,args in queries]==[
        (2,0),(2,2),(2,4),(2,6),(2,0)]
    assert all("g.status='GRANT_VERIFIED'" in sql for sql,args in queries)
    assert all("p.revision=s.budget_revision" in sql for sql,args in queries)
    assert all("LIMIT %s OFFSET %s" in sql for sql,args in queries)



@pytest.mark.parametrize("asset",["LINK","UNI"])
def test_new_owner_wallet_automatically_sends_scoped_approval_before_swap(
    monkeypatch,asset,
):
    if asset in ("BNB","POL","AVAX"):
        monkeypatch.setenv("MININEX_EVM_PASSIVE_SETTLEMENT_ENABLED","explicit-readonly")
        monkeypatch.setenv("DATABASE_URL","postgresql://example.invalid/test")
    actions=use_tick_mocks(monkeypatch,asset=asset,action="BUY")
    async def zero_allowance(did,**kw):
        actions.append(("onchain_source",kw["amount"]))
        return evidence(asset,"BUY",source="5000",allowance="0")
    async def approve(**kw):
        actions.append(("original_approval",kw["amount_atomic"]))
        assert kw["asset"]==asset
        assert kw["budget_revision"]==7
        return {"state":"SUBMITTED","tx_hash":"0x"+"ef"*32}
    monkeypatch.setattr(worker,"check_evm_source_allowance",zero_allowance)
    monkeypatch.setattr(worker,"submit_scoped_router_approval_once",approve)
    async def tick():
        async with httpx.AsyncClient() as client:
            return await worker.execute_owner_evm_live_tick(
                session(asset),market=object(),client=client,now=10000)
    result=asyncio.run(tick())
    assert result["state"]=="SUBMITTED"
    assert result["reason"]=="ORIGINAL_ROUTER_APPROVAL_BEFORE_SWAP"
    assert result["approval_tx_hash"]=="0x"+"ef"*32
    assert len([a for a in actions if isinstance(a,tuple)
                and a[0]=="original_approval"])==1
    assert not any(isinstance(a,tuple) and
                   a[0]=="original_single_execution" for a in actions)


def test_evm_live_batch_runs_separate_wallets_in_parallel_but_serializes_nonce(
    monkeypatch,
):
    monkeypatch.setattr(worker,"evm_auto_worker_enabled",lambda:True)
    first=session("LINK")
    same_owner_same_chain=session("UNI")
    other_owner=session("LINK")
    other_owner["privy_did"]="did:privy:second"
    calls=[]
    other_started=asyncio.Event()
    async def fake_tick(row,*,market,client):
        name=(row["privy_did"],row["asset"])
        calls.append(name)
        if name==(DID,"LINK"):
            # A fully serial worker cannot proceed until this unblocks.
            await asyncio.wait_for(other_started.wait(),timeout=3)
        elif name==("did:privy:second","LINK"):
            other_started.set()
        return {"state":"WAIT"}
    monkeypatch.setattr(worker,"execute_owner_evm_live_tick",fake_tick)
    async def do():
        async with httpx.AsyncClient() as client:
            return await worker.run_owner_evm_live_batch(
                [first,same_owner_same_chain,other_owner],
                market=object(),client=client)
    outcome=asyncio.run(do())
    assert outcome=={
        "sessions_seen":3,"attempted":3,"failed":0,
        "wallet_chain_groups":2}
    assert calls==[
        (DID,"LINK"),("did:privy:second","LINK"),(DID,"UNI")]


def test_evm_live_batch_isolates_a_failed_coin_without_rebroadcast(
    monkeypatch,
):
    monkeypatch.setattr(worker,"evm_auto_worker_enabled",lambda:True)
    good=session("UNI")
    bad=session("LINK")
    tried=[]
    async def fake_tick(row,*,market,client):
        tried.append(row["asset"])
        if row["asset"]=="LINK":
            raise RuntimeError("Original send requires journal reconciliation")
    monkeypatch.setattr(worker,"execute_owner_evm_live_tick",fake_tick)
    async def do():
        async with httpx.AsyncClient() as client:
            return await worker.run_owner_evm_live_batch(
                [bad,good],market=object(),client=client)
    result=asyncio.run(do())
    assert result["attempted"]==1
    assert result["failed"]==1
    assert tried==["LINK","UNI"]


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","ARB"])
@pytest.mark.parametrize("stable",["USDC","USDT"])
@pytest.mark.parametrize("action",["BUY","SELL"])
def test_cross_evm_session_uses_ethereum_settlement_never_legacy_local_send(
    monkeypatch,asset,stable,action
):
    from backend import ethereum_settlement_auto_plan as bridge
    from backend.ethereum_settlement_plan import settlement_route
    row=session(asset)
    row["stable"]=stable
    actions=use_tick_mocks(monkeypatch,asset=asset,action=action)
    async def forbidden(*args,**kwargs):
        pytest.fail("Legacy local allowance, swap, or wrap must not run")
    monkeypatch.setattr(worker,"check_evm_source_allowance",forbidden)
    monkeypatch.setattr(worker,"execute_internal_evm_strategy_signal",forbidden)
    monkeypatch.setattr(worker,"submit_scoped_router_approval_once",forbidden)
    monkeypatch.setattr(worker,"submit_internal_native_evm_sell_deposit",forbidden)
    async def funding(**kw):
        actions.append("ethereum_funding")
        assert kw["stable"]==stable and kw["asset"]==asset
        assert kw["budget_usd"]==2000
        return {"input_atomic":"12000000","input_amount":"12.000000"}
    async def candidate(**kw):
        actions.append("bridge_candidate")
        assert kw["stable"]==stable and kw["amount"]=="12.000000"
        assert kw["budget_revision"]==7 and kw["side"]==action
        route=settlement_route(asset,action,stable)
        assert route["source_chain_id" if action=="BUY" else "destination_chain_id"]==1
        return {"state":"CROSS_CHAIN_POLICY_MIGRATION_REQUIRED","can_submit":False}
    async def sell(**kw):
        actions.append("strategy_coin_position")
        assert kw["stable"]==stable and kw["asset"]==asset
        return {"input_atomic":"12000000000000000000","input_amount":"12.000000"}
    monkeypatch.setattr(bridge,"plan_cross_evm_buy",funding)
    monkeypatch.setattr(bridge,"plan_cross_evm_sell",sell)
    monkeypatch.setattr(bridge,"prepare_cross_evm_auto_candidate",candidate)
    async def tick():
        async with httpx.AsyncClient() as client:
            return await worker.execute_owner_evm_live_tick(
                row,market=object(),client=client,now=10000)
    result=asyncio.run(tick())
    if action=="BUY":
        assert result["can_submit"] is False
        assert actions==["verified_market","ethereum_funding","bridge_candidate"]
    else:
        assert result["can_submit"] is False
        assert actions==["verified_market","strategy_coin_position","bridge_candidate"]
