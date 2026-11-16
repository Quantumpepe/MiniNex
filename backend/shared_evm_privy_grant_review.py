"""Exact Privy remote-policy and wallet-signer readback for five EVM assets.

This is a single *read-only* verification core for BNB/POL/AVAX/LINK/UNI.
It does not accept a frontend assertion that a grant exists. A remote
Privy policy must match every action/condition of the server's reviewed
chain-specific draft, and the exact policy must be the only override on
the owner's exact additional signer. This is not durable user consent,
policy creation, a verified spending budget, or LIVE activation.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

from .privy_live_signer import _owned_eth_wallet, _scoped_signer
from .shared_evm_privy_user_policy import build_user_evm_policy_blueprint
from .shared_evm_swap_receipts import _pair

_ID=re.compile(r"^[A-Za-z0-9_-]{6,160}$")
_FIELDS=frozenset(("name","method","action","conditions"))


def verify_exact_remote_evm_policy(remote:object,expected:object)->dict:
    """Compare every remote condition; ignore only generated rule IDs."""
    if type(remote) is not dict or type(expected) is not dict:
        raise ValueError("Malformed Privy EVM policy")
    if set(expected)!={"name","version","chain_type","owner_id","rules"}:
        raise ValueError("Unexpected Privy EVM policy draft keys")
    pid=remote.get("id")
    if type(pid) is not str or not _ID.fullmatch(pid):
        raise ValueError("Missing remote policy ID")
    if any(remote.get(key)!=expected[key]
           for key in ("name","version","chain_type","owner_id")):
        raise ValueError("Different remote Privy policy owner or metadata")
    if (type(remote.get("rules")) is not list
            or type(expected.get("rules")) is not list
            or len(remote["rules"])!=len(expected["rules"])
            or len(expected["rules"]) not in (2,4)):
        raise ValueError("Unexpected EVM Privy permissions")
    ids=set()
    rules=[]
    for raw in remote["rules"]:
        if type(raw) is not dict or set(raw)!=_FIELDS|{"id"}:
            raise ValueError("Unexpected remote EVM permission fields")
        rid=raw.get("id")
        if type(rid) is not str or not _ID.fullmatch(rid) or rid in ids:
            raise ValueError("Missing or repeated remote Privy permission ID")
        ids.add(rid)
        if type(raw.get("conditions")) is not list:
            raise ValueError("Invalid remote Privy condition format")
        rules.append({k:deepcopy(raw[k]) for k in _FIELDS})
    if rules!=expected["rules"]:
        raise ValueError("Remote EVM policy is not the reviewed exact draft")
    digest=hashlib.sha256(json.dumps(
        expected,sort_keys=True,separators=(",",":"),
    ).encode("ascii")).hexdigest()
    return {"policy_id":pid,"policy_sha256":digest,
            "remote_exact_match":True,"additional_signer_granted":False,
            "live_execution_enabled":False}


def assess_owner_evm_privy_grant(
    *,did:str,asset:str,stable:str,user:object,
    wallet_metadata:object,remote_policy:object,policy_id:str,
    signer_id:str,owner_id:str,executor_id:str,
    buy_source_atomic_cap:str,sell_source_atomic_cap:str,
    native_wrap_atomic_cap:str|None,expires_at:int,now:int,
)->dict:
    """Evaluate actual server-read Privy user, wallet and exact remote rules.

    Any caller of this INTERNAL function must obtain remote_policy,
    wallet_metadata and policy_id from independent server records/Privy GET;
    untrusted request payloads are never authority to sign or trade.
    """
    pair=_pair(asset,stable,"BUY")
    result={
        "mode":"SHARED_EVM_PRIVY_OWNER_GRANT_REVIEW_ONLY",
        "asset":asset,"network":pair["network"],"chain_id":pair["chain_id"],
        "wallet_owned_by_user":False,
        "remote_policy_exactly_verified":False,
        "wallet_signer_binding_verified":False,
        "verified_wallet_address":None,
        "policy_id":None,
        "user_signed_consent_ledger_verified":False,
        "live_budget_ledger_verified":False,
        "remote_quorum_verified":False,
        "durable_order_claim_verified":False,
        "live_trading_enabled":False,"can_submit":False,
    }
    try:
        owned=_owned_eth_wallet(user,did)
        draft=build_user_evm_policy_blueprint(
            asset=asset,stable=stable,wallet=owned["address"],
            owner_id=owner_id,executor_id=executor_id,
            buy_source_atomic_cap=buy_source_atomic_cap,
            sell_source_atomic_cap=sell_source_atomic_cap,
            native_wrap_atomic_cap=native_wrap_atomic_cap,
            expires_at=expires_at,now=now,
        )
        if (type(wallet_metadata) is not dict
                or wallet_metadata.get("id")!=owned["wallet_id"]
                or wallet_metadata.get("owner",{}).get("user_id")!=did
                or type(policy_id) is not str or not _ID.fullmatch(policy_id)
                or type(signer_id) is not str or not _ID.fullmatch(signer_id)
                or signer_id!=executor_id):
            return result
        result["wallet_owned_by_user"]=True
        result["verified_wallet_address"]=owned["address"]
        remote=verify_exact_remote_evm_policy(remote_policy,draft)
        if remote["policy_id"]!=policy_id:
            return result
        result["remote_policy_exactly_verified"]=True
        result["policy_id"]=policy_id
        result["wallet_signer_binding_verified"]=_scoped_signer(
            wallet_metadata,did=did,address=owned["address"],
            config={"signer_id":signer_id,"policy_id":policy_id},
        )
    except (ValueError,KeyError,TypeError,AttributeError):
        return result
    return result
