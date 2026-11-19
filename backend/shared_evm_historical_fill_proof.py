"""Re-read a finalized original EVM swap at its canonical block timestamp.

Unlike the UI history created_at timestamp, this retrieves the *mined*
transaction index, block hash, and block Unix timestamp from an HTTPS chain
RPC, binding them to the independently verified Privy-owned swap. It is a
read-only source for later FIFO provenance; it cannot establish wallet
inventory, external transfers, or authorize/collect performance fees.
"""
from __future__ import annotations

from decimal import Decimal
import os
import re

import httpx
from fastapi import HTTPException

from .multicoin_evm_balances import _RPC
from .shared_evm_swap_receipts import _pair, _receipt_rpc, check_user_evm_swap

_HASH = re.compile(r"^0x[0-9a-f]{64}$")
_ADDR = re.compile(r"^0x[0-9a-f]{40}$")
_ASSETS = frozenset(("BNB", "POL", "AVAX", "LINK", "UNI", "ARB", "ARB"))


def verified_evm_swap_block_evidence(
    *, proof: object, tx_hash: str, asset: str, stable: str,
    side: str, receipt: object, block: object, finalized: object,
) -> dict:
    if (type(proof) is not dict or asset not in _ASSETS
            or type(receipt) is not dict or type(block) is not dict
            or type(finalized) is not dict
            or type(tx_hash) is not str or not _HASH.fullmatch(tx_hash)):
        raise ValueError("Missing original canonical finalized EVM swap")
    route = _pair(asset, stable, side)
    if (proof.get("mode") != "SHARED_EVM_FINALIZED_SWAP_VERIFICATION"
            or proof.get("state") != "FINALIZED_ERC20_SWAP_LEG"
            or proof.get("transaction_finalized") is not True
            or proof.get("swap_erc20_leg_verified") is not True
            or proof.get("tx_hash") != tx_hash
            or proof.get("asset") != asset or proof.get("stable") != stable
            or proof.get("side") != side
            or proof.get("chain_id") != route["chain_id"]
            or proof.get("network") != route["network"]
            or type(proof.get("wallet_address")) is not str
            or not _ADDR.fullmatch(proof["wallet_address"])
            or receipt.get("from", "").lower() != proof["wallet_address"]
            or receipt.get("transactionHash", "").lower() != tx_hash
            or receipt.get("blockHash", "").lower() != block.get("hash", "").lower()):
        raise ValueError("Original user-owned finalized receipt differs")
    try:
        n = int(receipt["blockNumber"], 16)
        index = int(receipt["transactionIndex"], 16)
        mined = int(block["number"], 16)
        final_height = int(finalized["number"], 16)
        timestamp = int(block["timestamp"], 16)
        if (n <= 0 or n != mined or n > final_height
                or n != proof.get("block_number")
                or index < 0 or index > 100000
                or timestamp <= 0
                or not _HASH.fullmatch(block["hash"].lower())
                or proof["tx_hash"] != tx_hash):
            raise ValueError("Noncanonical historical execution block")
        for field in ("source_spent_amount", "destination_received_amount"):
            raw = proof[field]
            value = Decimal(raw)
            if not value.is_finite() or value <= 0 or value >= Decimal("1e22"):
                raise ValueError("Unusable receipt-proven swap fill")
        gas = Decimal(proof["network_fee_native"])
        if not gas.is_finite() or gas < 0 or gas >= Decimal("1e22"):
            raise ValueError("Unusable original paid gas")
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise ValueError("Canonical EVM historical fill unavailable") from None
    return {
        "mode": "INDEPENDENT_FINALIZED_EVM_EXECUTION",
        "asset": asset, "stable": stable, "side": side,
        "chain_id": route["chain_id"], "network": route["network"],
        "wallet_address": proof["wallet_address"],
        "tx_hash": tx_hash,
        "block_hash": block["hash"].lower(),
        "block_number": n, "transaction_index": index,
        "block_unix": timestamp,
        "source_amount": proof["source_spent_amount"],
        "destination_amount": proof["destination_received_amount"],
        "network_fee_native": proof["network_fee_native"],
        "onchain_finalized": True,
        "history_complete": False,
        "external_inventory_reconciled": False,
        "fee_assessed": False,
    }


async def read_owner_finalized_evm_swap_block(
    *, did: str, asset: str, stable: str, side: str, tx_hash: str,
) -> dict:
    """No caller-supplied wallet, chain, blocktime, balance, or oracle quote."""
    if asset not in _ASSETS or type(tx_hash) is not str or not _HASH.fullmatch(tx_hash):
        raise ValueError("Unknown real EVM original fill")
    route = _pair(asset, stable, side)
    # Already rechecks Privy wallet ownership, calldata token amounts,
    # paid gas, canonical chain and finalized checkpoint.
    proof = await check_user_evm_swap(
        did, tx_hash, asset=asset, side=side, stable=stable)
    if proof.get("state") != "FINALIZED_ERC20_SWAP_LEG":
        raise HTTPException(409, "Original EVM swap is not finalized")
    chain, network, fallback, env = (
        _RPC[asset] if asset in ("BNB", "POL", "AVAX")
        else (_RPC["ARB_TOKENS"] if asset=="ARB" else _RPC["ETH_TOKENS"]))
    if chain != route["chain_id"] or network != route["network"]:
        raise ValueError("Wrong historical EVM chain source")
    url = os.getenv(env, "").strip() or fallback
    if not url.startswith("https://") or any(c in url for c in " \r\n\t#@"):
        raise HTTPException(503, "EVM history RPC not securely configured")
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(9), follow_redirects=False
        ) as client:
            chain_id = await _receipt_rpc(client, url, "eth_chainId", [], 51)
            if int(chain_id, 16) != chain:
                raise ValueError("Historical RPC chain mismatch")
            receipt = await _receipt_rpc(
                client, url, "eth_getTransactionReceipt", [tx_hash], 52)
            if type(receipt) is not dict or type(receipt.get("blockNumber")) is not str:
                raise ValueError("Original finalized receipt unavailable")
            block = await _receipt_rpc(
                client, url, "eth_getBlockByNumber",
                [receipt["blockNumber"], False], 53)
            finalized = await _receipt_rpc(
                client, url, "eth_getBlockByNumber", ["finalized", False], 54)
        return verified_evm_swap_block_evidence(
            proof=proof, tx_hash=tx_hash, asset=asset, stable=stable,
            side=side, receipt=receipt, block=block, finalized=finalized)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(502, "Canonical historical EVM fill not verified") from None
