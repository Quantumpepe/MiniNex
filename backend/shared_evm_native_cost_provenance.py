"""On-chain original native conversion gas/provenance for EVM FIFO.

A completed trade-cycle DB link is only a *claim*: the original conversion
is rechecked against Privy wallet ownership, real finalized receipt, canonical
block, native quantity and native-token historical USD rate. No HWM write,
signer permission, or fees are initiated by this module.
"""
from __future__ import annotations

from decimal import Decimal
import os
import re

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .multicoin_evm_balances import _RPC
from .shared_evm_historical_usd import fetch_recent_historical_usd_observation
from .shared_evm_swap_receipts import _pair, _receipt_rpc
from .shared_native_evm_receipts import check_user_native_evm_conversion
from .storage import _db_url, _ensure_schema, _safe_did

_NATIVE = frozenset(("BNB", "POL", "AVAX"))
_HASH = re.compile(r"^0x[0-9a-f]{64}$")
_ADDR = re.compile(r"^0x[0-9a-f]{40}$")


def check_original_cycle_link(*, asset: str, side: str, stable: str,
                              fill: dict, cycle: object) -> str:
    """Require the existing durable original same-owner/chain cycle."""
    if (asset not in _NATIVE or type(fill) is not dict or type(cycle) is not dict):
        raise ValueError("Missing original native conversion cycle")
    route = _pair(asset, stable, side)
    native_hash = cycle.get("conversion_tx_hash")
    if (fill.get("mode") != "INDEPENDENT_FINALIZED_EVM_EXECUTION"
            or fill.get("onchain_finalized") is not True
            or fill.get("asset") != asset or fill.get("side") != side
            or fill.get("stable") != stable
            or fill.get("chain_id") != route["chain_id"]
            or fill.get("network") != route["network"]
            or cycle.get("asset") != asset or cycle.get("side") != side
            or cycle.get("stable") != stable
            or cycle.get("chain_id") != route["chain_id"]
            or cycle.get("wallet_address") != fill.get("wallet_address")
            or cycle.get("intent_key") != fill.get("intent_key")
            or cycle.get("swap_tx_hash") != fill.get("tx_hash")
            or type(native_hash) is not str or not _HASH.fullmatch(native_hash)
            or native_hash == fill["tx_hash"]):
        raise ValueError("Native chain ledger and finalized swap differ")
    return native_hash


def bind_original_finalized_native_cost(
    *, asset: str, side: str, stable: str, fill: dict,
    conversion: object, receipt: object, block: object, finalized: object,
    native_usd_proof: object,
) -> dict:
    """Construct exactly the immutable conversion-cost evidence for FIFO."""
    if (asset not in _NATIVE or type(conversion) is not dict
            or type(receipt) is not dict or type(block) is not dict
            or type(finalized) is not dict or type(native_usd_proof) is not dict):
        raise ValueError("No finalized owner native receipt")
    route = _pair(asset, stable, side)
    swap_block = fill.get("block_number")
    expected_op = "WITHDRAW" if side == "BUY" else "DEPOSIT"
    amount = (fill.get("destination_amount") if side == "BUY"
              else fill.get("source_amount"))
    native_hash = conversion.get("tx_hash")
    if (conversion.get("mode") != "SHARED_FINALIZED_NATIVE_EVM_CONVERSION"
            or conversion.get("state") != "FINALIZED_NATIVE_CONVERSION"
            or conversion.get("transaction_finalized") is not True
            or conversion.get("confirmed_native_conversion") is not True
            or conversion.get("asset") != asset
            or conversion.get("network") != route["network"]
            or conversion.get("chain_id") != route["chain_id"]
            or conversion.get("wallet_address") != fill.get("wallet_address")
            or conversion.get("operation") != expected_op
            or type(native_hash) is not str or not _HASH.fullmatch(native_hash)
            or type(swap_block) is not int or swap_block <= 0
            or receipt.get("transactionHash", "").lower() != native_hash
            or receipt.get("from", "").lower() != fill.get("wallet_address")
            or receipt.get("blockHash", "").lower() != block.get("hash", "").lower()):
        raise ValueError("Native conversion owner or original proof mismatch")
    try:
        conv_height = int(block["number"], 16)
        original_height = int(receipt["blockNumber"], 16)
        final_height = int(finalized["number"], 16)
        timestamp = int(block["timestamp"], 16)
        if (conv_height <= 0 or conv_height != original_height
                or conv_height != conversion.get("block_number")
                or conv_height > final_height or timestamp <= 0
                or (side == "BUY" and conv_height < swap_block)
                or (side == "SELL" and conv_height > swap_block)):
            raise ValueError("Native conversion chronology or finality invalid")
        qty = Decimal(str(amount))
        actual_qty = Decimal(str(conversion["amount_native"]))
        gas = Decimal(str(conversion["network_fee_native"]))
        if (not qty.is_finite() or qty <= 0 or actual_qty != qty
                or not gas.is_finite() or gas < 0 or gas >= Decimal("1e22")):
            raise ValueError("Native conversion quantity or paid fee differs")
        rate = Decimal(str(native_usd_proof["usd_price"]))
        sample = native_usd_proof["rate_observed_unix"]
        if (native_usd_proof.get("mode") != "HISTORICAL_EXECUTION_USD_REFERENCE_ONLY"
                or native_usd_proof.get("provider") != "COINGECKO_MARKET_CHART_RANGE"
                or native_usd_proof.get("asset") != asset
                or native_usd_proof.get("execution_block_unix") != timestamp
                or type(sample) is not int or abs(sample - timestamp) > 180
                or not rate.is_finite() or rate <= 0):
            raise ValueError("Unverifiable native historical gas USD rate")
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise ValueError("Native conversion historical proof incomplete") from None
    return {
        "mode": "INDEPENDENT_FINALIZED_NATIVE_EVM_CONVERSION_COST",
        "asset": asset, "side": side, "operation": expected_op,
        "chain_id": route["chain_id"],
        "wallet_address": fill["wallet_address"],
        "swap_tx_hash": fill["tx_hash"], "intent_key": fill["intent_key"],
        "tx_hash": native_hash, "block_number": conv_height,
        "block_unix": timestamp, "quantity_native": format(qty, "f"),
        "network_fee_native": format(gas, "f"),
        "native_usd_proof": native_usd_proof, "finalized": True,
        "fee_assessed": False,
    }


def _original_native_cycle(did: str, asset: str, key: str) -> dict:
    db = _db_url()
    _ensure_schema(db)
    with psycopg.connect(db, connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT asset,intent_key,chain_id,wallet_address,
                          swap_tx_hash,conversion_tx_hash,side,stable
                   FROM mininex_live_evm_completed_native_cycles
                   WHERE privy_did=%s AND asset=%s AND intent_key=%s""",
                (did, asset, key))
            row = cur.fetchone()
            if type(row) is not dict:
                raise HTTPException(409, "Original native cycle not settled")
            return row


async def read_original_native_execution_cost(
    *, did: str, fill: dict, client: httpx.AsyncClient,
    now: int | None = None,
) -> dict:
    """Never accept client-supplied cycle linkage or source time."""
    did = _safe_did(did)
    if (type(fill) is not dict or fill.get("asset") not in _NATIVE
            or type(client) is not httpx.AsyncClient):
        raise ValueError("Untrusted finalized native fill")
    asset, stable, side = fill["asset"], fill["stable"], fill["side"]
    cycle = _original_native_cycle(did, asset, fill["intent_key"])
    tx_hash = check_original_cycle_link(
        asset=asset, side=side, stable=stable, fill=fill, cycle=cycle)
    conversion = await check_user_native_evm_conversion(
        did, tx_hash, asset=asset,
        operation="WITHDRAW" if side == "BUY" else "DEPOSIT")
    if conversion.get("state") != "FINALIZED_NATIVE_CONVERSION":
        raise HTTPException(409, "Original native EVM conversion incomplete")
    chain, network, fallback, env = _RPC[asset]
    if chain != _pair(asset, stable, side)["chain_id"]:
        raise ValueError("Original conversion belongs to another chain")
    url = os.getenv(env, "").strip() or fallback
    if not url.startswith("https://") or any(c in url for c in " \r\n\t#@"):
        raise HTTPException(503, "Native historical chain RPC unavailable")
    try:
        chain_id = await _receipt_rpc(client, url, "eth_chainId", [], 61)
        if int(chain_id, 16) != chain:
            raise ValueError("Native historical RPC chain differs")
        receipt = await _receipt_rpc(
            client, url, "eth_getTransactionReceipt", [tx_hash], 62)
        if type(receipt) is not dict or not isinstance(receipt.get("blockNumber"), str):
            raise ValueError("Native transaction receipt not accessible")
        block = await _receipt_rpc(
            client, url, "eth_getBlockByNumber", [receipt["blockNumber"], False], 63)
        finalized = await _receipt_rpc(
            client, url, "eth_getBlockByNumber", ["finalized", False], 64)
        if type(block) is not dict or type(finalized) is not dict:
            raise ValueError("Native canonical finalized block absent")
        timestamp = int(block["timestamp"], 16)
        rate = await fetch_recent_historical_usd_observation(
            asset=asset, block_unix=timestamp, client=client, now=now)
        return bind_original_finalized_native_cost(
            asset=asset, side=side, stable=stable, fill=fill,
            conversion=conversion, receipt=receipt, block=block,
            finalized=finalized, native_usd_proof=rate)
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        raise HTTPException(502, "Original native conversion historical cost unverified") from None
