"""Read-only Esplora confirmation of original Privy-owned BTC UTXOs.

Live source transaction construction must never trust user-provided txid,
value, script or a locally cached unspent bit. This module fetches the actual
funding transaction and outspend for every input from Bitcoin mainnet. A
single Esplora source is not a cryptographic SPV proof or an atomic UTXO lock;
a signer must re-check the inputs immediately before any approved broadcast.

No PSBT signing, Privy spending action, broadcast or ledger write here.
"""
from __future__ import annotations

import hashlib
import json
import re

import httpx
from fastapi import HTTPException

from .blockchain_preview import BTC_API
from .live_btc_receipts import _get_tip, _get_tx
from .native_btc_thor_outputs import _mainnet_p2wpkh_script
from .privy_wallet_verification import fetch_privy_user
from .wallet_balances import verified_addresses

_TXID = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_SATS = 21_000_000 * 100_000_000


def inspect_original_btc_utxo_set(
    *, owner: str, inputs: object, funding_txs: object,
    outspends: object, tip_height: int,
) -> dict:
    """Fail closed if any supplied original-input claim differs from Esplora.

    Evidence is time-local, explorer-reported and cannot itself grant a
    signature or live send. Each outspend must be explicitly false.
    """
    owner_script = _mainnet_p2wpkh_script(owner)
    if (type(inputs) is not list or not inputs
            or type(funding_txs) is not dict
            or type(outspends) is not dict
            or type(tip_height) is not int
            or not 1 <= tip_height <= 10_000_000):
        raise ValueError("Original Bitcoin source-chain evidence unavailable")
    checked = []
    seen = set()
    for claim in inputs:
        if (type(claim) is not dict
                or type(claim.get("txid")) is not str
                or not _TXID.fullmatch(claim["txid"])
                or type(claim.get("vout")) is not int
                or not 0 <= claim["vout"] <= 0xffffffff
                or type(claim.get("value_sats")) is not int
                or not 0 < claim["value_sats"] <= _MAX_SATS
                or type(claim.get("script_pubkey_hex")) is not str
                or claim["script_pubkey_hex"].lower() != owner_script.hex()):
            raise ValueError("Bitcoin input is not bound to the original owner")
        txid = claim["txid"].lower()
        vout_index = claim["vout"]
        identity = f"{txid}:{vout_index}"
        if identity in seen:
            raise ValueError("Duplicate Bitcoin source input")
        seen.add(identity)
        tx = funding_txs.get(txid)
        spend = outspends.get(identity)
        if (type(tx) is not dict or type(spend) is not dict
                or str(tx.get("txid", "")).lower() != txid
                or spend.get("spent") is not False):
            raise ValueError("Bitcoin UTXO absent, spent, or mismatched")
        vouts = tx.get("vout")
        if type(vouts) is not list or not 0 <= vout_index < len(vouts):
            raise ValueError("Original Bitcoin transaction output missing")
        source = vouts[vout_index]
        if (type(source) is not dict
                or type(source.get("value")) is not int
                or source["value"] != claim["value_sats"]
                or type(source.get("scriptpubkey")) is not str
                or source["scriptpubkey"].lower() != owner_script.hex()
                or source.get("scriptpubkey_address") != owner):
            raise ValueError("Bitcoin funding outpoint not owned by Privy wallet")
        state = tx.get("status")
        if (type(state) is not dict or state.get("confirmed") is not True
                or type(state.get("block_height")) is not int
                or not 1 <= state["block_height"] <= tip_height
                or type(state.get("block_hash")) is not str
                or not _TXID.fullmatch(state["block_hash"])):
            raise ValueError("Bitcoin funding UTXO not confirmed by explorer")
        checked.append({
            "txid": txid, "vout": vout_index,
            "value_sats": claim["value_sats"],
            "script_pubkey_hex": owner_script.hex(),
            "funding_confirmations": tip_height - state["block_height"] + 1,
            "funding_block_hash": state["block_hash"].lower(),
        })
    if sum(row["value_sats"] for row in checked) > _MAX_SATS:
        raise ValueError("Bitcoin source UTXO total exceeds valid supply")
    canonical = {
        "network": "bitcoin-mainnet", "source_wallet": owner,
        "tip_height": tip_height,
        "original_inputs": checked,
    }
    digest = hashlib.sha256(json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    return {
        "mode": "BTC_ORIGINAL_OWNER_UTXO_ESPLORA_READ_ONLY",
        "asset": "BTC", "source_wallet": owner,
        "network": "bitcoin-mainnet", "tip_height": tip_height,
        "input_count": len(checked),
        "inputs": checked,
        "input_total_sats": str(sum(row["value_sats"] for row in checked)),
        "current_explorer_unspent_reported": True,
        "funding_transactions_checked": True,
        "original_owner_output_script_checked": True,
        "proof_sha256": digest,
        "bitcoin_spv_proof_verified": False,
        "atomic_utxo_lock_acquired": False,
        "fresh_privy_signer_policy_verified": False,
        "original_order_claim_verified": False,
        "wallet_signed": False, "source_transaction_sent": False,
        "execution_enabled": False, "can_submit": False,
    }


async def get_original_btc_utxo_evidence(
    did: str, *, inputs: object,
) -> dict:
    """Obtain current nonspendable chain proofs for uniquely linked BTC owner."""
    if type(inputs) is not list or not inputs or len(inputs) > 256:
        raise HTTPException(422, "Bitcoin source UTXO input batch invalid")
    user = await fetch_privy_user(did)
    try:
        owner = verified_addresses(user, did)["BTC"]
        _mainnet_p2wpkh_script(owner)
    except (KeyError, ValueError, TypeError):
        raise HTTPException(409, "Original Privy Bitcoin source unavailable") from None
    # Validate shape and uniqueness BEFORE making any remote requests.
    ids = set()
    for item in inputs:
        if (type(item) is not dict
                or type(item.get("txid")) is not str
                or not _TXID.fullmatch(item["txid"])
                or type(item.get("vout")) is not int
                or not 0 <= item["vout"] <= 0xffffffff):
            raise HTTPException(422, "Malformed original Bitcoin outpoint")
        key = (item["txid"].lower(), item["vout"])
        if key in ids:
            raise HTTPException(422, "Duplicate original Bitcoin input")
        ids.add(key)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(9), follow_redirects=False,
        ) as client:
            tip = await _get_tip(client)
            transactions = {}
            spends = {}
            for txid, vout in sorted(ids):
                if txid not in transactions:
                    transactions[txid] = await _get_tx(client, txid)
                response = await client.get(
                    f"{BTC_API}/tx/{txid}/outspend/{vout}",
                    headers={"Accept": "application/json"},
                )
                response.raise_for_status()
                if len(response.content) > 20_000:
                    raise ValueError("Oversized Bitcoin unspent status")
                spends[f"{txid}:{vout}"] = response.json()
        return inspect_original_btc_utxo_set(
            owner=owner, inputs=inputs, funding_txs=transactions,
            outspends=spends, tip_height=tip,
        )
    except (httpx.HTTPError, ValueError, KeyError, TypeError, OverflowError):
        raise HTTPException(
            502, "Bitcoin funding outputs could not be verified"
        ) from None
