"""Read-only ETH intent-to-receipt binding preflight for a future executor.

This deliberately cannot finalize an order. Persisted intent identifiers and
two receipt hashes alone do not prove the signer submitted those transactions.
The audit requires immutable server-side order metadata and rejects any
uncertain or conflicting association without writing to the live ledger.
"""
from __future__ import annotations

import hashlib
import json
import re

from .eth_native_cycle_comparison import compare_eth_native_cycle
from .live_orders import valid_order_key

_HASH = re.compile(r"^0x[0-9a-f]{64}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_UNCERTAIN = {"SENDING", "RECONCILIATION_REQUIRED", "REORG_PENDING"}
_ELIGIBLE = {"SUBMITTED", "CONFIRMED"}


def inspect_eth_order_receipt_binding(*, order: dict, swap: dict,
                                      conversion: dict) -> dict:
    """Pure advisory comparison; no authorization, settlement or order mutation.

    The order snapshot MUST be obtained by a future trusted internal caller
    under appropriate account isolation. It MUST NOT be accepted from HTTP.
    An actual binding still needs immutable wallet, nonce, calldata, transaction
    submission journal and verified chain chronology.
    """
    if type(order) is not dict:
        raise ValueError("Durable order snapshot required")
    key = valid_order_key(order.get("intent_key"))
    if order.get("asset") != "ETH" or order.get("chain_network") != "ethereum-mainnet":
        raise ValueError("Order network mismatch")
    if order.get("state") in _UNCERTAIN:
        raise ValueError("Uncertain broadcast requires independent reconciliation")
    if order.get("state") not in _ELIGIBLE:
        raise ValueError("Order is not a submitted on-chain intent")
    side = order.get("side")
    stable = order.get("stable")
    bound_swap = order.get("swap_tx_hash")
    bound_conversion = order.get("conversion_tx_hash")
    digest = order.get("quote_digest")
    if (type(bound_swap) is not str or not _HASH.fullmatch(bound_swap)
            or type(bound_conversion) is not str
            or not _HASH.fullmatch(bound_conversion)
            or bound_swap == bound_conversion
            or type(digest) is not str or not _DIGEST.fullmatch(digest)):
        raise ValueError("Missing immutable two-leg order reference")
    if type(swap) is not dict or type(conversion) is not dict:
        raise ValueError("Independent receipt evidence required")
    if (swap.get("tx_hash") != bound_swap
            or conversion.get("tx_hash") != bound_conversion):
        raise ValueError("Receipt is not recorded under this order")
    comparison = compare_eth_native_cycle(
        swap=swap, conversion=conversion, side=side, stable=stable)
    material = json.dumps(
        [key, side, stable, bound_swap, bound_conversion, digest],
        separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")
    return {
        "mode": "ETH_ORDER_RECEIPT_BINDING_PREFLIGHT_ONLY",
        "intent_key": key,
        "asset": "ETH",
        "network": "ethereum-mainnet",
        "order_state": order["state"],
        "reference_digest": hashlib.sha256(material).hexdigest(),
        "matched_recorded_hashes": True,
        "independent_receipts_match": comparison["weth_quantities_match"],
        "network_fee_eth": comparison["total_network_fee_eth"],
        "needs_immutable_submission_journal": True,
        "wallet_and_nonce_proven": False,
        "transaction_order_proven": False,
        "authorized_signer_proven": False,
        "same_order_cryptographically_proven": False,
        "ledger_settled": False,
        "portfolio_credit_allowed": False,
        "performance_fee_allowed": False,
        "execution_enabled": False,
        "can_submit": False,
    }
