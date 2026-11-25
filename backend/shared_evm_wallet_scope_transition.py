"""Offline transition planner for ONE EVM wallet policy and ONE Privy signer.

Privy permits one override policy per additional signer. Updating the rules
of a wallet's existing policy requires explicit user approval for any added
coin, larger budget, longer expiry, stablecoin change, or treasury change.

This module does NOT write to Privy or a database. The future controller
must persist a fresh one-use nonce, verify the original DID/wallet and
current Privy policy remotely, and consume the nonce under a DB lock BEFORE
using an approved update. A signature here is NOT a Privy wallet grant.
"""
from __future__ import annotations

import hashlib
import re

from .eth_user_consent import verify_eoa_eip191_signature
from .privy_wallet_verification import APP_ID
from .shared_evm_consolidated_wallet_policy import (
    compose_scoped_evm_wallet_policy, verified_wallet_policy_digest,
)

_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9_-]{6,160}$")
_NONCE = re.compile(r"^[0-9a-f]{64}$")
_LIFETIME_SECONDS = 300


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_wallet_identity(
    did: str, wallet: str, wallet_id: str,
    owner_id: str, executor_id: str,
) -> None:
    if (type(did) is not str or not did.startswith("did:privy:")
            or not 15 <= len(did) <= 256
            or type(wallet) is not str or not _ADDR.fullmatch(wallet)
            or type(wallet_id) is not str or not _ID.fullmatch(wallet_id)
            or type(owner_id) is not str or not _ID.fullmatch(owner_id)
            or type(executor_id) is not str or not _ID.fullmatch(executor_id)
            or owner_id == executor_id):
        raise ValueError("Original Privy user, wallet and independent signer required")


def _scopes_by_asset(scopes: list[dict]) -> dict[str, dict]:
    if type(scopes) is not list:
        raise ValueError("Expected verified wallet-wide coin scope list")
    result: dict[str, dict] = {}
    for row in scopes:
        if type(row) is not dict or type(row.get("asset")) is not str:
            raise ValueError("Expected exact canonical coin scope")
        asset = row["asset"]
        if asset in result:
            raise ValueError("Duplicate coin scope")
        result[asset] = row
    return result


def plan_wallet_policy_transition(
    *, did: str, wallet: str, wallet_id: str,
    owner_id: str, executor_id: str,
    existing_scopes: list[dict], desired_scopes: list[dict],
    existing_policy: dict | None,
    now: int, nonce: str,
) -> dict:
    """Compare strict recomputed policies without granting any permissions.

    Only the FIRST wallet addSigners grant avoids an EIP-191 pre-signature.
    For an existing wallet signer, any newly permitted or changed rule
    requires its own user-signed intent before owner-controlled policy edits.
    Removing exact coin scopes can only narrow the prior allowlist.
    """
    _validate_wallet_identity(did, wallet, wallet_id, owner_id, executor_id)
    if (type(now) is not int or now <= 0
            or type(nonce) is not str or not _NONCE.fullmatch(nonce)):
        raise ValueError("Fresh server-issued one-use nonce and time required")
    initial = _scopes_by_asset(existing_scopes)
    desired = _scopes_by_asset(desired_scopes)
    if not desired:
        raise ValueError("Empty wallet policy must revoke signer, not publish allowlists")
    target_args = dict(
        wallet=wallet, owner_id=owner_id,
        executor_id=executor_id, scopes=desired_scopes, now=now, allow_expired=True,
    )
    target = compose_scoped_evm_wallet_policy(**target_args)
    target_digest = verified_wallet_policy_digest(target, **target_args)
    prior_digest = "0"*64
    if initial:
        if type(existing_policy) is not dict:
            raise ValueError("A remote-verified original policy is required")
        current_args = dict(target_args, scopes=existing_scopes)
        prior_digest = verified_wallet_policy_digest(existing_policy, **current_args)
    elif existing_policy is not None:
        raise ValueError("Cannot silently overwrite a previous Privy wallet policy")

    added_or_changed = [
        coin for coin in desired
        if coin not in initial or desired[coin] != initial[coin]
    ]
    removed = [coin for coin in initial if coin not in desired]
    if not initial:
        transition = "INITIAL_OWNER_ADD_SIGNER_REQUIRED"
    elif added_or_changed:
        transition = "OWNER_SIGNED_POLICY_UPDATE_INTENT_REQUIRED"
    elif removed:
        transition = "REMOVE_UNNEEDED_COIN_RULES_ONLY"
    else:
        transition = "UNCHANGED"

    challenge = None
    if transition == "OWNER_SIGNED_POLICY_UPDATE_INTENT_REQUIRED":
        # Only one user-visible signMessage for a previously delegated wallet,
        # even when several coins are added in the same user request.
        changed = ",".join(sorted(added_or_changed))
        retained = ",".join(sorted(desired))
        issued, expires = now, now + _LIFETIME_SECONDS
        challenge = "\n".join((
            "MiniNex - authorize an update to my EVM trading scope",
            "I am changing the permissions of my existing Privy wallet.",
            "This signature is an intent to change a specific policy only.",
            "It is NOT a transaction, and does NOT by itself update Privy.",
            "No automatic approval for other coins, recipients or values.",
            f"Application: {APP_ID}",
            f"Privy User ID: {did}",
            f"Wallet: {wallet.lower()}",
            f"Wallet ID: {wallet_id}",
            f"Policy owner ID: {owner_id}",
            f"Shared execution signer ID: {executor_id}",
            f"Previous policy SHA256: {prior_digest}",
            f"Requested policy SHA256: {target_digest}",
            f"New or changed coins: {changed}",
            f"Authorized coin scope: {retained}",
            *(("BTC/XRP/SOL/TRX trading uses wrapped ERC-20 inventory on Ethereum; native wallets are separate." if "TRX" in desired else "BTC/XRP/SOL trading uses wrapped ERC-20 inventory on Ethereum; native wallets are separate." if "SOL" in desired else "BTC/XRP trading uses wrapped ERC-20 inventory on Ethereum; native wallets are separate.",)
              if any(x.get("settlement_policy")=="ETHEREUM_WRAPPED_SPOT_V1" for x in desired.values()) else ()),
            f"Nonce: {nonce}",
            f"Issued Unix: {issued}",
            f"Expires Unix: {expires}",
            "All amounts and fee recipients are fixed in the reviewed policy hash.",
        ))
        if len(challenge) > 3500:
            raise ValueError("Consent message exceeds EIP-191 review limit")
    return {
        "mode": "OFFLINE_OWNER_EVM_WALLET_POLICY_TRANSITION",
        "state": transition,
        "wallet": wallet.lower(),
        "wallet_id": wallet_id,
        "did": did,
        "shared_signer_id": executor_id,
        "previous_policy_sha256": prior_digest,
        "target_policy_sha256": target_digest,
        "coins_changed": sorted(added_or_changed),
        "coins_removed": sorted(removed),
        "selected_coins": sorted(desired),
        "owner_wallet_confirmation_required": transition in (
            "INITIAL_OWNER_ADD_SIGNER_REQUIRED",
            "OWNER_SIGNED_POLICY_UPDATE_INTENT_REQUIRED",
        ),
        "intent_eip191_required": challenge is not None,
        "intent_challenge": challenge,
        "intent_challenge_sha256": _sha(challenge) if challenge else None,
        "nonce": nonce if challenge else None,
        "expires_at_unix": now + _LIFETIME_SECONDS if challenge else None,
        "policy_created_in_privy": False,
        "signer_granted_or_changed": False,
        "live_session_started": False,
        "transaction_sent": False,
    }


def verify_wallet_policy_update_intent(
    *, plan: dict, expected_did: str, expected_wallet: str,
    expected_wallet_id: str, expected_previous_digest: str,
    expected_target_digest: str, signature: str, now: int,
) -> bool:
    """Cryptographic check ONLY; future DB must consume nonce exactly once.

    A caller MUST reload the saved, still-pending intent under a lock and
    recreate the exact policy from independently verified coin preferences.
    Returned True alone is never enough to authorize remote Privy updates.
    """
    if (type(plan) is not dict
            or plan.get("mode") != "OFFLINE_OWNER_EVM_WALLET_POLICY_TRANSITION"
            or plan.get("state") != "OWNER_SIGNED_POLICY_UPDATE_INTENT_REQUIRED"
            or plan.get("intent_eip191_required") is not True
            or type(expected_did) is not str
            or type(expected_wallet) is not str
            or not _ADDR.fullmatch(expected_wallet)
            or type(expected_wallet_id) is not str
            or type(expected_previous_digest) is not str
            or not _DIGEST.fullmatch(expected_previous_digest)
            or type(expected_target_digest) is not str
            or not _DIGEST.fullmatch(expected_target_digest)
            or type(now) is not int
            or type(plan.get("nonce")) is not str
            or not _NONCE.fullmatch(plan["nonce"])
            or type(plan.get("expires_at_unix")) is not int
            or not plan["expires_at_unix"] - _LIFETIME_SECONDS <= now < plan["expires_at_unix"]
            or plan.get("did") != expected_did
            or plan.get("wallet") != expected_wallet.lower()
            or plan.get("wallet_id") != expected_wallet_id
            or plan.get("previous_policy_sha256") != expected_previous_digest
            or plan.get("target_policy_sha256") != expected_target_digest):
        return False
    message = plan.get("intent_challenge")
    if (type(message) is not str or len(message) > 3500
            or _sha(message) != plan.get("intent_challenge_sha256")
            or f"Previous policy SHA256: {expected_previous_digest}" not in message
            or f"Requested policy SHA256: {expected_target_digest}" not in message
            or f"Wallet ID: {expected_wallet_id}" not in message
            or f"Privy User ID: {expected_did}" not in message
            or f"Wallet: {expected_wallet.lower()}" not in message):
        return False
    return verify_eoa_eip191_signature(
        message=message, signature=signature, wallet=expected_wallet,
    )
