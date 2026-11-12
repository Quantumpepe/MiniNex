"""Authenticated, read-only Privy wallet RECORD readiness for manual swaps.

This does not prove chain custody, wallet signing, deposits or transaction authority.
Only the server-fetched Privy user may supply linked account metadata. Never
accept client addresses; never expose addresses, wallet IDs or signing material.
"""
from __future__ import annotations

import re

from fastapi import HTTPException

from .manual_swap_routes import build_manual_swap_route
from .privy_wallet_verification import fetch_privy_user

_EVM = re.compile(r"^0x[0-9a-fA-F]{40}$")
_BTC = re.compile(r"^(?:bc1[ac-hj-np-z02-9]{39,74}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})$")
_BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_TRON = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")
_XRP = re.compile(r"^r[A-Za-z0-9]{24,34}$")
_DOGE = re.compile(r"^D[1-9A-HJ-NP-Za-km-z]{25,34}$")
_ADA = re.compile(r"^addr1[02-9ac-hj-np-z]{50,140}$")

# Chain-type matching is exact. EVM chains intentionally reuse the SAME
# Ethereum wallet address, but still require independent chain permissions.
_CHAIN_RECORDS = {
    "ethereum-mainnet": ("ethereum", _EVM),
    "bsc-mainnet": ("ethereum", _EVM),
    "polygon-pos-mainnet": ("ethereum", _EVM),
    "avalanche-c-mainnet": ("ethereum", _EVM),
    "bitcoin-mainnet": ("bitcoin-segwit", _BTC),
    "solana-mainnet": ("solana", _BASE58),
    "xrpl-mainnet": ("xrpl", _XRP),
    "tron-mainnet": ("tron", _TRON),
    "dogecoin-mainnet": ("dogecoin", _DOGE),
    "cardano-mainnet": ("cardano", _ADA),
}
_RECORD_STATES = frozenset({"PRESENT", "MISSING", "AMBIGUOUS", "INVALID_FORMAT"})


def _wallet_record_status(linked: list, network: str) -> str:
    """Wallet metadata presence only, NOT a checksum or spendability proof."""
    chain, address_pattern = _CHAIN_RECORDS[network]
    candidates = [
        row for row in linked
        if isinstance(row, dict) and row.get("type") == "wallet"
        and row.get("chain_type") == chain
        # Never count a MetaMask/external address as a Privy embedded signer.
        and (row.get("wallet_client_type") == "privy"
             or row.get("wallet_client") == "privy")
    ]
    if not candidates:
        return "MISSING"
    # Any duplicate record is ambiguous, even if addresses match, because an
    # executor could silently choose a different wallet ID or access grant.
    if len(candidates) != 1:
        return "AMBIGUOUS"
    address = candidates[0].get("address")
    if not isinstance(address, str) or not address_pattern.fullmatch(address):
        return "INVALID_FORMAT"
    return "PRESENT"


def inspect_manual_swap_wallet_records(user: object, did: str,
                                       source: str, target: str) -> dict:
    """Never use client-provided wallet data, and fail closed on bad identity."""
    route = build_manual_swap_route(source, target)
    if not isinstance(user, dict) or user.get("id") != did:
        raise HTTPException(503, "Privy wallet identity unavailable")
    linked = user.get("linked_accounts")
    if not isinstance(linked, list):
        raise HTTPException(503, "Privy linked wallet records unavailable")

    src = route["source"]
    dst = route["destination"]
    left = _wallet_record_status(linked, src["network"])
    right = _wallet_record_status(linked, dst["network"])
    assert left in _RECORD_STATES and right in _RECORD_STATES
    return {
        "mode": "MANUAL_SWAP_WALLET_READINESS_ONLY",
        "source": {"asset": source, "network": src["network"],
                   "record_status": left},
        "destination": {"asset": target, "network": dst["network"],
                        "record_status": right},
        "route_class": route["route_class"],
        "ada_in_progress": source == "ADA" or target == "ADA",
        "linked_wallet_records_checked": True,
        # An address in Privy's user metadata is NOT evidence of spendability.
        "ownership_proof_verified": False,
        "native_signer_verified": False,
        "network_fees_verified": False,
        "minimum_received_guaranteed": False,
        "refund_path_verified": False,
        "signing_authorized": False,
        "execution_ready": False,
        "can_submit": False,
    }


async def get_manual_swap_wallet_readiness(did: str, source: str,
                                            target: str) -> dict:
    # Reject unknown/identical symbols before spending a Privy API request.
    build_manual_swap_route(source, target)
    user = await fetch_privy_user(did)
    return inspect_manual_swap_wallet_records(user, did, source, target)
