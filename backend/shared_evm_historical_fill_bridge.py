"""Read-only trust bridge from durable owner fill to mined-time USD evidence.

Never substitute journal creation time for chain block time. A server-loaded
finalized fill must agree byte-for-byte in economic terms with its independently
reverified wallet transaction; CoinGecko prices must be near the real block.

Native BNB/POL/AVAX still need separate independently verified wrap/unwrap and
a complete wallet transfer audit. This bridge NEVER marks those proven, cannot
write any HWM ledger or send a fee, and cannot authorize a LIVE order.
"""
from __future__ import annotations

from decimal import Decimal
import re

import httpx
from fastapi import HTTPException

from .shared_evm_historical_fill_proof import read_owner_finalized_evm_swap_block
from .shared_evm_historical_usd import fetch_recent_historical_usd_observation
from .shared_evm_durable_usd_reference import historical_usd_for_verified_fill
from .shared_evm_native_cost_provenance import read_original_native_execution_cost
from .shared_evm_native_trace_provenance import read_original_native_conversion_trace
from .shared_evm_swap_receipts import _pair

_ASSETS = frozenset(("BNB", "POL", "AVAX", "LINK", "UNI"))
_NATIVE = frozenset(("BNB", "POL", "AVAX"))
_HASH = re.compile(r"^0x[0-9a-f]{64}$")


def join_owner_persisted_evm_fill(row: object, proof: object) -> dict:
    """Match all immutable on-chain amounts and wallet/order fields."""
    if (type(row) is not dict or type(proof) is not dict
            or row.get("asset") not in _ASSETS
            or proof.get("mode") != "INDEPENDENT_FINALIZED_EVM_EXECUTION"
            or proof.get("onchain_finalized") is not True):
        raise ValueError("Untrusted original order and chain record")
    route = _pair(row["asset"], row.get("stable"), row.get("side"))
    if (row.get("onchain_finalized") is not True
            or row.get("chain_id") != route["chain_id"]
            or row.get("network") != route["network"]
            or proof.get("chain_id") != route["chain_id"]
            or proof.get("network") != route["network"]
            or proof.get("asset") != row["asset"]
            or proof.get("stable") != row["stable"]
            or proof.get("side") != row["side"]
            or proof.get("wallet_address") != row.get("wallet_address")
            or proof.get("tx_hash") != row.get("tx_hash")
            or type(row.get("intent_key")) is not str
            or not 8 <= len(row["intent_key"]) <= 96
            or type(proof.get("block_unix")) is not int
            or proof["block_unix"] <= 0
            or type(proof.get("block_number")) is not int
            or proof["block_number"] <= 0
            or type(proof.get("transaction_index")) is not int
            or proof["transaction_index"] < 0):
        raise ValueError("Persisted order cannot be tied to mined execution")
    try:
        for persisted, mined in (
            ("spent_amount", "source_amount"),
            ("received_amount", "destination_amount"),
            ("network_fee_native", "network_fee_native"),
        ):
            a, b = Decimal(str(row[persisted])), Decimal(str(proof[mined]))
            if not a.is_finite() or not b.is_finite() or a != b:
                raise ValueError("Durable fill and actual tokens/gas differ")
            if a < 0 or (persisted != "network_fee_native" and a == 0):
                raise ValueError("Unusable original swap amount")
    except (KeyError, ArithmeticError, TypeError):
        raise ValueError("Immutable onchain fill mismatched") from None
    result = dict(proof)
    result["intent_key"] = row["intent_key"]
    result["external_inventory_reconciled"] = False
    result["historic_usd_priced"] = False
    result["native_cycle_complete"] = False
    result["fee_assessed"] = False
    return result


async def independently_price_stored_evm_fill(
    *,did: str, row: dict, client: httpx.AsyncClient,
    now: int | None = None, durable_rates: bool = False,
) -> dict:
    """One original DB fill -> mined receipt -> two historical rates.

    Full FIFO/custody, native conversion and HWM remain separate operations.
    Missing original prices or an old unsupported price window fail closed.
    """
    if (type(row) is not dict or row.get("asset") not in _ASSETS
            or type(client) is not httpx.AsyncClient
            or type(row.get("tx_hash")) is not str
            or not _HASH.fullmatch(row["tx_hash"])):
        raise ValueError("Untrusted original verified owner fill")
    proof = await read_owner_finalized_evm_swap_block(
        did=did, asset=row["asset"], stable=row["stable"],
        side=row["side"], tx_hash=row["tx_hash"])
    joined = join_owner_persisted_evm_fill(row, proof)
    block = joined["block_unix"]
    native_asset = row["asset"] if row["asset"] in _NATIVE else "ETH"
    if durable_rates:
        stable_rate = await historical_usd_for_verified_fill(
            did=did,proof=joined,quoted_asset=row["stable"],
            client=client,now=now)
        native_rate = await historical_usd_for_verified_fill(
            did=did,proof=joined,quoted_asset=native_asset,
            client=client,now=now)
    else:
        stable_rate = await fetch_recent_historical_usd_observation(
            asset=row["stable"],block_unix=block,client=client,now=now)
        native_rate = await fetch_recent_historical_usd_observation(
            asset=native_asset,block_unix=block,client=client,now=now)
    if (stable_rate.get("execution_block_unix") != block
            or native_rate.get("execution_block_unix") != block
            or stable_rate.get("asset") != row["stable"]
            or native_rate.get("asset") != native_asset):
        raise ValueError("Original source exchange rate proof mismatched")
    joined["stable_usd_proof"] = stable_rate
    joined["native_usd_proof"] = native_rate
    joined["historic_usd_priced"] = True
    # A native swap is NOT a settled coin trade until its original
    # confirmed wrap/unwrap ledger and on-chain costs are independently read.
    if row["asset"] in _NATIVE:
        joined["native_conversion"] = await read_original_native_execution_cost(
            did=did, fill=joined, client=client, now=now)
        # Independently inspect the actual native value transfer calls. The
        # original wrap/unwrap receipt alone cannot prove wallet custody.
        # This still does not audit every external wallet movement.
        joined["native_conversion_trace"] = (
            await read_original_native_conversion_trace(
                asset=row["asset"],side=row["side"],stable=row["stable"],
                fill=joined,conversion=joined["native_conversion"],client=client))
        if (joined["native_conversion_trace"].get(
                "original_finalized_internal_value_proven") is not True
                or joined["native_conversion_trace"].get(
                    "full_historical_wallet_activity_proven") is not False):
            raise ValueError("Original native wallet trace not independently proven")
        joined["native_cycle_complete"] = True
    else:
        joined["native_conversion"] = None
        joined["native_cycle_complete"] = True
    joined["external_inventory_reconciled"] = False
    joined["fee_assessed"] = False
    return joined
