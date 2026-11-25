"""Independent, exact readback of ONE multi-coin EVM Privy owner grant.

The caller must obtain user, wallet and policy through server-side Privy GET,
not frontend request data. This only checks exact original ownership and
attached signer; it never publishes/updates policies or submits transactions.
"""
from __future__ import annotations

from copy import deepcopy
import re

from .privy_live_signer import _owned_eth_wallet, _scoped_signer
from .shared_evm_consolidated_wallet_policy import (
    compose_scoped_evm_wallet_policy, verified_wallet_policy_digest,
)

_ID = re.compile(r"^[A-Za-z0-9_-]{6,160}$")
_RULE_FIELDS = frozenset(("name", "method", "action", "conditions"))


def verify_remote_combined_policy(remote: object, *, expected: dict) -> str:
    """Compare arbitrary-length canonical multi-coin rules, IDs excepted."""
    if type(remote) is not dict or type(expected) is not dict:
        raise ValueError("Expected a verified original Privy policy")
    if set(expected) != {"version","chain_type","owner_id","name","rules"}:
        raise ValueError("Unrecognized combined policy format")
    policy_id = remote.get("id")
    if type(policy_id) is not str or not _ID.fullmatch(policy_id):
        raise ValueError("Missing original Privy policy ID")
    if any(remote.get(field) != expected[field] for field in (
        "version","chain_type","owner_id","name"
    )):
        raise ValueError("Remote Privy policy ownership or name changed")
    if (type(remote.get("rules")) is not list
            or len(remote["rules"]) != len(expected["rules"])):
        raise ValueError("Privy override policy rule count differs")
    unique_rule_ids = set()
    verified = []
    for rule in remote["rules"]:
        if type(rule) is not dict or set(rule) != _RULE_FIELDS | {"id"}:
            raise ValueError("Remote rule has unapproved fields")
        rid = rule.get("id")
        if type(rid) is not str or not _ID.fullmatch(rid) or rid in unique_rule_ids:
            raise ValueError("Missing or duplicate remote Privy rule ID")
        unique_rule_ids.add(rid)
        verified.append({key:deepcopy(rule[key]) for key in _RULE_FIELDS})
    if verified != expected["rules"]:
        raise ValueError("Remote Privy override rules differ from owner consent")
    return policy_id


def review_original_one_signer_wallet(
    *, did: str, user: dict, wallet_metadata: dict,
    remote_policy: dict, saved_grant: dict,
    scopes: list[dict], now: int,
) -> dict:
    """Verify wallet and policy with zero implicit migration or LIVE rights."""
    if (type(saved_grant) is not dict
            or saved_grant.get("status") != "GRANT_VERIFIED"
            or saved_grant.get("legacy_reconciliation_required") is not False
            or type(scopes) is not list):
        raise ValueError("Original multi-coin signer grant not verified")
    original = _owned_eth_wallet(user,did)
    if original["delegated"] is not True:
        raise ValueError("Original wallet has not approved an additional signer")
    if (original["wallet_id"] != saved_grant.get("wallet_id")
            or original["address"] != saved_grant.get("wallet_address")
            or type(wallet_metadata) is not dict
            or wallet_metadata.get("id") != original["wallet_id"]):
        raise ValueError("Privy original wallet identity changed")
    signer = saved_grant.get("signer_id")
    policy_id = saved_grant.get("policy_id")
    if (type(signer) is not str or not _ID.fullmatch(signer)
            or type(policy_id) is not str or not _ID.fullmatch(policy_id)):
        raise ValueError("Original wallet signer or policy missing")
    # No other signers may bypass the single signed override during migration.
    additional = wallet_metadata.get("additional_signers")
    if type(additional) is not list or len(additional) != 1:
        raise ValueError("Existing original wallet grants require reconciliation")
    if not _scoped_signer(
        wallet_metadata,did=did,address=original["address"],
        config={"signer_id":signer,"policy_id":policy_id},
    ):
        raise ValueError("Original wallet's single scoped signer differs")
    expected_args = dict(
        wallet=original["address"], owner_id=saved_grant["owner_id"],
        executor_id=signer, scopes=scopes, now=now, allow_expired=True,
    )
    expected = compose_scoped_evm_wallet_policy(**expected_args)
    digest = verified_wallet_policy_digest(expected, **expected_args)
    if digest != saved_grant.get("policy_sha256"):
        raise ValueError("Original policy digest or selected coin budgets changed")
    if verify_remote_combined_policy(remote_policy, expected=expected) != policy_id:
        raise ValueError("Different attached remote Privy policy")
    return {
        "mode":"SERVER_VERIFIED_ORIGINAL_ONE_SIGNER_EVM_WALLET",
        "did":did,
        "wallet_id":original["wallet_id"],
        "wallet":original["address"],
        "shared_signer_id":signer,
        "policy_id":policy_id,
        "policy_sha256":digest,
        "scope_coins":sorted({row["asset"] for row in scopes}),
        "original_privy_user_verified":True,
        "one_scoped_signer_verified":True,
        "remote_policy_exactly_verified":True,
        "needs_unverified_legacy_migration":False,
        "authorization_changed_by_this_review":False,
        "transaction_sent":False,
        "live_trading_started_by_this_review":False,
    }
