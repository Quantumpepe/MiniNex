"""One authenticated read-only EVM AUTO readiness audit, per user and coin.

Combines actual Privy account/wallet ownership, current signer policy
reference, pinned backend signer quorum, and the durable independent per-asset
consent/grant record. This is an audit, NOT an automatic trade permit.
Policy-rule content must still be re-read from Privy against an exact
server-stored reviewed blueprint before ANY privileged execution.
"""
from __future__ import annotations

import asyncio
from fastapi import HTTPException

from .storage import _safe_did
from .shared_evm_permission_status import read_owner_evm_permission_status
from .privy_wallet_verification import fetch_privy_user
from .privy_live_signer import _owned_eth_wallet,fetch_privy_wallet_metadata
from .shared_evm_trade_policy_roles import check_coin_trade_backend_quorum as check_privy_backend_quorum

_ADDITIONAL=frozenset(("BNB","POL","AVAX","LINK","UNI"))


def assess_evm_auto_readiness(
    *,asset:str,permission:object,user:object,
    wallet:object,quorum:object,
)->dict:
    if asset not in _ADDITIONAL:
        raise ValueError("Invalid EVM AUTO readiness asset")
    if not all(type(v) is dict for v in (permission,user,wallet,quorum)):
        raise ValueError("Missing independently read Privy evidence")
    did=user.get("id")
    checks={
        "owner_privy_wallet_verified":False,
        "permission_record_matches_privy_owner_wallet":False,
        "owner_wallet_delegated":False,
        "owner_wallet_metadata_current":False,
        "per_asset_user_consent_recorded":False,
        "per_asset_policy_digest_matched_in_ledger":False,
        "per_asset_grant_recorded_and_not_expired":False,
        "registered_backend_quorum_public_key_verified":False,
        "current_remote_privy_policy_rules_verified":False,
        "durable_per_coin_budget_reserved":False,
        "consented_live_worker_session_active":False,
    }
    try:
        owned=_owned_eth_wallet(user,did)
        checks["owner_privy_wallet_verified"]=True
        checks["permission_record_matches_privy_owner_wallet"]=(
            permission.get("stored_wallet_id")==owned["wallet_id"]
            and str(permission.get("stored_wallet_address","")).lower()
                ==owned["address"])
        checks["owner_wallet_delegated"]=owned["delegated"] is True
        checks["owner_wallet_metadata_current"]=(
            wallet.get("id")==owned["wallet_id"]
            and wallet.get("chain_type")=="ethereum"
            and str(wallet.get("address","")).lower()==owned["address"]
            and type(wallet.get("owner")) is dict
            and wallet["owner"].get("user_id")==did)
    except (ValueError,KeyError,TypeError):
        owned=None
    checks["per_asset_user_consent_recorded"]=(
        permission.get("asset")==asset
        and permission.get("user_consent_recorded") is True)
    checks["per_asset_policy_digest_matched_in_ledger"]=(
        permission.get("asset")==asset
        and permission.get("server_policy_digest_matched") is True)
    checks["per_asset_grant_recorded_and_not_expired"]=(
        permission.get("asset")==asset
        and permission.get("signer_grant_recorded") is True
        and permission.get("policy_current") is True)
    checks["registered_backend_quorum_public_key_verified"]=(
        quorum.get("remote_quorum_public_key_verified") is True)
    blockers=[name for name,ready in checks.items() if ready is not True]
    return {
        "mode":"SHARED_EVM_OWNER_LIVE_READINESS_AUDIT_ONLY",
        "asset":asset,"chain_id":permission.get("chain_id"),
        "checks":checks,"blockers":blockers,
        "wallet_address":owned["address"] if owned is not None else None,
        "wallet_signing_authorized_by_this_audit":False,
        "policy_created_by_this_audit":False,
        "live_auto_execution_enabled":False,
        "can_broadcast":False,
    }


async def get_owner_evm_auto_readiness(did:str,asset:str)->dict:
    did=_safe_did(did)
    if asset not in _ADDITIONAL:
        raise HTTPException(404,"Unsupported EVM AUTO asset")
    permission=await asyncio.to_thread(
        read_owner_evm_permission_status,did,asset=asset)
    user=await fetch_privy_user(did)
    owned=_owned_eth_wallet(user,did)
    wallet=await fetch_privy_wallet_metadata(owned["wallet_id"])
    quorum=await check_privy_backend_quorum(asset)
    return assess_evm_auto_readiness(
        asset=asset,permission=permission,user=user,
        wallet=wallet,quorum=quorum)
