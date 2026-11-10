"""Server-side Privy ETH policy inspection and fail-closed rule diagnostics.

A matching policy ID is not enough to grant delegated trading. A policy may
also allow unrestricted calldata, wrong networks or arbitrary recipients.
This module fetches the actual rule contents from Privy and checks a
conservative per-wallet subset. Static validation is NOT a formal audit.

No signer registration, wallet grant, or transaction execution exists here.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time

import httpx
from fastapi import HTTPException

from .privy_live_signer import signer_configuration
from .privy_wallet_verification import APP_ID, fetch_privy_user
from .wallet_balances import verified_addresses
from .storage import _safe_did

LOG = logging.getLogger("mininex.privy_eth_policy")
_POLICY_URL = "https://api.privy.io/v1/policies/"
_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")
_ETH_ADDR = re.compile(r"^0x[a-fA-F0-9]{40}$")
# Conservative ceiling for policy-level native ETH exposure, not order funding.
_MAX_NATIVE_WEI = 10**18
_MAX_POLICY_LIFETIME_SEC = 30*24*3600


def _canonical_rules_hash(policy: object) -> str:
    """Hash only security-relevant canonical policy fields, including rules.

    The operator must obtain and pin this digest *after* independently
    reviewing the source policy, not from user-supplied JSON.
    """
    if not isinstance(policy, dict) or not isinstance(policy.get("rules"), list):
        raise ValueError("Unrecognized Privy policy payload")
    relevant = {
        "version": policy.get("version"),
        "chain_type": policy.get("chain_type"),
        "rules": policy["rules"],
    }
    return hashlib.sha256(json.dumps(
        relevant, sort_keys=True, separators=(",",":"), ensure_ascii=True,
    ).encode("utf-8")).hexdigest()


def _positive_integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int,str)):
        return None
    if isinstance(value,str):
        if not value or len(value)>80:
            return None
        try:
            n = int(value, 16 if value.startswith("0x") else 10)
        except ValueError:
            return None
    else:
        n=value
    return n if n >= 0 else None


def _has(condition_list: list, source: str, field: str, operator: str,
         predicate) -> bool:
    for condition in condition_list:
        if not isinstance(condition,dict):
            continue
        if (condition.get("field_source") == source
                and condition.get("field") == field
                and condition.get("operator") == operator
                and predicate(condition.get("value"))):
            if source == "ethereum_calldata":
                abi = condition.get("abi")
                if not isinstance(abi, list) or not abi:
                    continue
            return True
    return False


def inspect_policy_structure(policy: object, *, policy_id: str,
                             wallet_address: str, router_address: str,
                             now: int | None = None) -> dict:
    """Conservative prerequisites; never a complete EVM calldata audit."""
    checked = {
        "policy_id_matches": False,
        "ethereum_only": False,
        "bounded_allow_rules": False,
        "mainnet_only": False,
        "router_only": False,
        "native_value_bounded": False,
        "calldata_function_restricted": False,
        "recipient_wallet_bound": False,
        "amount_bounded": False,
        "time_bounded": False,
        "structure_checked": False,
    }
    if (not isinstance(policy,dict) or policy.get("id") != policy_id
            or policy.get("version") != "1.0"
            or policy.get("chain_type") != "ethereum"
            or not isinstance(wallet_address,str) or not _ETH_ADDR.fullmatch(wallet_address)
            or not isinstance(router_address,str) or not _ETH_ADDR.fullmatch(router_address)):
        return checked
    checked["policy_id_matches"] = True
    checked["ethereum_only"] = True
    rules=policy.get("rules")
    if not isinstance(rules,list) or not 1 <= len(rules) <= 8:
        return checked
    # Unknown methods, unrestricted allow rules and wildcard RPC access
    # are forbidden even when other rules are restrictive.
    allowed=[]
    for rule in rules:
        if not isinstance(rule,dict) or rule.get("method") != "eth_sendTransaction":
            return checked
        if rule.get("action") not in ("ALLOW","DENY"):
            return checked
        if rule["action"] == "ALLOW":
            if not isinstance(rule.get("conditions"),list) or not rule["conditions"]:
                return checked
            allowed.append(rule)
    if not allowed:
        return checked
    checked["bounded_allow_rules"] = True
    ref_time=int(time.time()) if now is None else now
    requirements={
        "mainnet_only": [],
        "router_only": [],
        "native_value_bounded": [],
        "calldata_function_restricted": [],
        "recipient_wallet_bound": [],
        "amount_bounded": [],
        "time_bounded": [],
    }
    for rule in allowed:
        conditions=rule["conditions"]
        # All conditions in a rule are AND; rules are OR. Every ALLOW rule
        # must include all protections or a looser ALLOW could bypass them.
        requirements["mainnet_only"].append(_has(
            conditions,"ethereum_transaction","chain_id","eq",
            lambda v: _positive_integer(v) == 1))
        requirements["router_only"].append(_has(
            conditions,"ethereum_transaction","to","eq",
            lambda v: isinstance(v,str) and v.lower() == router_address.lower()))
        requirements["native_value_bounded"].append(_has(
            conditions,"ethereum_transaction","value","lte",
            lambda v: _positive_integer(v) is not None
                      and _positive_integer(v) <= _MAX_NATIVE_WEI))
        requirements["calldata_function_restricted"].append(_has(
            conditions,"ethereum_calldata","function_name","eq",
            lambda v: v == "exactInputSingle"))
        requirements["recipient_wallet_bound"].append(_has(
            conditions,"ethereum_calldata","exactInputSingle.params.recipient","eq",
            lambda v: isinstance(v,str) and v.lower()==wallet_address.lower()))
        requirements["amount_bounded"].append(_has(
            conditions,"ethereum_calldata","exactInputSingle.params.amountIn","lte",
            lambda v: _positive_integer(v) is not None
                      and 0 < _positive_integer(v) <= 10**18))
        requirements["time_bounded"].append(_has(
            conditions,"system","current_unix_timestamp","lt",
            lambda v: _positive_integer(v) is not None
                      and ref_time < _positive_integer(v)
                      <= ref_time+_MAX_POLICY_LIFETIME_SEC))
    for name, results in requirements.items():
        checked[name] = all(results)
    checked["structure_checked"] = all(
        checked[name] for name in ("policy_id_matches", "ethereum_only",
        "bounded_allow_rules", *requirements.keys()))
    return checked


async def fetch_privy_policy(policy_id: str) -> dict:
    config=signer_configuration()
    if not config or config["policy_id"] != policy_id:
        raise HTTPException(503,"Expected Privy policy is not configured")
    secret=os.getenv("MININEX_PRIVY_APP_SECRET","").strip()
    if not secret:
        raise HTTPException(503,"Privy policy verification is not configured")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(9.0),
                                     follow_redirects=False) as client:
            resp=await client.get(_POLICY_URL+policy_id,
                auth=(APP_ID,secret),
                headers={"privy-app-id":APP_ID,"Accept":"application/json"})
            resp.raise_for_status()
            if len(resp.content) > 256_000:
                raise ValueError("Privy policy payload too large")
            policy=resp.json()
        if not isinstance(policy,dict) or policy.get("id") != policy_id:
            raise ValueError("Policy identity mismatch")
        return policy
    except (httpx.HTTPError,ValueError,TypeError):
        LOG.warning("Privy ETH policy verification temporarily unavailable")
        raise HTTPException(503,"Privy policy rules cannot be verified") from None


async def get_eth_policy_review(did: str) -> dict:
    """Authenticated, read-only readiness for a user-owned ETH wallet."""
    _safe_did(did)
    config=signer_configuration()
    router=os.getenv("MININEX_ETH_SWAP_ROUTER","").strip()
    pin=os.getenv("MININEX_PRIVY_ETH_POLICY_SHA256","").strip().lower()
    has_pin=bool(_SHA256.fullmatch(pin))
    result={
        "mode":"PRIVY_POLICY_STATIC_REVIEW",
        "asset":"ETH",
        "chain_id":1,
        "policy_id_configured":config is not None,
        "router_configured":bool(_ETH_ADDR.fullmatch(router)),
        "policy_hash_pinned":has_pin,
        "policy_digest_matches":False,
        "policy_structure_checked":False,
        "recipient_wallet_bound":False,
        "policy_contents_independently_audited":False,
        "grant_enrollment_enabled":False,
        "execution_enabled":False,
        "checks":{},
    }
    if not config or not has_pin or not _ETH_ADDR.fullmatch(router):
        return result
    user=await fetch_privy_user(did)
    try:
        wallet=verified_addresses(user,did)["ETH"]
    except ValueError:
        return result
    if not wallet or not _ETH_ADDR.fullmatch(wallet):
        return result
    policy=await fetch_privy_policy(config["policy_id"])
    digest=_canonical_rules_hash(policy)
    result["policy_digest_matches"] = digest == pin
    if not result["policy_digest_matches"]:
        return result
    checks=inspect_policy_structure(policy,policy_id=config["policy_id"],
                                   wallet_address=wallet,router_address=router)
    result["checks"]=checks
    result["policy_structure_checked"]=checks["structure_checked"]
    result["recipient_wallet_bound"]=checks["recipient_wallet_bound"]
    # A successful static check is NOT a third-party code audit and does not
    # establish runtime transaction decoding/route security; enrollment off.
    return result
