"""Offline composer for ONE Privy signer and ONE EVM policy per user wallet.

Privy currently supports one override policy per additional signer on a wallet.
A per-coin policy and a shared signer cannot be attached repeatedly: that
would replace other coins' permissions. Compose exact per-coin allowed
transactions into a single bounded policy instead.

This is a pure candidate builder only. It DOES NOT publish policies, grant
signers, mutate prior grants, change sessions, or submit transactions.
A production migration must lock all per-coin budget revisions, obtain
owner consent for any expanded scope, reconcile current Privy policy, and
confirm exact wallet metadata before enabling this route.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from .shared_evm_single_grant_policy import build_single_grant_evm_policy

# Canonical order; ETH and QNT share the existing exact EVM contract route factory.
_EVM_ASSETS = ("ETH", "BNB", "POL", "AVAX", "LINK", "QNT", "UNI", "ARB")
_REQUIRED_SCOPE_FIELDS = frozenset((
    "asset", "stable", "treasury", "buy_source_atomic_cap",
    "sell_source_atomic_cap", "native_wrap_atomic_cap",
    "owner_max_fee_atomic", "expires_at",
))


def compose_scoped_evm_wallet_policy(
    *, wallet: str, owner_id: str, executor_id: str,
    scopes: list[dict], now: int,
) -> dict:
    """Create one DENY-by-default wallet policy from explicit coin budgets.

    The caller must pass server-verified scope values. Never derive budgets,
    treasury addresses, or authorization state from user-supplied JSON.
    Separate BUY/SELL, finite approvals, and HWM-fee transfer rules are
    preserved exactly from the existing strict per-coin factory.
    """
    if type(scopes) is not list or not 1 <= len(scopes) <= len(_EVM_ASSETS):
        raise ValueError("One or more explicit, verified EVM coin scopes required")
    selected = {}
    for scope in scopes:
        if type(scope) is not dict or set(scope) != _REQUIRED_SCOPE_FIELDS:
            raise ValueError("Only exact verified coin policy fields are accepted")
        asset = scope["asset"]
        if type(asset) is not str or asset not in _EVM_ASSETS or asset in selected:
            raise ValueError("Unsupported or duplicate EVM coin scope")
        selected[asset] = dict(scope)

    rules = []
    for asset in _EVM_ASSETS:
        if asset not in selected:
            continue
        args = selected[asset]
        per_coin = build_single_grant_evm_policy(
            wallet=wallet, owner_id=owner_id, executor_id=executor_id,
            now=now, **args,
        )
        if (per_coin.get("version") != "1.0"
                or per_coin.get("chain_type") != "ethereum"
                or per_coin.get("owner_id") != owner_id
                or type(per_coin.get("rules")) is not list):
            raise ValueError("Cannot combine mismatched Privy policy templates")
        for rule in per_coin["rules"]:
            if (type(rule) is not dict
                    or rule.get("method") != "eth_sendTransaction"
                    or rule.get("action") != "ALLOW"
                    or type(rule.get("conditions")) is not list
                    or not rule["conditions"]):
                raise ValueError("Unscoped or malformed Privy permission")
            rules.append(deepcopy(rule))
    if len({rule["name"] for rule in rules}) != len(rules):
        raise ValueError("Duplicate wallet policy rule")
    return {
        "version": "1.0",
        "chain_type": "ethereum",
        "name": f"MiniNex-EVM-wallet-{wallet[-8:].lower()}",
        "owner_id": owner_id,
        "rules": rules,
    }


def verified_wallet_policy_digest(policy: object, **original_kwargs) -> str:
    """Hash only a byte-for-byte-equivalent strict recomputed candidate."""
    expected = compose_scoped_evm_wallet_policy(**original_kwargs)
    if type(policy) is not dict or policy != expected:
        raise ValueError("Wallet policy differs from approved coin scopes")
    return hashlib.sha256(json.dumps(
        expected, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
