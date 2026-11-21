"""One original-native BUY strategy signal: approval -> fresh quote -> ETH source.

Internal strategy entry only. No public route, no background auto-enablement.
A user signs once for a restricted capital/Privy policy at final enrollment;
this coordinator only consumes independently verified owner grant evidence.
The first call may submit one exact token approval; a later signal tick observes
its finalized allowance before any buy. USDT zero-reset necessarily has its
own finality before the finite approval. Never infer trade completion from
a signed approval, Ethereum submission, or a THORChain quote.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
import re

import httpx
from fastapi import HTTPException

from .native_thor_swap_quote import get_private_native_thornode_terms
from .native_thor_buy_approval_plan import _read_original_thor_buy_allowance_from_quote
from .native_thor_buy_approval_send import submit_original_thor_approval_once
from .native_eth_native_buy_preflight import _preflight_verified_original_eth_buy
from .native_live_order_journal import (
    immutable_native_order_identity,persist_internal_native_order,
)
from .native_eth_buy_privy_dispatch import submit_original_eth_native_buy_once
from .storage import _safe_did

_ASSETS=frozenset(("BTC","SOL","XRP","TRX","DOGE"))
_SHA=re.compile(r"^[0-9a-f]{64}$")
_EVENT=re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_AMOUNT=re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_REQUIRED=("owner_wallet_current","exact_remote_policy_verified",
           "backend_quorum_current","owner_signed_budget_current")


def _validate_original_native_buy_intent(
    did:str,*,asset:str,stable:str,amount:str,strategy_run_id:str,
    signal_event_id:str,grant:dict,
)->None:
    if (asset not in _ASSETS or stable not in ("USDC","USDT")
            or (asset=="XRP" and stable!="USDC")
            or type(amount) is not str or not _AMOUNT.fullmatch(amount)
            or len(amount)>55 or type(strategy_run_id) is not str
            or not _EVENT.fullmatch(strategy_run_id)
            or type(signal_event_id) is not str
            or not _EVENT.fullmatch(signal_event_id)
            or type(grant) is not dict
            or grant.get("did")!=did or grant.get("asset")!=asset
            or grant.get("stable")!=stable
            or grant.get("source_amount")!=amount
            or type(grant.get("budget_revision")) is not int
            or grant["budget_revision"]<=0
            or type(grant.get("source_wallet")) is not str
            or any(grant.get(k) is not True for k in _REQUIRED)
            or any(type(grant.get(k)) is not str
                   or not _SHA.fullmatch(grant[k]) for k in (
                    "owner_consent_sha256","privy_grant_sha256",
                    "policy_sha256"))):
        raise HTTPException(409,"Owner native BUY requires signed current Privy budget")
    try:
        units=Decimal(amount)*Decimal(10**6)
        if not units.is_finite() or units<=0 or units!=int(units):
            raise ValueError("Invalid exact native stable amount")
    except (ValueError,InvalidOperation,OverflowError):
        raise HTTPException(422,"Invalid exact native stable quantity") from None


async def coordinate_original_native_buy_once(
    did:str,*,asset:str,stable:str,amount:str,strategy_run_id:str,
    signal_event_id:str,permission_audit:dict,client:httpx.AsyncClient,
)->dict:
    """Follow owner budget and current THORChain source, one stage per tick.

    The worker providing permission_audit must independently check the real
    remote Privy policy, signed user budget and active strategy event. This
    function cannot manufacture these records or bypass a missing grant.
    """
    did=_safe_did(did)
    _validate_original_native_buy_intent(
        did,asset=asset,stable=stable,amount=amount,
        strategy_run_id=strategy_run_id,signal_event_id=signal_event_id,
        grant=permission_audit)
    if type(client) is not httpx.AsyncClient:
        raise HTTPException(409,"Original native worker HTTP context invalid")
    # One private quote provides the same verified vault/memo/owner addresses
    # for the allowance read and subsequent gas/nonce preflight.
    quote=await get_private_native_thornode_terms(
        did,asset=asset,side="BUY",stable=stable,amount=amount)
    if permission_audit["source_wallet"].lower()!=quote["source_wallet"].lower():
        raise HTTPException(409,"Native source wallet changed since owner consent")
    allowance=await _read_original_thor_buy_allowance_from_quote(
        did,asset=asset,stable=stable,amount=amount,quote=quote)
    if allowance.get("owner_wallet_needs_erc20_approval") is True:
        sent=await submit_original_thor_approval_once(
            did,asset=asset,stable=stable,amount=amount,
            signal_event_id=signal_event_id,
            permission_audit=permission_audit,client=client)
        return {
            "mode":"ORIGINAL_NATIVE_BUY_AWAITING_APPROVAL_FINALITY",
            "asset":asset,"side":"BUY","stable":stable,
            "stage":sent.get("mode"),
            "approval_state":sent.get("state"),
            "approval_tx_hash":sent.get("tx_hash"),
            "source_trade_sent":False,
            "native_destination_settled":False,
            "fee_assessed":False,"resend_permitted":False,
        }
    if allowance.get("owner_wallet_needs_erc20_approval") is not False:
        raise HTTPException(409,"Original ERC20 router allowance unavailable")
    preflight=await _preflight_verified_original_eth_buy(
        did,asset=asset,stable=stable,amount=amount,original=quote)
    identity=immutable_native_order_identity(
        did=did,asset=asset,side="BUY",stable=stable,
        source_wallet=quote["source_wallet"],
        destination_wallet=quote["destination_wallet"],
        amount=amount,quote=quote,
        quote_expires_at=quote["quote_expires_at"],
        strategy_run_id=strategy_run_id,signal_event_id=signal_event_id,
        budget_revision=permission_audit["budget_revision"],
        owner_consent_sha256=permission_audit["owner_consent_sha256"],
        privy_grant_sha256=permission_audit["privy_grant_sha256"])
    saved=await asyncio.to_thread(persist_internal_native_order,identity)
    if saved.get("state")!="PREPARED":
        # Previously claimed, never re-send even if a user presses Start twice.
        raise HTTPException(409,"Original native BUY already claimed or submitted")
    audit={**permission_audit,
           "payload_sha256":identity["payload_sha256"],
           "owner_consent_sha256":identity["owner_consent_sha256"],
           "privy_grant_sha256":identity["privy_grant_sha256"],
           "budget_revision":identity["budget_revision"],
           "source_wallet":identity["source_wallet"]}
    sent=await submit_original_eth_native_buy_once(
        did,asset=asset,intent_key=identity["intent_key"],
        quote=quote,preflight=preflight,
        permission_audit=audit,client=client)
    return {
        "mode":"ORIGINAL_NATIVE_BUY_ETH_SOURCE_AWAITING_SETTLEMENT",
        "asset":asset,"side":"BUY","stable":stable,
        "intent_key":identity["intent_key"],
        "source_tx_hash":sent.get("source_tx_hash"),
        "source_state":sent.get("journal_state"),
        "source_trade_sent":sent.get("journal_state")=="SUBMITTED",
        "native_destination_settled":False,
        "fee_assessed":False,"resend_permitted":False,
    }
