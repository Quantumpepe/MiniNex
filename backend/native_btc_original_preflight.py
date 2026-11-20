"""Original-owner BTC -> stablecoin THORChain unsigned source preflight.

Owner-bound private THORChain quote, original Privy BTC funding outpoints and
live Esplora mining fees must all be refreshed before a PSBT can be built.
Internal read-only function: it never requests Privy authorization, signs,
records an intent, broadcasts, or treats a preflight as a completed trade.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
import json
import time

import httpx
from fastapi import HTTPException

from .blockchain_preview import BTC_API
from .native_btc_thor_outputs import _mainnet_p2wpkh_script
from .native_btc_thor_psbt import compile_original_unsigned_btc_thor_psbt
from .native_btc_utxo_proof import get_original_btc_utxo_evidence
from .native_thor_swap_quote import get_private_native_thornode_terms
from .storage import _safe_did


def validate_original_btc_network_fee_estimate(raw: object) -> int:
    """Return upward-rounded sat/vB from Esplora's fresh next-block estimate."""
    if type(raw) is not dict:
        raise ValueError("Bitcoin fee rate estimate missing")
    fee = raw.get("1")
    if type(fee) not in (str, int, float) or len(str(fee)) > 24:
        raise ValueError("Bitcoin next-block fee rate malformed")
    try:
        number = Decimal(str(fee))
    except (InvalidOperation, ValueError):
        raise ValueError("Bitcoin next-block fee rate unavailable") from None
    if not number.is_finite() or not 0 < number <= 1_000_000:
        raise ValueError("Bitcoin next-block fee rate unavailable")
    return int(number.to_integral_value(rounding=ROUND_CEILING))


async def _get_fresh_bitcoin_fee_rate() -> int:
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8), follow_redirects=False,
        ) as client:
            response = await client.get(
                BTC_API + "/fee-estimates",
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            if len(response.content) > 10_000:
                raise ValueError("Bitcoin fee estimate response oversized")
            return validate_original_btc_network_fee_estimate(response.json())
    except (httpx.HTTPError, ValueError, TypeError):
        raise HTTPException(502, "Fresh Bitcoin miner-fee estimate unavailable") from None


async def prepare_original_btc_unsigned_psbt(
    did: str, *, stable: str, amount: str, input_utxos: object,
) -> dict:
    """Owner-only internal readback; never expose as spend authorization."""
    did = _safe_did(did)
    if stable not in ("USDC", "USDT"):
        raise HTTPException(422, "Original Bitcoin settlement stable unsupported")
    if type(input_utxos) is not list or not input_utxos:
        raise HTTPException(422, "Original Bitcoin source UTXOs required")
    # Quote and chain evidence are obtained independently for the same Privy DID.
    terms = await get_private_native_thornode_terms(
        did, asset="BTC", side="SELL", stable=stable, amount=amount,
    )
    inputs = await get_original_btc_utxo_evidence(did, inputs=input_utxos)
    if (terms.get("source_wallet") != inputs.get("source_wallet")
            or terms.get("source_asset") != "BTC"
            or terms.get("destination_asset") != stable
            or terms.get("amount") != amount
            or terms.get("original_vault_verified") is not True
            or inputs.get("current_explorer_unspent_reported") is not True
            or inputs.get("funding_transactions_checked") is not True
            or inputs.get("bitcoin_spv_proof_verified") is not False):
        raise HTTPException(409, "BTC quote and original owner UTXOs do not agree")
    fee_rate = await _get_fresh_bitcoin_fee_rate()
    # Reevaluate quote TTL AFTER all external network requests have finished.
    now = int(time.time())
    try:
        owner_script = _mainnet_p2wpkh_script(terms["source_wallet"]).hex()
        vault_script = _mainnet_p2wpkh_script(terms["inbound_vault"]).hex()
        source_utxos = [
            {"txid": row["txid"], "vout": row["vout"],
             "value_sats": row["value_sats"],
             "script_pubkey_hex": row["script_pubkey_hex"]}
            for row in inputs["inputs"]
        ]
        psbt = compile_original_unsigned_btc_thor_psbt(
            terms, amount=amount, now=now, input_utxos=source_utxos,
            owner_script_hex=owner_script, vault_script_hex=vault_script,
            fee_rate_sats_per_vbyte=fee_rate,
        )
    except (ValueError, TypeError, KeyError, OverflowError):
        raise HTTPException(409, "BTC quote expired or source PSBT preflight failed") from None
    if (psbt["source_wallet"] != inputs["source_wallet"]
            or psbt["input_count"] != inputs["input_count"]
            or psbt["psbt_created"] is not True):
        raise HTTPException(409, "Original Bitcoin source PSBT identity changed")
    digest = hashlib.sha256(json.dumps([
        did, psbt["psbt_sha256"], inputs["proof_sha256"],
        psbt["original_output_digest_sha256"], now,
    ], separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()
    return {
        "mode": "ORIGINAL_BTC_PSVT_OWNER_PREFLIGHT_READ_ONLY",
        "asset": "BTC", "side": "SELL", "stable": stable,
        "source_wallet": psbt["source_wallet"],
        "destination_wallet": psbt["destination_wallet"],
        "source_amount_sats": psbt["source_amount_sats"],
        "unsigned_psbt": psbt,
        "explorer_utxo_proof_sha256": inputs["proof_sha256"],
        "original_preflight_sha256": digest,
        "fee_rate_sats_per_vbyte": fee_rate,
        "btc_utxo_tip_height": inputs["tip_height"],
        "read_only_private_quote_checked": True,
        "current_esplora_unspent_reported": True,
        "bitcoin_spv_proof_verified": False,
        "bitcoin_utxo_atomic_lock_acquired": False,
        "fresh_privy_owner_permission_granted": False,
        "original_order_send_claim_acquired": False,
        "wallet_signed": False, "source_transaction_sent": False,
        "execution_enabled": False, "can_submit": False,
    }
