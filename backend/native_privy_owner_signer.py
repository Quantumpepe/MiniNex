"""Server-only readback of BTC/SOL/XRP Privy wallet ownership and delegation.

A connected native wallet, its 'delegated' flag, or an additional signer
reference is not sufficient to permit an automatic trade. This intentionally
does not create native signing policies or use a global policy ID as proof of
a specific user's spending authorization.

The returned response has NO wallet secret, policy ID, signer ID or wallet ID;
a future executor must re-read independently verified per-user policy content,
the current consent/budget revision and durable original send claim.
"""
from __future__ import annotations

from fastapi import HTTPException

from .native_btc_thor_outputs import _mainnet_p2wpkh_script
from .native_coin_balances import _valid_address, verified_native_addresses
from .privy_live_signer import _PRIVY_ID, fetch_privy_wallet_metadata
from .privy_wallet_verification import fetch_privy_user
from .storage import _safe_did
from .wallet_balances import verified_addresses

_CHAINS = {
    "BTC": "bitcoin-segwit",
    "SOL": "solana",
    "XRP": "xrpl",
}


def _original_privy_native_wallet(user: object, did: str, asset: str) -> dict:
    """Select exactly one original native wallet, not an external/imported one."""
    if asset not in _CHAINS or type(user) is not dict or user.get("id") != did:
        raise ValueError("Wrong original Privy native account")
    linked = user.get("linked_accounts")
    if type(linked) is not list:
        raise ValueError("Original Privy wallet list unavailable")
    candidates = []
    for account in linked:
        if (type(account) is not dict or account.get("type") != "wallet"
                or account.get("chain_type") != _CHAINS[asset]):
            continue
        if (account.get("wallet_client_type",
                        account.get("wallet_client")) != "privy"):
            continue
        address = account.get("address")
        wallet_id = account.get("id")
        if (type(address) is not str or type(wallet_id) is not str
                or not _PRIVY_ID.fullmatch(wallet_id)):
            raise ValueError("Native wallet missing verifiable owner ID")
        if asset == "BTC":
            _mainnet_p2wpkh_script(address)
        elif not _valid_address(asset, address):
            raise ValueError("Original native wallet address invalid")
        candidates.append({
            "wallet_id": wallet_id,
            "address": address,
            "delegated": account.get("delegated") is True,
        })
    if len(candidates) != 1:
        raise ValueError("Original native wallet must be unique")
    if asset == "BTC":
        canonical = verified_addresses(user, did)["BTC"]
    else:
        canonical = verified_native_addresses(user, did)[asset]
    if canonical != candidates[0]["address"]:
        raise ValueError("Current original native custody is ambiguous")
    return candidates[0]


def inspect_original_native_signer_metadata(
    *, did: str, asset: str, linked: object, remote: object,
) -> dict:
    """Compare independently observed original Privy user and wallet metadata.

    A positive result is still *read-only*. Policy reference does not prove
    policy content or exact user-signed trade permission.
    """
    original = _original_privy_native_wallet(linked, did, asset)
    if type(remote) is not dict:
        raise ValueError("Privy native wallet metadata missing")
    if (remote.get("id") != original["wallet_id"]
            or remote.get("chain_type") != _CHAINS[asset]
            or remote.get("address") != original["address"]
            or type(remote.get("owner")) is not dict
            or remote["owner"].get("user_id") != did):
        raise ValueError("Remote native wallet belongs to another owner")
    signers = remote.get("additional_signers")
    if type(signers) is not list:
        raise ValueError("Native additional signer metadata unavailable")
    signer_ids = set()
    has_scoped_policy = False
    for signer in signers:
        if type(signer) is not dict:
            raise ValueError("Malformed native Privy additional signer")
        identifier = signer.get("signer_id")
        policies = signer.get("override_policy_ids")
        if (type(identifier) is not str
                or not _PRIVY_ID.fullmatch(identifier)
                or identifier in signer_ids):
            raise ValueError("Ambiguous native Privy signer identity")
        signer_ids.add(identifier)
        if (type(policies) is not list or len(policies) != 1
                or type(policies[0]) is not str
                or not _PRIVY_ID.fullmatch(policies[0])):
            raise ValueError("Native Privy signer policy missing or unrestricted")
        has_scoped_policy = True
    return {
        "mode": "PRIVY_ORIGINAL_NATIVE_OWNER_READBACK_ONLY",
        "asset": asset,
        "network": {
            "BTC": "bitcoin-mainnet",
            "SOL": "solana-mainnet",
            "XRP": "xrpl-mainnet",
        }[asset],
        "original_wallet_link_verified": True,
        "original_wallet_remote_owner_verified": True,
        "wallet_delegation_flag_reported": original["delegated"],
        "additional_signer_reference_present": bool(signer_ids),
        "signer_policy_reference_present": has_scoped_policy,
        "exact_remote_policy_contents_verified": False,
        "per_user_owner_trade_consent_verified": False,
        "original_budget_revision_verified": False,
        "privy_transaction_signing_capability_verified": False,
        "original_order_claim_verified": False,
        "automated_signing_enabled": False,
        "live_trading_enabled": False,
        "execution_enabled": False,
        "can_submit": False,
    }


async def get_original_native_privy_signer_status(
    did: str, *, asset: str,
) -> dict:
    """Authenticated owner-specific status; metadata GET does not grant access."""
    did = _safe_did(did)
    if asset not in _CHAINS:
        raise HTTPException(422, "Unsupported original native wallet")
    base = {
        "mode": "PRIVY_ORIGINAL_NATIVE_OWNER_READBACK_ONLY",
        "asset": asset,
        "network": {
            "BTC": "bitcoin-mainnet",
            "SOL": "solana-mainnet",
            "XRP": "xrpl-mainnet",
        }[asset],
        "original_wallet_link_verified": False,
        "original_wallet_remote_owner_verified": False,
        "wallet_delegation_flag_reported": False,
        "additional_signer_reference_present": False,
        "signer_policy_reference_present": False,
        "exact_remote_policy_contents_verified": False,
        "per_user_owner_trade_consent_verified": False,
        "original_budget_revision_verified": False,
        "privy_transaction_signing_capability_verified": False,
        "original_order_claim_verified": False,
        "automated_signing_enabled": False,
        "live_trading_enabled": False,
        "execution_enabled": False,
        "can_submit": False,
    }
    user = await fetch_privy_user(did)
    try:
        original = _original_privy_native_wallet(user, did, asset)
    except (KeyError, ValueError, TypeError):
        return base
    base["original_wallet_link_verified"] = True
    base["wallet_delegation_flag_reported"] = original["delegated"]
    # A native wallet can exist without delegation. Treat that as not granted
    # and avoid an unnecessary remote signer read.
    if not original["delegated"]:
        return base
    try:
        metadata = await fetch_privy_wallet_metadata(original["wallet_id"])
        return inspect_original_native_signer_metadata(
            did=did, asset=asset, linked=user, remote=metadata,
        )
    except (ValueError, TypeError, KeyError, HTTPException):
        # Do not infer user authority from a failed independent remote read.
        return base
