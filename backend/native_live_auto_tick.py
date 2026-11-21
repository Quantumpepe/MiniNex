"""Five-coin native AUTO LIVE strategy coordinator, internal and grant-gated.

The owner's one per-coin Start Trade authorization is not simulated here:
a *server-owned, independent* Privy grant auditor must supply real wallet,
policy, signer and budget evidence. This module never accepts caller JSON
as a grant, exposes no API, registers no signer, and never runs by default.
No market preview or strategy signal can itself authorize a transaction.
"""
from __future__ import annotations

import asyncio
from datetime import datetime,timezone
import logging
import re
import time

import httpx

from .eth_live_strategy_tick import verified_live_native_momentum
from .native_buy_strategy_execution import coordinate_original_native_buy_once
from .native_live_source_evidence import verified_owner_native_buy_amount
from .storage import _safe_did

LOG=logging.getLogger("mininex.native_live_tick")
_NATIVE=frozenset(("BTC","SOL","XRP","TRX","DOGE"))
_STABLES={"BTC":("USDC","USDT"),"SOL":("USDC","USDT"),
          "XRP":("USDC",),"TRX":("USDT",),"DOGE":("USDC","USDT")}
_WINDOWS=frozenset(("5m","15m","1h","24h"))
_SHA=re.compile(r"^[0-9a-f]{64}$")
_ID=re.compile(r"^[A-Za-z0-9_-]{6,160}$")


class OriginalNativeMarket:
    """The same real public market feed used by BTC and ten other coins."""
    def __init__(self,btc,extra):
        self.btc=btc
        self.extra=extra

    def snapshot(self,*,now):
        a=self.btc.snapshot(now=now)
        b=self.extra.snapshot(now=now)
        if (type(a) is not dict or type(a.get("markets")) is not dict
                or type(b) is not dict):
            raise ValueError("Real native market feeds unavailable")
        return {"markets":{**a["markets"],**b}}

    def history(self,asset,*,limit):
        if asset not in _NATIVE:
            raise ValueError("Not a native MiniNex market")
        return (self.btc if asset=="BTC" else self.extra).history(
            asset,limit=limit)


def _original_owner_native_session(row:object)->dict:
    if type(row) is not dict:
        raise ValueError("No real owner-started native session")
    asset=row.get("asset")
    stable=row.get("stable")
    started=row.get("started_by_owner_at")
    if (asset not in _NATIVE or stable not in _STABLES[asset]
            or row.get("status")!="ACTIVE"
            or type(started) is not datetime or started.tzinfo is None
            or type(row.get("budget_revision")) is not int
            or row["budget_revision"]<=0
            or row.get("mode") not in ("LONG","AUTO","SPOT_SHORT")
            or row.get("horizon") not in _WINDOWS
            or (asset=="BTC" and (
                type(row.get("budget_percent")) is not int
                or not 0<row["budget_percent"]<=100))
            or (asset!="BTC" and (
                type(row.get("budget_usd")) is not int
                or row["budget_usd"]<=0))):
        raise ValueError("Owner native trade setting or approval not current")
    _safe_did(row.get("privy_did"))
    return row


def _independent_native_owner_permission(
    report:object,*,row:dict,amount:str,
)->dict:
    """Never build/sign a grant from a market event or UI preference.

    The future native grant adaptor MUST fetch this report independently
    from the original user wallet, Privy current remote policy and the
    database's current signed budget. No other flag or confirmation works.
    """
    if (type(report) is not dict
            or report.get("audit_mode")!="CURRENT_NATIVE_OWNER_SIGNED_GRANT"
            or any(report.get(field)!=row[field] for field in (
                "did" if False else "asset","stable","budget_revision"))
            or report.get("did")!=row["privy_did"]
            or report.get("source_amount")!=amount
            or any(report.get(k) is not True for k in (
                "owner_wallet_current","exact_remote_policy_verified",
                "backend_quorum_current","owner_signed_budget_current",
                "user_consent_current"))
            or any(type(report.get(k)) is not str
                   or not _SHA.fullmatch(report[k]) for k in (
                   "owner_consent_sha256","privy_grant_sha256",
                   "policy_sha256"))
            or any(type(report.get(k)) is not str
                   or not _ID.fullmatch(report[k]) for k in (
                   "signer_id","policy_id"))
            or report.get("signer_id")==report.get("policy_id")
            or type(report.get("source_wallet")) is not str):
        raise ValueError("Independent current native owner Privy grant missing")
    return report


async def execute_owner_native_live_tick(
    row:dict,*,market,client:httpx.AsyncClient,grant_auditor,
    now:float|None=None,
)->dict:
    """One real market signal and (only with a remote owner grant) one BUY.

    This is a fully per-coin owner-capital decision. Native SELL cannot
    accidentally turn into an ETH stablecoin BUY, and an unsupported
    native SELL signer never causes a synthetic closed position.
    """
    row=_original_owner_native_session(row)
    if (type(client) is not httpx.AsyncClient
            or not callable(grant_auditor)):
        raise ValueError("Internal native execution context not configured")
    current=time.time() if now is None else now
    decision=verified_live_native_momentum(
        market,asset=row["asset"],horizon=row["horizon"],now=current)
    if decision.get("action")=="WAIT":
        return {"asset":row["asset"],"state":"WAIT",
                "reason":decision["reason"]}
    if decision.get("action")=="SELL":
        # SELL is no longer hard-blocked. The same independent grant auditor
        # is required. Actual signed native source settlement still depends
        # on a prepared and signed source; if that is missing the call fails
        # closed with a clear reason instead of a permanent block.
        amount=await verified_owner_native_buy_amount(
            row["privy_did"],asset=row["asset"],stable=row["stable"],
            budget_usd=(row["budget_usd"] if row["asset"]!="BTC" else None),
            budget_percent=(row["budget_percent"] if row["asset"]=="BTC" else None),
            client=client)
        if amount is None:
            return {"asset":row["asset"],"state":"WAIT",
                    "reason":"NO_VERIFIED_NATIVE_OWNER_AVAILABLE_CAPITAL"}
        run="owner_"+row["started_by_owner_at"].astimezone(
            timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        event="market_"+str(int(current//60))
        audit=await grant_auditor(
            row["privy_did"],asset=row["asset"],stable=row["stable"],
            amount=amount,budget_revision=row["budget_revision"],
            signal_event_id=event)
        evidence=_independent_native_owner_permission(
            audit,row=row,amount=amount)
        # Re-use the BUY coordinator infrastructure for the grant check;
        # the actual SELL broadcast still requires a prepared signed source.
        # For now we surface that the grant is valid and the path is open.
        return {"asset":row["asset"],"state":"SELL_GRANT_VERIFIED",
                "reason":"NATIVE_SELL_GRANT_OK_AWAITING_SIGNED_SOURCE",
                "audit_mode":evidence.get("audit_mode")}
    if decision.get("action")!="BUY":
        raise ValueError("Invalid actual live market signal")
    amount=await verified_owner_native_buy_amount(
        row["privy_did"],asset=row["asset"],stable=row["stable"],
        budget_usd=(row["budget_usd"] if row["asset"]!="BTC" else None),
        budget_percent=(row["budget_percent"] if row["asset"]=="BTC" else None),
        client=client)
    if amount is None:
        return {"asset":row["asset"],"state":"WAIT",
                "reason":"NO_VERIFIED_NATIVE_OWNER_AVAILABLE_CAPITAL"}
    # Each account/coin has a unique signal identity in UTC minute precision.
    # The original database journal is independently idempotent.
    run="owner_"+row["started_by_owner_at"].astimezone(
        timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    event="market_"+str(int(current//60))
    audit=await grant_auditor(
        row["privy_did"],asset=row["asset"],stable=row["stable"],
        amount=amount,budget_revision=row["budget_revision"],
        signal_event_id=event)
    evidence=_independent_native_owner_permission(
        audit,row=row,amount=amount)
    return await coordinate_original_native_buy_once(
        row["privy_did"],asset=row["asset"],stable=row["stable"],
        amount=amount,strategy_run_id=run,signal_event_id=event,
        permission_audit=evidence,client=client)


async def run_owner_native_live_batch(
    rows:list[dict],*,market,client:httpx.AsyncClient,grant_auditor,
)->dict:
    """Parallel independent owners, serialize every shared Ethereum nonce.

    Batching is strictly nonce serialization, not user trading throttling.
    No automatic public call or uncontrolled event queue is created.
    """
    if type(rows) is not list:
        raise ValueError("Original owner-started session page unavailable")
    groups:dict[str,list[dict]]={}
    invalid=0
    for row in rows:
        if type(row) is not dict or type(row.get("privy_did")) is not str:
            invalid+=1
            continue
        groups.setdefault(row["privy_did"],[]).append(row)

    async def one_owner(sessions):
        attempted=failed=0
        for session in sessions:
            try:
                await execute_owner_native_live_tick(
                    session,market=market,client=client,
                    grant_auditor=grant_auditor)
                attempted+=1
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Native owner signal needs grant or chain evidence")
                failed+=1
        return attempted,failed

    results=await asyncio.gather(*(one_owner(x) for x in groups.values()))
    return {"sessions_seen":len(rows),
            "attempted":sum(x for x,_ in results),
            "failed":invalid+sum(y for _,y in results),
            "serialized_eth_wallet_groups":len(groups)}
