"""Owner-initiated read-only Privy trade signer offer for five EVM coins.

A signed individual budget intent and independently fetched EXACT published
Privy policy must exist before the owner can call addSigners in their wallet.
No permission is attached by this reader. No order is sent.
"""
from __future__ import annotations

import asyncio
import time

from fastapi import HTTPException

from .shared_evm_privy_grant_confirm import _read_candidate
from .shared_evm_remote_grant_audit import validate_pinned_evm_policy
from .shared_evm_privy_grant_review import verify_exact_remote_evm_policy
from .shared_evm_owner_consent import _asset
from .privy_wallet_verification import fetch_privy_user
from .privy_live_signer import _owned_eth_wallet
from .eth_privy_user_grant import _fetch_policy
from .shared_evm_trade_policy_roles import check_coin_trade_backend_quorum as check_privy_backend_quorum
from .storage import _safe_did


def validate_owner_trade_grant_offer(
    *,asset:str,record:object,owned:object,expected:object,
    remote_policy:object,quorum:object,
)->dict:
    if (type(record) is not dict or type(owned) is not dict
            or type(expected) is not dict
            or record.get("asset")!=asset
            or not (
                record.get("status")=="CONSENT_VERIFIED"
                and record.get("consent_verified_at") is not None
                or record.get("status")=="DRAFT"
                and record.get("authorization_method")=="PRIVY_SCOPED_GRANT"
                and record.get("consent_verified_at") is None
                and record.get("consent_sha256") is None)
            or record.get("revoked_at") is not None
            or record.get("wallet_id")!=owned.get("wallet_id")
            or record.get("wallet_address")!=owned.get("address")
            or type(record.get("policy_id")) is not str
            or not 6<=len(record["policy_id"])<=160
            or type(record.get("signer_id")) is not str
            or not 6<=len(record["signer_id"])<=160
            or record.get("remote_policy_sha256")!=record.get("policy_sha256")):
        raise HTTPException(409,"No exact owner-authorized trading signer grant offer")
    try:
        reviewed=verify_exact_remote_evm_policy(remote_policy,expected)
    except (ValueError,TypeError,KeyError):
        raise HTTPException(409,"Privy owner trading policy not verified") from None
    if (type(reviewed) is not dict
            or reviewed.get("remote_exact_match") is not True
            or reviewed.get("policy_id")!=record["policy_id"]
            or reviewed.get("policy_sha256")!=record["policy_sha256"]
            or type(quorum) is not dict
            or quorum.get("remote_quorum_public_key_verified") is not True):
        raise HTTPException(409,"Privy original trading policy or quorum changed")
    combined=record.get("authorization_method")=="PRIVY_SCOPED_GRANT"
    fee_scope=None
    if combined:
        rules=expected.get("rules",[])
        fee=rules[-1] if type(rules) is list and rules else None
        if type(fee) is not dict or "owner-approved net-profit fee" not in fee.get("name",""):
            raise HTTPException(409,"One-grant policy missing explicit 5 percent fee scope")
        fields={(c.get("field"),c.get("operator")):c.get("value")
                for c in fee.get("conditions",[]) if type(c) is dict}
        treasury=fields.get(("transfer.to","eq"))
        maximum=fields.get(("transfer.value","lte"))
        if (type(treasury) is not str or len(treasury)!=42
                or type(maximum) is not str or not maximum.isdigit()):
            raise HTTPException(409,"Privy fee recipient and amount not explicit")
        fee_scope={
            "treasury_wallet":treasury,
            "fee_max_atomic":maximum,
            "performance_fee_percent":5,
            "fee_basis":"REALIZED_NET_GAIN_ABOVE_HWM",
            "router_allowance_scoped":True,
        }
    return {
        "combined_single_grant":combined,
        "fee_scope":fee_scope,
        "mode":"OWNER_EVM_TRADING_SIGNER_OFFER_ONLY",
        "asset":asset,"chain_id":record["chain_id"],
        "wallet_address":record["wallet_address"],
        "wallet_id":record["wallet_id"],
        "stable":record["stable"],"budget_revision":record["budget_revision"],
        "policy_id":record["policy_id"],
        "execution_signer_id":record["signer_id"],
        "policy_sha256":record["policy_sha256"],
        "owner_add_signer_required":True,
        "privy_signer_attached":False,
        "live_session_started":False,
        "trade_sent":False,
    }


async def get_owner_trade_grant_offer(*,did:str,asset:str)->dict:
    did=_safe_did(did)
    asset=_asset(asset)
    now=int(time.time())
    record=await asyncio.to_thread(_read_candidate,did,asset,now)
    try:
        candidate=validate_pinned_evm_policy(record,asset=asset,now=now)
    except (TypeError,ValueError,KeyError):
        raise HTTPException(409,"Original wallet owner policy expired or changed") from None
    owned=_owned_eth_wallet(await fetch_privy_user(did),did)
    remote=await _fetch_policy(record["policy_id"])
    quorum=await check_privy_backend_quorum(asset)
    return validate_owner_trade_grant_offer(
        asset=asset,record=record,owned=owned,expected=candidate,
        remote_policy=remote,quorum=quorum)
