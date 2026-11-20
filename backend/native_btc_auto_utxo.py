"""Automatic read-only original-owner BTC UTXO discovery and coin selection.

The candidate list from Esplora can be stale or forged: selection never
authorizes a spend. Every selected original outpoint MUST separately pass
native_btc_utxo_proof's exact funding transaction and unspent verification.
Largest-confirmed-first minimizes the number of paid BTC inputs and avoids
asking the user to manually pick outpoints in the final auto-trade workflow.
"""
from __future__ import annotations

import hashlib
import json
import re

import httpx
from fastapi import HTTPException

from .blockchain_preview import BTC_API
from .native_btc_thor_outputs import (
    _mainnet_p2wpkh_script, preview_btc_thor_original_outputs,
)
from .privy_wallet_verification import fetch_privy_user
from .wallet_balances import verified_addresses

_TXID = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_SATS = 21_000_000 * 100_000_000


def parse_original_btc_utxo_candidates(owner: object, raw: object) -> list[dict]:
    """Read Esplora /address/:address/utxo; no amount/trade-day caps."""
    script = _mainnet_p2wpkh_script(owner).hex()
    if type(raw) is not list:
        raise ValueError("Bitcoin owner UTXO discovery unavailable")
    candidates = []
    seen = set()
    for item in raw:
        if (type(item) is not dict or type(item.get("txid")) is not str
                or not _TXID.fullmatch(item["txid"])
                or type(item.get("vout")) is not int
                or not 0 <= item["vout"] <= 0xffffffff
                or type(item.get("value")) is not int
                or not 0 < item["value"] <= _MAX_SATS
                or type(item.get("status")) is not dict
                or type(item["status"].get("confirmed")) is not bool):
            raise ValueError("Malformed Bitcoin owner UTXO candidate")
        key = (item["txid"].lower(), item["vout"])
        if key in seen:
            raise ValueError("Duplicate Bitcoin owner UTXO candidate")
        seen.add(key)
        # Unconfirmed output is not claimed as confirmed spendable funding.
        # This is UTXO provenance, not a limit on trade capital or frequency.
        if item["status"]["confirmed"] is not True:
            continue
        candidates.append({
            "txid": key[0], "vout": key[1],
            "value_sats": item["value"],
            "script_pubkey_hex": script,
        })
    return sorted(candidates, key=lambda u: (
        -u["value_sats"], u["txid"], u["vout"],
    ))


def select_original_btc_utxos(
    *, quote: object, amount: str, now: int, candidates: object,
    fee_rate_sats_per_vbyte: int,
) -> dict:
    """Select just enough original confirmed inputs plus true change/fees.

    Successful preview proves only arithmetic; not current chain ownership.
    Every candidate's value/script must still match the on-chain outpoint.
    """
    if type(quote) is not dict or type(candidates) is not list:
        raise ValueError("Original Bitcoin quote and candidates required")
    owner_script = _mainnet_p2wpkh_script(quote.get("source_wallet")).hex()
    vault_script = _mainnet_p2wpkh_script(quote.get("inbound_vault")).hex()
    if (type(fee_rate_sats_per_vbyte) is not int
            or fee_rate_sats_per_vbyte <= 0):
        raise ValueError("Bitcoin live mining fee unavailable")
    if not candidates:
        raise ValueError("No confirmed original Bitcoin funding outpoints")
    # Input limits here are PSBT/fee serialization constraints only; not
    # investment, total capital, daily turnover or trade-count limits.
    chosen = []
    seen = set()
    preview = None
    for row in candidates:
        if (type(row) is not dict
                or type(row.get("txid")) is not str
                or not _TXID.fullmatch(row["txid"])
                or type(row.get("vout")) is not int
                or type(row.get("value_sats")) is not int
                or not 0 <= row["vout"] <= 0xffffffff
                or not 0 < row["value_sats"] <= _MAX_SATS
                or row.get("script_pubkey_hex") != owner_script):
            raise ValueError("Original Bitcoin selection candidate invalid")
        key = (row["txid"].lower(), row["vout"])
        if key in seen:
            raise ValueError("Duplicate original BTC source candidate")
        seen.add(key)
        chosen.append({
            "txid": key[0], "vout": key[1],
            "value_sats": row["value_sats"],
            "script_pubkey_hex": owner_script,
        })
        if len(chosen) > 256:
            raise ValueError("Bitcoin PSBT input batch exceeds safe transport size")
        try:
            preview = preview_btc_thor_original_outputs(
                quote, amount=amount, now=now,
                owner_script_hex=owner_script,
                vault_script_hex=vault_script,
                input_utxos=chosen,
                fee_rate_sats_per_vbyte=fee_rate_sats_per_vbyte,
            )
        except ValueError as error:
            # Do not swallow changes to original quoted source or memo:
            # absent funding is the only condition allowing more inputs.
            if (str(error) not in (
                "Cannot preserve original VIN0 change/refund wallet",
            )):
                raise
            continue
        break
    if preview is None:
        raise ValueError("Confirmed original Bitcoin UTXOs do not cover amount and fee")
    compact = [
        [row["txid"], row["vout"], row["value_sats"]]
        for row in chosen
    ]
    digest = hashlib.sha256(json.dumps([
        quote["source_wallet"], quote["inbound_vault"], amount,
        fee_rate_sats_per_vbyte, compact,
    ], separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()
    return {
        "mode": "BTC_ORIGINAL_OWNER_UTXO_SELECTION_READ_ONLY",
        "source_wallet": quote["source_wallet"],
        "input_utxos": chosen,
        "input_count": len(chosen),
        "input_total_sats": str(preview["total_input_sats"]),
        "owner_change_sats": str(preview["owner_change_sats"]),
        "estimated_network_fee_sats": str(preview["estimated_network_fee_sats"]),
        "selection_sha256": digest,
        "candidate_outpoints_independently_verified": False,
        "owner_privy_policy_verified": False,
        "wallet_signed": False,
        "source_transaction_sent": False,
        "execution_enabled": False, "can_submit": False,
    }


async def get_original_btc_utxo_candidates(did: str) -> dict:
    """Find all currently reported confirmed native BTC outpoints."""
    user = await fetch_privy_user(did)
    try:
        owner = verified_addresses(user, did)["BTC"]
        _mainnet_p2wpkh_script(owner)
    except (ValueError, KeyError, TypeError):
        raise HTTPException(409, "Original Privy Bitcoin address unavailable") from None
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(9), follow_redirects=False,
        ) as client:
            response = await client.get(
                f"{BTC_API}/address/{owner}/utxo",
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            if len(response.content) > 1_000_000:
                raise ValueError("Bitcoin UTXO response exceeded safe transport size")
            raw = response.json()
        candidates = parse_original_btc_utxo_candidates(owner, raw)
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        raise HTTPException(502, "Original Bitcoin unspent discovery failed") from None
    return {
        "mode": "BTC_ORIGINAL_OWNER_CANDIDATES_READ_ONLY",
        "source_wallet": owner,
        "candidates": candidates,
        "confirmed_candidate_count": len(candidates),
        "candidate_chain_provenance_checked": False,
        "private_wallet_signing_granted": False,
        "can_submit": False,
    }
