"""Owner-only readback of exact Privy auto-fee signer offer.

The wallet owner may add the restricted *fee* signer using Privy's SDK only
after a separately signed 5%-net-profit intent and policy publication.
This read-only endpoint never attaches a signer or transfers assets.
"""
from __future__ import annotations

import asyncio
import time

from fastapi import HTTPException

from .shared_evm_fee_auto_grant_confirm import _read_original_fee_grant
from .shared_evm_fee_policy_publish import original_signed_fee_policy
from .shared_evm_privy_grant_review import verify_exact_remote_evm_policy
from .shared_evm_owner_consent import _asset
from .privy_wallet_verification import fetch_privy_user
from .privy_live_signer import _owned_eth_wallet
from .eth_privy_user_grant import _fetch_policy
from .shared_evm_fee_policy_roles import check_privy_fee_backend_quorum as check_privy_backend_quorum
from .storage import _safe_did


def verify_owner_auto_fee_grant_offer(
    *,record:object,asset:str,expected:object,owned:object,
    remote:object,quorum:object,now:int,
)->dict:
    """Offer exactly the signed policy and signer; never broad delegation."""
    if (type(record) is not dict or type(expected) is not dict
            or type(owned) is not dict or asset!=record.get("asset")
            or record.get("status")!="REMOTE_UNATTACHED"
            or record.get("revoked_at") is not None
            or record.get("owner_signed_at") is None
            or record.get("policy_id") is None
            or record.get("policy_sha256")!=record.get("remote_policy_sha256")
            or record.get("wallet_id")!=owned.get("wallet_id")
            or record.get("wallet_address")!=owned.get("address")
            or type(record.get("signer_id")) is not str
            or not 6<=len(record["signer_id"])<=160
            or type(record.get("policy_id")) is not str
            or not 6<=len(record["policy_id"])<=160
            or type(now) is not int or now<=0
            or not record.get("expires_at")
            or int(record["expires_at"].timestamp())<=now):
        raise HTTPException(409,"No current owner-signed fee grant offer")
    try:
        result=verify_exact_remote_evm_policy(remote,expected)
    except (TypeError,KeyError,ValueError):
        raise HTTPException(409,"Original Privy fee policy is not current") from None
    if (type(result) is not dict
            or result.get("remote_exact_match") is not True
            or result.get("policy_id")!=record["policy_id"]
            or result.get("policy_sha256")!=record["policy_sha256"]
            or type(quorum) is not dict
            or quorum.get("remote_quorum_public_key_verified") is not True):
        raise HTTPException(409,"Fee owner signer policy or quorum not verified")
    return {
        "mode":"OWNER_EVM_FEE_SIGNER_GRANT_OFFER_ONLY",
        "asset":asset,"stable":record["stable"],
        "chain_id":record["chain_id"],
        "wallet_address":record["wallet_address"],
        "wallet_id":record["wallet_id"],
        "treasury_wallet":record["treasury_wallet"],
        "owner_max_fee_atomic":str(record["owner_max_fee_atomic"]),
        "policy_id":record["policy_id"],
        "execution_signer_id":record["signer_id"],
        "policy_sha256":record["policy_sha256"],
        "policy_expires_unix":int(record["expires_at"].timestamp()),
        "owner_grant_required":True,
        "privy_grant_attached":False,
        "automatic_payment_sent":False,
    }


async def get_owner_auto_fee_grant_offer(*,did:str,asset:str)->dict:
    """Authenticated owner must explicitly accept the returned Privy offer."""
    did=_safe_did(did)
    asset=_asset(asset)
    record=await asyncio.to_thread(_read_original_fee_grant,did,asset)
    if type(record) is not dict or record.get("status")!="REMOTE_UNATTACHED":
        raise HTTPException(409,"No original fee policy awaiting owner grant")
    now=int(time.time())
    try:
        expected=original_signed_fee_policy(record,asset=asset,now=now)
    except (ValueError,TypeError,KeyError):
        raise HTTPException(409,"Original signed Treasury scope unavailable") from None
    user=await fetch_privy_user(did)
    owned=_owned_eth_wallet(user,did)
    remote=await _fetch_policy(record["policy_id"])
    quorum=await check_privy_backend_quorum()
    return verify_owner_auto_fee_grant_offer(
        record=record,asset=asset,expected=expected,
        owned=owned,remote=remote,quorum=quorum,now=int(time.time()))
