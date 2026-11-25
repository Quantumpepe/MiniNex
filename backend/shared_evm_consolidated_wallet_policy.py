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
from .ethereum_wrapped_spot_policy import (
    WRAPPED_SPOT_POLICY, WRAPPED_SPOT_TOKENS, build_wrapped_spot_policy,
)

# Canonical order; ETH and QNT share the existing exact EVM contract route factory.
_EVM_ASSETS = ("ETH", "BNB", "POL", "AVAX", "LINK", "QNT", "UNI", "ARB", "BTC", "XRP", "SOL", "TRX")
_REQUIRED_SCOPE_FIELDS = frozenset((
    "asset", "stable", "treasury", "buy_source_atomic_cap",
    "sell_source_atomic_cap", "native_wrap_atomic_cap",
    "owner_max_fee_atomic", "expires_at",
))


DIRECT_SETTLEMENT_POLICY="ETHEREUM_ACROSS_DIRECT_V1"
DIRECT_SETTLEMENT_ASSETS=frozenset(("BNB","POL","ARB"))


def valid_scope_fields(scope):
    if type(scope) is not dict:
        return False
    if set(scope)==_REQUIRED_SCOPE_FIELDS:
        return scope.get("asset") not in WRAPPED_SPOT_TOKENS
    if scope.get("settlement_policy")==WRAPPED_SPOT_POLICY:
        return (set(scope)==_REQUIRED_SCOPE_FIELDS|{"settlement_policy"}
                and scope.get("asset") in WRAPPED_SPOT_TOKENS
                and scope.get("native_wrap_atomic_cap") is None
                and scope.get("owner_max_fee_atomic")=="0")
    return (set(scope)==_REQUIRED_SCOPE_FIELDS|{"settlement_policy"}
            and scope.get("settlement_policy")==DIRECT_SETTLEMENT_POLICY
            and scope.get("asset") in DIRECT_SETTLEMENT_ASSETS
            and scope.get("native_wrap_atomic_cap") is None
            and scope.get("owner_max_fee_atomic")=="0")


def is_direct_settlement_upgrade(asset,old):
    return (asset in DIRECT_SETTLEMENT_ASSETS and valid_scope_fields(old)
            and old.get("asset")==asset and "settlement_policy" not in old)


def changed_wallet_coin(original,target,original_revisions=None,target_revisions=None):
    """Only add one coin or migrate one legacy scope to Ethereum direct settlement.

    No sibling changes/removals or stablecoin/budget revision changes are
    allowed by the migration path. The exact target still requires the
    original wallet's signed policy digest and independent Privy readback.
    """
    if (type(original) is not dict or type(target) is not dict or not original
            or not set(original).issubset(target)):
        raise ValueError("Original approved coin scope removed")
    changed=[a for a in target if a not in original or target[a]!=original[a]]
    if len(changed)!=1:
        raise ValueError("Exactly one approved coin scope may change")
    asset=changed[0]
    if asset in original:
        before,after=original[asset],target[asset]
        if (not is_direct_settlement_upgrade(asset,before)
                or not valid_scope_fields(after)
                or after.get("settlement_policy")!=DIRECT_SETTLEMENT_POLICY
                or after.get("asset")!=asset or after.get("stable")!=before.get("stable")):
            raise ValueError("Only original Ethereum settlement upgrade is allowed")
    if original_revisions is not None or target_revisions is not None:
        if (type(original_revisions) is not dict or type(target_revisions) is not dict
                or set(original_revisions)!=set(original)
                or set(target_revisions)!=set(target)
                or any(target_revisions.get(a)!=v for a,v in original_revisions.items())):
            raise ValueError("Original approved budget revision changed")
    return asset


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
        if not valid_scope_fields(scope):
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
        if args.get("settlement_policy")==WRAPPED_SPOT_POLICY:
            per_coin=build_wrapped_spot_policy(
                asset=asset,stable=args["stable"],wallet=wallet,
                owner_id=owner_id,executor_id=executor_id,now=now,
                buy_source_atomic_cap=args["buy_source_atomic_cap"],
                sell_source_atomic_cap=args["sell_source_atomic_cap"],
                expires_at=args["expires_at"])
        elif args.get("settlement_policy")==DIRECT_SETTLEMENT_POLICY:
            from .ethereum_settlement_policy_migration import build_recurring_direct_policy
            draft=build_recurring_direct_policy(asset=asset,stable=args["stable"],
                wallet=wallet,owner_id=owner_id,executor_id=executor_id,
                buy_cap_micro=args["buy_source_atomic_cap"],
                sell_cap_atomic=args["sell_source_atomic_cap"],
                expires_at=args["expires_at"],now=now)
            per_coin=draft["policy"]
            # This version authorizes only existing ERC20/wrapped inventory,
            # never unrelated native gas holdings, arbitrary nested calls
            # or any fee collection. Old scopes retain their original digest.
            per_coin["rules"]=[r for r in per_coin["rules"]
                               if not r["name"].endswith(" native")]
        else:
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
