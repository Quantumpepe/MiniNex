"""Owner-started LIVE EVM strategist for the shared five-asset execution path.

No PAPER orders, simulated balances, client-supplied signals or arbitrary
order/day limits. The same verified market momentum function serves all
six EVM assets (including existing ETH). Owner settings and actual source
token balances/Router02 allowances determine trade quantities.

Native BNB/POL/AVAX require the pending one-shot native conversion sender
before automated swaps can be activated. This worker never leaves a
wrapped-native position under a misleading "completed native" claim.
"""
from __future__ import annotations

import asyncio
from datetime import datetime,timezone
from decimal import Decimal,ROUND_DOWN
import logging
import os
import time
from threading import Lock

import httpx
import psycopg
from psycopg.rows import dict_row

from .eth_live_strategy_tick import verified_live_evm_momentum,verified_live_native_momentum
from .shared_evm_allowance import check_evm_source_allowance
from .shared_evm_router_approval_send import submit_scoped_router_approval_once
from .shared_evm_router_approval_finality import reconcile_original_router_approvals_once
from .live_eth_quotes import _quantity
from .shared_evm_policy_reference_rates import (
    _price,fetch_evm_policy_reference_rates,
)
from .shared_evm_strategy_execution import execute_internal_evm_strategy_signal
from .ethereum_settlement_reconcile_worker import reconcile_bridge_receipts_once
from .ethereum_settlement_plan import settlement_route
from .ethereum_settlement_funding import read_pending_ethereum_funding
from .shared_evm_swap_receipts import _pair
from .shared_evm_native_buy_reconcile import reconcile_native_buy_cycle_once
from .shared_evm_native_sell_reconcile import reconcile_native_sell_once
from .shared_evm_token_hwm_worker import reconcile_verified_erc20_fees_once
from .shared_evm_passive_finality_worker import reconcile_submitted_evm_swaps_once
from .shared_evm_passive_settlement import _settlement_enabled
from .shared_evm_auto_fee_finality import reconcile_auto_fee_receipts_once
from .shared_evm_auto_fee_worker import run_owner_approved_auto_fee_batch
from .shared_evm_native_sell_deposit import (
    prepare_original_native_sell_amount,submit_internal_native_evm_sell_deposit,
)
from .storage import _db_url,_ensure_schema,_safe_did

LOG=logging.getLogger("mininex.shared_evm_auto_live")
_INTERVAL_SECONDS=30
_ASSETS=frozenset(("ETH","BNB","POL","AVAX","LINK","QNT","UNI","ARB","BTC","XRP","SOL","TRX"))
_NATIVE=frozenset(("ETH","BNB","POL","AVAX"))
_CROSS_EVM=frozenset(("BNB","POL","AVAX","ARB"))

# Rotate the worker query across all owner-activated sessions. This is a
# database work page only; it never restricts trading volumes or active users.
_EVM_SESSION_PAGE_LOCK=Lock()
_EVM_SESSION_PAGE_OFFSET=0


def evm_auto_worker_enabled()->bool:
    """No operator feature switch beyond each owner's actual trade grant.

    Every user must explicitly start their own session with current remote
    Privy authorization. The database is the only service dependency.
    """
    return bool(os.getenv("DATABASE_URL","").strip())


def _persist_evm_worker_heartbeat()->None:
    """Record an actual current worker poll, never infer it from an env flag."""
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """INSERT INTO mininex_live_worker_heartbeats
                       (worker_name,last_poll_at,status)
                   VALUES ('shared_evm_auto',now(),'RUNNING')
                   ON CONFLICT(worker_name) DO UPDATE
                   SET last_poll_at=now(),status='RUNNING'""")


def owner_started_evm_sessions(limit:int=20)->list[dict]:
    """Read only ACTIVE owner-started sessions with unchanged coin settings.

    The trusted send path independently checks fresh remote Privy consent,
    policy, scoped signer/quorum, budget and ACTIVE session under SQL lock.
    """
    if not evm_auto_worker_enabled():
        return []
    if type(limit) is not int or not 1<=limit<=100:
        raise ValueError("Invalid EVM session scan page")
    global _EVM_SESSION_PAGE_OFFSET
    db=_db_url()
    _ensure_schema(db)
    sql="""SELECT s.privy_did,s.asset,s.stable,s.chain_id,
                          s.budget_revision,s.started_by_owner_at,s.status AS session_status,
                          COALESCE(p.budget_usd::numeric,l.planned_capital_usd,b.planned_capital_usd)
                             AS budget_usd,
                           COALESCE(p.mode,t.mode) AS mode,
                           COALESCE(p.horizon,t.horizon) AS horizon,
                          (c.status='GRANT_VERIFIED'
                           AND c.revoked_at IS NULL
                           AND c.legacy_reconciliation_required=FALSE
                           AND c.scopes_json ? s.asset
                           AND c.revisions_json->>s.asset=s.budget_revision::text)
                             AS central,
                          c.scopes_json->s.asset->>'settlement_policy' AS settlement_policy,
                          COALESCE((
                              SELECT SUM(r.amount_usd)
                              FROM mininex_live_evm_budget_reservations r
                              WHERE r.privy_did=s.privy_did
                                AND r.asset=s.asset AND r.side='BUY'
                                AND r.state IN
                                  ('RESERVED','SENDING','SUBMITTED',
                                   'RECONCILIATION_REQUIRED')
                          ),0) AS pending_buy_usd
                   FROM mininex_live_evm_auto_sessions s
                   LEFT JOIN mininex_extra_coin_preferences p
                     ON p.privy_did=s.privy_did AND p.asset=s.asset
                    AND s.asset NOT IN ('ETH','BTC')
                   LEFT JOIN mininex_live_btc_limits b
                     ON b.privy_did=s.privy_did AND s.asset='BTC'
                   LEFT JOIN mininex_live_eth_limits l
                     ON l.privy_did=s.privy_did AND s.asset='ETH'
                   LEFT JOIN mininex_trade_preferences t
                     ON t.privy_did=s.privy_did AND t.asset=s.asset
                    AND s.asset IN ('ETH','BTC')
                   LEFT JOIN mininex_live_evm_owner_permissions g
                     ON g.privy_did=s.privy_did AND g.asset=s.asset
                   LEFT JOIN mininex_live_evm_wallet_policy_grants c
                     ON c.privy_did=s.privy_did
                   WHERE (s.status='ACTIVE' OR (s.status='EXIT_PENDING' AND s.asset IN ('ETH','BNB','POL','AVAX','ARB','LINK','QNT','UNI','BTC','XRP','SOL','TRX')))
                     AND s.asset IN ('ETH','BNB','POL','AVAX','LINK','QNT','UNI','ARB','BTC','XRP','SOL','TRX')
                     AND s.started_by_owner_at IS NOT NULL
                     AND (
                          (s.asset='ETH'
                           AND l.revision=s.budget_revision
                           AND t.stable=s.stable
                           AND l.trading_paused=TRUE)
                          OR
                          (s.asset='BTC' AND b.revision=s.budget_revision AND t.stable=s.stable)
                          OR
                          (s.asset NOT IN ('ETH','BTC')
                           AND p.revision=s.budget_revision
                           AND p.stable=s.stable)
                     )
                     AND (
                       (s.asset NOT IN ('BTC','XRP','SOL','TRX') AND g.status='GRANT_VERIFIED' AND g.revoked_at IS NULL
                        AND g.expires_at>now()
                        AND g.budget_revision=s.budget_revision)
                       OR
                       (c.status='GRANT_VERIFIED'
                        AND c.revoked_at IS NULL
                        AND c.legacy_reconciliation_required=FALSE
                        AND c.wallet_address=s.wallet_address
                        AND c.scopes_json ? s.asset
                        AND c.scopes_json->s.asset->>'stable'=s.stable
                        AND c.revisions_json->>s.asset=s.budget_revision::text
                        AND (c.scopes_json->s.asset->>'expires_at')::bigint>
                            extract(epoch from now())::bigint)
                     )
                   ORDER BY s.updated_at ASC,s.privy_did,s.asset
                   LIMIT %s OFFSET %s"""
    with _EVM_SESSION_PAGE_LOCK:
        offset=_EVM_SESSION_PAGE_OFFSET
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql,(limit,offset))
                page=list(cur.fetchall())
                if not page and offset:
                    offset=0
                    cur.execute(sql,(limit,0))
                    page=list(cur.fetchall())
        _EVM_SESSION_PAGE_OFFSET=offset+len(page)
    return page


def _trusted_live_session(row:dict)->dict:
    if type(row) is not dict or row.get("asset") not in _ASSETS:
        raise ValueError("Untrusted coin LIVE session")
    if row["asset"] in _CROSS_EVM:
        route=settlement_route(row["asset"],"BUY",row.get("stable"))
        coin_chain_id=route["destination_chain_id"]
    else:
        coin_chain_id=_pair(row["asset"],row.get("stable"),"BUY")["chain_id"]
    started=row.get("started_by_owner_at")
    try:
        pending=Decimal(str(row.get("pending_buy_usd")))
        if not pending.is_finite() or pending<0:
            raise ValueError("Invalid pending coin allocation")
    except (ArithmeticError,ValueError,TypeError):
        raise ValueError("Unverified outstanding coin buy capital") from None
    if (type(started) is not datetime or started.tzinfo is None
            or type(row.get("budget_usd")) not in (int,Decimal)
            or row["budget_usd"]<=0
            or type(row.get("budget_revision")) is not int
            or row["budget_revision"]<=0
            or row.get("chain_id")!=coin_chain_id
            or row.get("mode") not in ("LONG","SPOT_SHORT","AUTO")
            or row.get("horizon") not in ("5m","15m","1h","24h")):
        raise ValueError("Coin LIVE session settings are not verified")
    return row


def exact_user_coin_live_amount(
    *,asset:str,side:str,stable:str,budget_usd:int,
    source_evidence:dict,prices:dict,now:int,
    pending_buy_usd:object=Decimal(0),
    ignore_allowance:bool=False,
)->str|None:
    """Exact user-budget BUY or owner-held SELL from real onchain evidence.

    No system max order, daily cap, 50% utilization rule or synthetic cash.
    SELL may close appreciated positions above their original USD budget.
    Native gas remains independent of the EVM ERC20 source amount.
    """
    pair=_pair(asset,stable,side)
    if (type(budget_usd) not in (int,Decimal) or budget_usd<=0
            or type(source_evidence) is not dict
            or source_evidence.get("mode")!="SHARED_EVM_SOURCE_ALLOWANCE_READ_ONLY"
            or source_evidence.get("asset")!=asset
            or source_evidence.get("side")!=side
            or source_evidence.get("stable")!=stable
            or source_evidence.get("network")!=pair["network"]
            or source_evidence.get("chain_id")!=pair["chain_id"]
            or source_evidence.get("source_token_address")!=pair["source_token"]
            or source_evidence.get("wallet_identity_verified") is not True
            or source_evidence.get("native_gas_available") is not True):
        return None
    try:
        source=Decimal(source_evidence["source_balance"])
        allowance=Decimal(source_evidence["router_allowance"])
        if (not source.is_finite() or not allowance.is_finite()
                or source<=0 or allowance<0):
            return None
        if side=="BUY":
            if (source_evidence.get("destination_token_address")
                    !=pair["output_token"]
                    or source_evidence.get("destination_balance") is None):
                return None
            current_holdings=Decimal(source_evidence["destination_balance"])
            if not current_holdings.is_finite() or current_holdings<0:
                return None
            if asset in _NATIVE:
                atomic=source_evidence.get("native_gas_balance_atomic")
                if (type(atomic) is not str or not atomic.isascii()
                        or not atomic.isdecimal()):
                    return None
                current_holdings+=Decimal(atomic)/Decimal(10**18)
            existing_exposure=current_holdings*_price(prices,asset,now)
            pending=Decimal(str(pending_buy_usd))
            if not pending.is_finite() or pending<0:
                return None
            available_budget=max(Decimal(0),
                                 Decimal(budget_usd)-existing_exposure-pending)
            stable_usd=_price(prices,stable,now)
            amount=min(source,available_budget/stable_usd)
            if not ignore_allowance:
                amount=min(amount,allowance)
        else:
            amount=source if ignore_allowance else min(source,allowance)
        quantum=Decimal(1).scaleb(-pair["source_decimals"])
        amount=amount.quantize(quantum,rounding=ROUND_DOWN)
        return format(amount,"f") if amount>0 else None
    except (ArithmeticError,ValueError,KeyError,TypeError):
        return None


async def execute_owner_evm_live_tick(
    row:dict,*,market,client:httpx.AsyncClient,
    now:float|None=None,
)->dict:
    """One per-coin market decision, original event and one budget/Privy send."""
    row=_trusted_live_session(row)
    asset=row["asset"]
    did=_safe_did(row["privy_did"])
    if type(client) is not httpx.AsyncClient:
        raise ValueError("Internal HTTP client required")
    current=time.time() if now is None else now
    exiting=row.get("session_status")=="EXIT_PENDING"
    if asset=="AVAX":
        from .ethereum_stargate_staged_worker import advance_avax_order
        existing=await advance_avax_order(did,row=row,client=client)
        if existing is not None:
            return existing
    if asset in ("BNB","POL","ARB") and row.get("settlement_policy")=="ETHEREUM_ACROSS_STAGED_V1":
        from .ethereum_across_staged_worker import advance_across_order
        existing=await advance_across_order(did,row=row,client=client)
        if existing is not None:
            return existing
    if exiting:
        if asset not in ("ETH","BNB","POL","AVAX","ARB","LINK","QNT","UNI","BTC","XRP","SOL","TRX"):
            raise ValueError("No automatic exit route for this coin")
        decision={"action":"SELL","reason":"OWNER_REQUESTED_EXIT"}
    else:
        signal=(verified_live_native_momentum if asset in ("BTC","XRP","SOL","TRX") else verified_live_evm_momentum)
        decision=signal(market,asset=asset,now=current,horizon=row["horizon"])
    if decision["action"]=="WAIT":
        return {"asset":asset,"state":"WAIT","reason":decision["reason"]}
    side=decision["action"]
    # Ethereum-only settlement is mandatory for remote EVM assets. Do not
    # fall through to legacy local stablecoin swaps or router approvals.
    # The direct bridge executor uses the existing remote-verified wallet scope.
    if asset in _CROSS_EVM:
        from .ethereum_settlement_auto_plan import plan_cross_evm_buy, plan_cross_evm_sell, prepare_cross_evm_auto_candidate
        if side=="BUY":
            funding=await plan_cross_evm_buy(
                did=did,asset=asset,stable=row["stable"],
                budget_usd=row["budget_usd"],estimated_cost_usd=0,
                client=client,now=int(current))
            if int(funding["input_atomic"])==0:
                return {"asset":asset,"state":"WAIT",
                        "reason":funding.get("reason","NO_VERIFIED_ETHEREUM_FUNDS_OR_BUDGET")}
        else:
            funding=await plan_cross_evm_sell(did=did,asset=asset,stable=row["stable"],client=client)
            if int(funding["input_atomic"])==0:
                if exiting and funding["reason"]=="ETHEREUM_SETTLEMENT_SELL_POSITION_REQUIRED":
                    from .shared_evm_auto_sessions import complete_bridge_exit_if_flat
                    if await asyncio.to_thread(complete_bridge_exit_if_flat,did,asset,row["started_by_owner_at"]):
                        return {"asset":asset,"state":"STOPPED","reason":"VERIFIED_POSITION_FLAT"}
                return {"asset":asset,"state":"WAIT","reason":funding["reason"]}
        return await prepare_cross_evm_auto_candidate(
            did=did,asset=asset,side=side,stable=row["stable"],
            budget_revision=row["budget_revision"],
            amount=funding["input_amount"],client=client,now=int(current),
            budget_usd=row["budget_usd"],session_started_at=row["started_by_owner_at"])
    if asset in _NATIVE and not _settlement_enabled():
        # Native BUY needs an onchain WITHDRAW after swap, while SELL needs
        # a prior DEPOSIT. Both require the original receipt reconciler.
        return {"asset":asset,"state":"WAIT",
                "reason":"NATIVE_CHAIN_SETTLEMENT_NOT_ACTIVE"}
    stable=row["stable"]
    _pair(asset,stable,side)
    pair=_pair(asset,stable,side)
    probe=format(Decimal(1).scaleb(-pair["source_decimals"]),"f")
    evidence=await check_evm_source_allowance(
        did,asset=asset,side=side,stable=stable,amount=probe)
    run="owner_"+row["started_by_owner_at"].astimezone(
        timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    event="market_"+str(int(current//60))
    if asset in _NATIVE and side=="SELL":
        exit_wei=None
        if exiting or (asset=="ETH" and row.get("central") is True):
            from .shared_evm_exit_inventory import read_token_exit_position
            from .shared_evm_auto_sessions import complete_bridge_exit_if_flat
            if row.get("central") is not True or evidence.get("wallet_identity_verified") is not True:
                return {"asset":asset,"state":"WAIT","reason":"EXIT_WALLET_UNVERIFIED"}
            held=await asyncio.to_thread(read_token_exit_position,did,asset,evidence["wallet_address"])
            if held==0:
                if exiting and await asyncio.to_thread(complete_bridge_exit_if_flat,did,asset,row["started_by_owner_at"]):
                    return {"asset":asset,"state":"STOPPED","reason":"VERIFIED_POSITION_FLAT"}
                return {"asset":asset,"state":"WAIT","reason":(
                    "EXIT_SETTLEMENT_PENDING" if exiting else "NO_VERIFIED_STRATEGY_COIN_BALANCE")}
            from decimal import localcontext
            with localcontext() as ctx:
                ctx.prec=100
                exit_wei=int(held.scaleb(18))
        # Prefer an already-held wrapped coin, which needs no additional
        # conversion. If the actual wrapped inventory is zero, a real
        # onchain native DEPOSIT must finalize before its original swap.
        try:
            wrapped=Decimal(str(evidence["source_balance"]))
            if not wrapped.is_finite() or wrapped<0:
                raise ValueError("Unverifiable wrapped-native balance")
            if wrapped>0:
                # Preexisting wrapped inventory may be an unrelated wallet
                # deposit or an earlier incomplete native cycle. Only the
                # persisted original DEPOSIT event may sell its exact
                # native amount; the finality worker handles that path.
                return {"asset":asset,"state":"WAIT",
                        "reason":"ORIGINAL_NATIVE_SELL_CYCLE_REQUIRED"}
            amount_wei=await prepare_original_native_sell_amount(
                did=did,asset=asset,stable=stable,
                source_evidence=evidence,client=client,
                **({"central":True} if row.get("central") is True else {}))
            if exit_wei is not None:
                amount_wei=min(amount_wei,exit_wei)
            if amount_wei<=0:
                return {"asset":asset,"state":"WAIT","reason":"NO_VERIFIED_STRATEGY_COIN_BALANCE"}
            return await submit_internal_native_evm_sell_deposit(
                did=did,asset=asset,strategy_run_id=run,
                signal_event_id=event,
                budget_revision=row["budget_revision"],
                native_amount_wei=amount_wei,client=client,
                **({"central":True} if row.get("central") is True else {}))
        except (ValueError,KeyError,ArithmeticError):
            return {"asset":asset,"state":"WAIT",
                    "reason":"NATIVE_WALLET_BALANCE_OR_GAS_UNVERIFIED"}
    if side=="SELL" and asset in ("LINK","QNT","UNI","BTC","XRP","SOL","TRX"):
        from .shared_evm_exit_inventory import read_token_exit_position
        from .shared_evm_auto_sessions import complete_bridge_exit_if_flat
        if evidence.get("wallet_identity_verified") is not True:
            return {"asset":asset,"state":"WAIT","reason":"EXIT_WALLET_UNVERIFIED"}
        held=await asyncio.to_thread(read_token_exit_position,did,asset,evidence["wallet_address"])
        if held==0:
            if exiting and await asyncio.to_thread(complete_bridge_exit_if_flat,did,asset,row["started_by_owner_at"]):
                return {"asset":asset,"state":"STOPPED","reason":"VERIFIED_POSITION_FLAT"}
            return {"asset":asset,"state":"WAIT","reason":(
                "EXIT_SETTLEMENT_PENDING" if exiting else "NO_VERIFIED_STRATEGY_COIN_BALANCE")}
        evidence={**evidence,"source_balance":format(min(held,Decimal(evidence["source_balance"])),"f")}
    # The BUY allowance inspection already includes the current target
    # token inventory from the same onchain RPC batch. No extra wallet
    # roundtrip is needed to enforce owner-selected coin allocation.
    prices=await fetch_evm_policy_reference_rates(asset,stable)
    pending_buy_usd=row["pending_buy_usd"]
    if side=="BUY":
        # Size the next order against spendable cash rather than the full
        # wallet balance. A protected profit is not a new coin allocation.
        shared=await asyncio.to_thread(read_pending_ethereum_funding,did,
            asset=asset,stable=stable,stable_usd=_price(prices,stable,int(current)))
        withheld=Decimal(shared["pending_wallet_stable_atomic"]).scaleb(-6)
        if not withheld.is_finite() or withheld<0:
            raise ValueError("Original protected funding unavailable")
        available=max(Decimal(0),Decimal(evidence["source_balance"])-withheld)
        evidence={**evidence,"source_balance":format(available,"f")}
        pending_buy_usd=max(Decimal(pending_buy_usd),Decimal(shared["pending_coin_usd"]))
    amount=exact_user_coin_live_amount(
        asset=asset,side=side,stable=stable,
        budget_usd=row["budget_usd"],source_evidence=evidence,
        prices=prices,now=int(current),
        pending_buy_usd=pending_buy_usd,
        ignore_allowance=True)
    if amount is None:
        return {"asset":asset,"state":"WAIT",
                "reason":"NO_VERIFIED_SOURCE_BALANCE_OR_BUDGET"}
    # The user's existing ONE scoped Privy grant includes bounded Router02
    # approve calldata. A new wallet normally has zero token allowance.
    # Submit and finalize that ONE on-chain approval first, without any
    # additional user-facing signature. Never reserve/spend a swap before
    # its real allowance exists onchain.
    pair=_pair(asset,stable,side)
    if asset in ("BTC","XRP","SOL","TRX"):
        from .shared_evm_central_runtime import original_evm_coin_record
        if row.get("central") is not True:
            return {"asset":asset,"state":"WAIT","reason":"CENTRAL_WRAPPED_GRANT_REQUIRED"}
        grant=await asyncio.to_thread(original_evm_coin_record,did,asset)
        cap=Decimal(grant["scope"]["buy_source_atomic_cap" if side=="BUY" else "sell_source_atomic_cap"]).scaleb(-pair["source_decimals"])
        amount=format(min(Decimal(amount),cap),"f")
    _,required_atomic=_quantity(amount,pair["source_decimals"])
    try:
        allowance_atomic=int(evidence["router_allowance_atomic"])
    except (KeyError,ValueError,TypeError):
        return {"asset":asset,"state":"WAIT",
                "reason":"TOKEN_ROUTER_ALLOWANCE_UNVERIFIED"}
    if allowance_atomic<int(required_atomic):
        submitted=await submit_scoped_router_approval_once(
            did=did,asset=asset,side=side,stable=stable,
            amount_atomic=int(required_atomic),
            budget_revision=row["budget_revision"],
            signal_event_id=event,client=client,
            **({"central":True} if row.get("central") is True else {}))
        return {"asset":asset,"state":submitted["state"],
                "reason":"ORIGINAL_ROUTER_APPROVAL_BEFORE_SWAP",
                "approval_tx_hash":submitted.get("tx_hash")}
    # The coordinator fetches a fresh Router02 quote AND checks exact full
    # source/allowance/gas atomics once more before any budget claim/send.
    # Do not repeat the identical RPC here just to delay order execution.
    return await execute_internal_evm_strategy_signal(
        did=did,asset=asset,strategy_run_id=run,
        signal_event_id=event,side=side,stable=stable,amount=amount,
        budget_revision=row["budget_revision"],client=client,
        **({"central":True} if row.get("central") is True else {}))


async def run_owner_evm_live_batch(
    rows:list[dict],*,market,client:httpx.AsyncClient,
)->dict:
    """Parallelize different wallets/chains; serialize a wallet's same chain.

    An Ethereum wallet has one shared nonce for LINK and UNI. Processing
    their granted sessions concurrently could race two durable claims, even
    though each trade is owner-authorized. Other wallets (and independent
    chains) have no such nonce dependency and should not wait on a slow RPC.
    """
    groups:dict[tuple[str,int],list[dict]]={}
    invalid=0
    for row in rows:
        if (type(row) is not dict
                or type(row.get("privy_did")) is not str
                or type(row.get("chain_id")) is not int):
            invalid+=1
            continue
        groups.setdefault(
            (row["privy_did"],row["chain_id"]),[]
        ).append(row)

    async def one_wallet_chain(sessions:list[dict])->tuple[int,int]:
        attempted=0
        failures=0
        for session in sessions:
            if not evm_auto_worker_enabled():
                break
            try:
                await execute_owner_evm_live_tick(
                    session,market=market,client=client)
                attempted+=1
            except asyncio.CancelledError:
                raise
            except Exception:
                # Fail only this owner/coin, never hold other wallet chains.
                failures+=1
                LOG.exception(
                    "EVM LIVE tick failed; check original submission journal")
        return attempted,failures

    completed=await asyncio.gather(*(
        one_wallet_chain(group) for group in groups.values()
    ))
    return {
        "sessions_seen":len(rows),
        "attempted":sum(n for n,_ in completed),
        "failed":invalid+sum(n for _,n in completed),
        "wallet_chain_groups":len(groups),
    }


class SharedEvmLiveWorker:
    """Opt-in owner-controlled worker, disabled unless explicitly configured."""
    def __init__(self,market):
        self.market=market
        self._task=None
        self._bridge_task=None
        self._net_accounting_task=None

    async def start(self)->None:
        if evm_auto_worker_enabled() and self._task is None:
            self._task=asyncio.create_task(
                self._run(),name="mininex-shared-evm-auto-live")
        if evm_auto_worker_enabled() and self._bridge_task is None:
            self._bridge_task=asyncio.create_task(
                self._run_bridge_receipts(),name="mininex-ethereum-bridge-receipts")
        if evm_auto_worker_enabled() and self._net_accounting_task is None:
            self._net_accounting_task=asyncio.create_task(
                self._run_net_accounting(),name="mininex-staged-net-accounting")

    async def stop(self)->None:
        tasks=[t for t in (self._task,self._bridge_task,self._net_accounting_task) if t is not None]
        self._task=None
        self._bridge_task=None
        self._net_accounting_task=None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks,return_exceptions=True)

    async def _run_net_accounting(self):
        from .staged_net_valuation import reconcile_staged_net_valuations_once
        while True:
            await asyncio.sleep(60)
            if not evm_auto_worker_enabled():
                continue
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(12),follow_redirects=False) as client:
                    await reconcile_staged_net_valuations_once(client)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Original staged historical valuation unavailable")

    async def _run_bridge_receipts(self):
        # Separate passive polling task: a slow bridge cannot delay new
        # independent coin signals or the existing live worker.
        while True:
            await asyncio.sleep(_INTERVAL_SECONDS)
            if not evm_auto_worker_enabled():
                continue
            try:
                await reconcile_bridge_receipts_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Original Ethereum bridge finality unavailable")
            try:
                from .ethereum_stargate_staged_worker import reconcile_avax_staged_steps_once
                async with httpx.AsyncClient(timeout=httpx.Timeout(12),follow_redirects=False) as client:
                    await reconcile_avax_staged_steps_once(client)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Original AVAX staged finality unavailable")
            try:
                from .ethereum_across_staged_worker import reconcile_across_staged_steps_once
                async with httpx.AsyncClient(timeout=httpx.Timeout(12),follow_redirects=False) as client:
                    await reconcile_across_staged_steps_once(client)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Original Across staged finality unavailable")

    async def _run(self)->None:
        while True:
            await asyncio.sleep(_INTERVAL_SECONDS)
            if not evm_auto_worker_enabled():
                continue
            try:
                await asyncio.to_thread(_persist_evm_worker_heartbeat)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("LIVE EVM worker heartbeat persistence failed")
            try:
                sessions=await asyncio.to_thread(owner_started_evm_sessions)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed ACTIVE-session query must not strand transactions
                # already sent by Privy. Receipt finality and HWM readbacks
                # operate independently of the current trading session.
                LOG.exception("LIVE EVM owner-session scan unavailable")
                sessions=[]
            async with httpx.AsyncClient(
                    timeout=httpx.Timeout(12),follow_redirects=False) as client:
                try:
                    # Restore spending rights ONLY after original approval
                    # transaction was finalized, never after an RPC timeout.
                    await reconcile_original_router_approvals_once(client=client)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOG.exception("Original router allowance finality unavailable")
                try:
                    # Finalize the exact original Router02 submissions first.
                    # Never rebroadcast and never require an ACTIVE session.
                    await reconcile_submitted_evm_swaps_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOG.exception("Original EVM submitted swap finality unavailable")
                try:
                    # Complete prior finalized native BUYs without using
                    # a second trade signal or retrying an uncertain send.
                    await reconcile_native_buy_cycle_once(client=client)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOG.exception("Original native EVM BUY completion unavailable")
                try:
                    # DEPOSIT receipts, original SELL swap and the final
                    # complete native cycle are reconciled separately.
                    # A paused session cannot start a new downstream send.
                    await reconcile_native_sell_once(client=client)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOG.exception("Original native EVM SELL reconciliation unavailable")
                # Passive cost/HWM reconciliation is independent of an
                # individual coin's current ACTIVE vs PAUSED trading state.
                # Never send a fee wallet transfer or block new trade signals.
                try:
                    await reconcile_verified_erc20_fees_once(client=client,capture_only=True)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOG.exception("Verified EVM token accounting needs reconciliation")
                try:
                    # Confirm and credit previously sent exact Treasury fees,
                    # even if the original user later revokes auto permission.
                    await reconcile_auto_fee_receipts_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOG.exception("Original verified fee payment finality unavailable")
                try:
                    # This is the only monetary fee path: explicitly configured,
                    # separately user-granted, one HWM obligation per send.
                    await run_owner_approved_auto_fee_batch(client=client)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOG.exception("Owner-authorized EVM automatic fee batch unavailable")
                # Run independent wallets/chains without an unnecessary
                # global serial wait, but never race a nonce on one chain.
                await run_owner_evm_live_batch(
                    sessions,market=self.market,client=client)
