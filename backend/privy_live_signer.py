"""Read-only verification of an ETH wallet's Privy additional signer and policy.

This module NEVER adds/delegates signers, signs, or sends an order.
A login JWT, wallet address, or `delegated` flag alone is NOT permission to trade.
An external signer must be independently verified against Privy's wallet
metadata with the precise key quorum and restricted policy IDs. Fail closed.
"""
from __future__ import annotations

import logging
import os
import re

import httpx
from fastapi import HTTPException

from .privy_wallet_verification import APP_ID, fetch_privy_user
from .storage import _safe_did

LOG = logging.getLogger("mininex.privy_eth_signer")
_PRIVY_ID = re.compile(r"^[a-zA-Z0-9_-]{6,160}$")
_ETH_ADDR = re.compile(r"^0x[a-fA-F0-9]{40}$")
_PRIVY_WALLET_API = "https://api.privy.io/v1/wallets/"


def signer_configuration() -> dict[str, str] | None:
    """Public quorum/policy IDs may be configured; private signer key stays elsewhere."""
    quorum = os.environ.get("MININEX_PRIVY_ETH_SIGNER_ID", "").strip()
    policy = os.environ.get("MININEX_PRIVY_ETH_POLICY_ID", "").strip()
    if not _PRIVY_ID.fullmatch(quorum) or not _PRIVY_ID.fullmatch(policy):
        return None
    if quorum == policy:
        return None
    return {"signer_id": quorum, "policy_id": policy}


def _owned_eth_wallet(user: object, did: str) -> dict:
    """Only the uniquely server-verified, delegated Privy EVM wallet is relevant."""
    if not isinstance(user, dict) or user.get("id") != did:
        raise ValueError("Invalid Privy user")
    linked = user.get("linked_accounts")
    if not isinstance(linked, list):
        raise ValueError("No verified wallets")
    candidates = []
    for account in linked:
        if not isinstance(account, dict):
            continue
        if account.get("type") != "wallet" or account.get("chain_type") != "ethereum":
            continue
        if account.get("wallet_client_type") != "privy" and account.get("wallet_client") != "privy":
            continue
        address = account.get("address")
        wallet_id = account.get("id")
        if not isinstance(address, str) or not _ETH_ADDR.fullmatch(address):
            raise ValueError("Invalid embedded wallet address")
        if not isinstance(wallet_id, str) or not _PRIVY_ID.fullmatch(wallet_id):
            raise ValueError("Embedded wallet not provisioned for server-side verification")
        candidates.append({
            "wallet_id": wallet_id,
            "address": address.lower(),
            "delegated": account.get("delegated") is True,
        })
    if len(candidates) != 1:
        raise ValueError("Expected exactly one owned Privy Ethereum wallet")
    return candidates[0]


def _scoped_signer(wallet: object, *, did: str, address: str,
                   config: dict[str, str]) -> bool:
    """Do not infer ownership/policy from a frontend or user metadata field."""
    if not isinstance(wallet, dict):
        return False
    if wallet.get("chain_type") != "ethereum":
        return False
    if str(wallet.get("address", "")).lower() != address.lower():
        return False
    owner = wallet.get("owner")
    if not isinstance(owner, dict) or owner.get("user_id") != did:
        return False
    signers = wallet.get("additional_signers")
    if not isinstance(signers, list):
        return False
    matches = [
        signer for signer in signers
        if isinstance(signer, dict) and signer.get("signer_id") == config["signer_id"]
    ]
    if len(matches) != 1:
        return False
    match = matches[0]
    # Privy docs: one optional override policy per additional signer.
    # Missing or empty policy would be unrestricted: NEVER accept.
    policies = match.get("override_policy_ids")
    if not isinstance(policies, list) or policies != [config["policy_id"]]:
        return False
    return True


async def fetch_privy_wallet_metadata(wallet_id: str) -> dict:
    """GET wallet by server-observed ID; never accepts a client-provided ID."""
    if not isinstance(wallet_id, str) or not _PRIVY_ID.fullmatch(wallet_id):
        raise HTTPException(503, "Cannot verify Privy wallet")
    secret = os.environ.get("MININEX_PRIVY_APP_SECRET", "").strip()
    if not secret:
        raise HTTPException(503, "Privy server verification not configured")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(9.0), follow_redirects=False) as client:
            response = await client.get(
                _PRIVY_WALLET_API + wallet_id,
                auth=(APP_ID, secret),
                headers={"privy-app-id": APP_ID, "Accept": "application/json"},
            )
            response.raise_for_status()
            if len(response.content) > 128_000:
                raise ValueError("Oversized wallet metadata")
            data = response.json()
        if not isinstance(data, dict) or data.get("id") != wallet_id:
            raise ValueError("Wallet metadata identity mismatch")
        return data
    except (httpx.HTTPError, ValueError, TypeError):
        LOG.warning("Privy wallet signer metadata verification unavailable")
        raise HTTPException(503, "Cannot independently verify Privy signer") from None


async def get_eth_signer_status(did: str) -> dict:
    """Account-specific read-only grant status.

    `configured` says only that the operator set public quorum + policy IDs.
    `signer_verified` is observed directly from the Privy server, NOT from
    app/user-controlled values. Even if true, execution is NOT authorized.
    """
    _safe_did(did)
    config = signer_configuration()
    result = {
        "asset": "ETH",
        "chain_id": 1,
        "mode": "PRIVY_SIGNER_VERIFICATION_ONLY",
        "signer_policy_configured": config is not None,
        "wallet_delegated": False,
        "signer_verified": False,
        "policy_verified": False,
        "trading_approved": False,
        "automated_signing_enabled": False,
        "execution_enabled": False,
        "revocation_available_in_wallet": True,
    }
    if config is None:
        return result
    user = await fetch_privy_user(did)
    try:
        owned = _owned_eth_wallet(user, did)
    except ValueError:
        return result
    if not owned["delegated"]:
        return result
    result["wallet_delegated"] = True
    wallet = await fetch_privy_wallet_metadata(owned["wallet_id"])
    # Both exact Privy key quorum and exact restrictive override policy must match.
    scoped = _scoped_signer(wallet, did=did, address=owned["address"], config=config)
    result["signer_verified"] = scoped
    result["policy_verified"] = scoped
    return result
