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

import httpx
import psycopg
from psycopg.rows import dict_row

from .eth_live_strategy_tick import verified_live_evm_momentum
from .shared_evm_allowance import check_evm_source_allowance
from .shared_evm_policy_reference_rates import (
    _price,fetch_evm_policy_reference_rates,
)
from .shared_evm_strategy_execution import execute_internal_evm_strategy_signal
from .shared_evm_swap_receipts import _pair
from .storage import _db_url,_ensure_schema,_safe_did

LOG=logging.getLogger("mininex.shared_evm_auto_live")
_INTERVAL_SECONDS=30
_ASSETS=frozenset(("BNB","POL","AVAX","LINK","UNI"))
_NATIVE=frozenset(("BNB","POL","AVAX"))


def evm_auto_worker_enabled()->bool:
    return (
        os.getenv("MININEX_EVM_AUTO_WORKER_ENABLED")=="explicit-live"
        and os.getenv("MININEX_LIVE_TRADING_ENABLED")=="true"
        and bool(os.getenv("DATABASE_URL","").strip())
    )


def owner_started_evm_sessions(limit:int=20)->list[dict]:
    """Read only ACTIVE owner-started sessions with unchanged coin settings.

    The trusted send path independently checks fresh remote Privy consent,
    policy, scoped signer/quorum, budget and ACTIVE session under SQL lock.
    """
    if not evm_auto_worker_enabled():
        return []
    if type(limit) is not int or not 1<=limit<=100:
        raise ValueError("Invalid EVM session scan page")
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT s.privy_did,s.asset,s.stable,s.chain_id,
                          s.budget_revision,s.started_by_owner_at,
                          p.budget_usd,p.mode,p.horizon
                   FROM mininex_live_evm_auto_sessions s
                   JOIN mininex_extra_coin_preferences p
                     ON p.privy_did=s.privy_did AND p.asset=s.asset
                   JOIN mininex_live_evm_owner_permissions g
                     ON g.privy_did=s.privy_did AND g.asset=s.asset
                   WHERE s.status='ACTIVE'
                     AND s.asset IN ('BNB','POL','AVAX','LINK','UNI')
                     AND s.started_by_owner_at IS NOT NULL
                     AND p.revision=s.budget_revision
                     AND p.stable=s.stable
                     AND g.status='GRANT_VERIFIED'
                     AND g.revoked_at IS NULL
                     AND g.expires_at>now()
                     AND g.budget_revision=s.budget_revision
                   ORDER BY s.updated_at ASC,s.privy_did,s.asset
                   LIMIT %s""",(limit,))
            return list(cur.fetchall())


def _trusted_live_session(row:dict)->dict:
    if type(row) is not dict or row.get("asset") not in _ASSETS:
        raise ValueError("Untrusted coin LIVE session")
    pair=_pair(row["asset"],row.get("stable"),"BUY")
    started=row.get("started_by_owner_at")
    if (type(started) is not datetime or started.tzinfo is None
            or type(row.get("budget_usd")) is not int
            or row["budget_usd"]<=0
            or type(row.get("budget_revision")) is not int
            or row["budget_revision"]<=0
            or row.get("chain_id")!=pair["chain_id"]
            or row.get("mode") not in ("LONG","SPOT_SHORT","AUTO")
            or row.get("horizon") not in ("15m","1h","24h")):
        raise ValueError("Coin LIVE session settings are not verified")
    return row


def exact_user_coin_live_amount(
    *,asset:str,side:str,stable:str,budget_usd:int,
    source_evidence:dict,prices:dict,now:int,
)->str|None:
    """Exact user-budget BUY or owner-held SELL from real onchain evidence.

    No system max order, daily cap, 50% utilization rule or synthetic cash.
    SELL may close appreciated positions above their original USD budget.
    Native gas remains independent of the EVM ERC20 source amount.
    """
    pair=_pair(asset,stable,side)
    if (type(budget_usd) is not int or budget_usd<=0
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
                or source<=0 or allowance<=0):
            return None
        if side=="BUY":
            stable_usd=_price(prices,stable,now)
            amount=min(source,allowance,Decimal(budget_usd)/stable_usd)
        else:
            amount=min(source,allowance)
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
    decision=verified_live_evm_momentum(market,asset=asset,now=current)
    if decision["action"]=="WAIT":
        return {"asset":asset,"state":"WAIT","reason":decision["reason"]}
    side=decision["action"]
    if asset in _NATIVE:
        # A WBNB/WPOL/WAVAX swap is not itself a native-asset trade.
        # Until the unique native-leg sender is joined to the journal,
        # automated submission is intentionally unavailable on these chains.
        return {"asset":asset,"state":"WAIT",
                "reason":"NATIVE_CONVERSION_EXECUTION_NOT_CONNECTED"}
    stable=row["stable"]
    _pair(asset,stable,side)
    probe="0.000001" if side=="BUY" else "0.000000000000000001"
    evidence=await check_evm_source_allowance(
        did,asset=asset,side=side,stable=stable,amount=probe)
    prices=await fetch_evm_policy_reference_rates(asset,stable)
    amount=exact_user_coin_live_amount(
        asset=asset,side=side,stable=stable,
        budget_usd=row["budget_usd"],source_evidence=evidence,
        prices=prices,now=int(current))
    if amount is None:
        return {"asset":asset,"state":"WAIT",
                "reason":"NO_APPROVED_VERIFIED_SOURCE_BALANCE"}
    # Revalidate the chosen FULL amount independently before quote or send.
    # The exact final send also checks its own onchain quote, grant,
    # consent and reservation in a permanent SENDING claim.
    full=await check_evm_source_allowance(
        did,asset=asset,side=side,stable=stable,amount=amount)
    if (full.get("source_funds_sufficient") is not True
            or full.get("router_allowance_sufficient") is not True
            or full.get("native_gas_available") is not True):
        return {"asset":asset,"state":"WAIT",
                "reason":"SOURCE_BALANCE_OR_OWNER_APPROVAL_CHANGED"}
    run="owner_"+row["started_by_owner_at"].astimezone(
        timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    event="market_"+str(int(current//60))
    return await execute_internal_evm_strategy_signal(
        did=did,asset=asset,strategy_run_id=run,
        signal_event_id=event,side=side,stable=stable,amount=amount,
        budget_revision=row["budget_revision"],client=client)


class SharedEvmLiveWorker:
    """Opt-in owner-controlled worker, disabled unless explicitly configured."""
    def __init__(self,market):
        self.market=market
        self._task=None

    async def start(self)->None:
        if evm_auto_worker_enabled() and self._task is None:
            self._task=asyncio.create_task(
                self._run(),name="mininex-shared-evm-auto-live")

    async def stop(self)->None:
        task,self._task=self._task,None
        if task is not None:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def _run(self)->None:
        while True:
            await asyncio.sleep(_INTERVAL_SECONDS)
            if not evm_auto_worker_enabled():
                continue
            try:
                sessions=await asyncio.to_thread(owner_started_evm_sessions)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("LIVE EVM owner-session scan unavailable")
                continue
            async with httpx.AsyncClient(
                    timeout=httpx.Timeout(12),follow_redirects=False) as client:
                for row in sessions:
                    if not evm_auto_worker_enabled():
                        break
                    try:
                        await execute_owner_evm_live_tick(
                            row,market=self.market,client=client)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # A committed SENDING claim must never be retried.
                        LOG.exception("EVM LIVE tick failed; check original journal")
