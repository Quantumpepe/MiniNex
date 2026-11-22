"""Exact USD FIFO arithmetic for independently audited EVM trading history.

Pure calculation only: the caller MUST independently prove the COMPLETE wallet
transfer history, finalized original swap/conversion receipts, and each block-
time USD price. Neither a database row nor a client-supplied boolean is proof.
No signer, SQL mutation, HWM assessment, or treasury transfer exists here.
"""
from __future__ import annotations

from collections import deque
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, localcontext
import re

from .shared_evm_net_costs import calculate_verified_evm_net_costs
from .shared_evm_swap_receipts import _pair

_ASSETS = frozenset(("ETH", "BNB", "POL", "AVAX", "LINK", "QNT", "UNI", "ARB"))
_NATIVE = frozenset(("ETH", "BNB", "POL", "AVAX"))
_HASH = re.compile(r"^0x[0-9a-f]{64}$")
_ADDR = re.compile(r"^0x[0-9a-f]{40}$")
_CENT_12 = Decimal("0.000000000001")


def _decimal(value: object, *, allow_zero: bool = False) -> Decimal:
    if type(value) is not str or not value or len(value) > 70 or "e" in value.lower():
        raise ValueError("An exact on-chain amount is required")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("Invalid exact on-chain amount") from None
    if (not number.is_finite() or number < 0 or (number == 0 and not allow_zero)
            or number >= Decimal("1e22") or number.as_tuple().exponent < -18):
        raise ValueError("Unusable verified quantity")
    return number


def _historical_price(proof: object, asset: str, block_unix: int) -> Decimal:
    if (type(proof) is not dict
            or proof.get("mode") != "HISTORICAL_EXECUTION_USD_REFERENCE_ONLY"
            or proof.get("provider") != "COINGECKO_MARKET_CHART_RANGE"
            or proof.get("asset") != asset
            or proof.get("execution_block_unix") != block_unix
            or type(proof.get("rate_observed_unix")) is not int
            or abs(proof["rate_observed_unix"] - block_unix) > 180):
        raise ValueError("No block-time historical USD price proof")
    number = _decimal(proof.get("usd_price"))
    if asset in ("USDC", "USDT") and not Decimal(".95") <= number <= Decimal("1.05"):
        raise ValueError("Stablecoin depeg cannot be charged automatically")
    return number


def _conversion_fee(row: dict, asset: str, side: str,
                    swap_block: int, amount: Decimal, seen: set[str]) -> Decimal:
    conversion = row.get("native_conversion")
    if asset not in _NATIVE:
        if conversion is not None:
            raise ValueError("Unexpected native conversion on ERC20 trade")
        return Decimal(0)
    if (type(conversion) is not dict or conversion.get("finalized") is not True
            or conversion.get("mode") != "INDEPENDENT_FINALIZED_NATIVE_EVM_CONVERSION_COST"
            or conversion.get("asset") != asset
            or conversion.get("side") != side
            or conversion.get("chain_id") != row.get("chain_id")
            or conversion.get("wallet_address") != row.get("wallet_address")
            or conversion.get("intent_key") != row.get("intent_key")
            or conversion.get("swap_tx_hash") != row.get("tx_hash")
            or conversion.get("operation") != (
                "WITHDRAW" if side == "BUY" else "DEPOSIT")
            or type(conversion.get("tx_hash")) is not str
            or not _HASH.fullmatch(conversion["tx_hash"])
            or conversion["tx_hash"] in seen
            or type(conversion.get("block_number")) is not int
            or type(conversion.get("block_unix")) is not int
            or conversion["block_number"] <= 0 or conversion["block_unix"] <= 0
            or (side == "BUY" and conversion["block_number"] < swap_block)
            or (side == "SELL" and conversion["block_number"] > swap_block)
            or _decimal(conversion.get("quantity_native")) != amount):
        raise ValueError("Native conversion does not match original trade")
    seen.add(conversion["tx_hash"])
    return (_decimal(conversion.get("network_fee_native"), allow_zero=True)
            * _historical_price(
                conversion.get("native_usd_proof"), asset, conversion["block_unix"]))


def _quantized(value: Decimal, *, cost: bool = False) -> str:
    # Never overcharge: round proceeds down and costs up to persisted precision.
    return format(value.quantize(
        _CENT_12, rounding=ROUND_CEILING if cost else ROUND_FLOOR), "f")


def reconcile_verified_evm_fifo_net(evidence: object) -> dict:
    """Calculate SELL net USD using shared FIFO lots across USDC and USDT.

    Existing BUY lots carry acquisition price, buy-swap gas and any native
    unwrap gas through partial closes. SELL gas and native wrap gas are charged
    once at the actual closing block. Each SELL is returned independently.
    This DOES NOT grant permission to persist or collect a performance fee.
    """
    if type(evidence) is not dict:
        raise ValueError("Full verified wallet history required")
    asset, wallet = evidence.get("asset"), evidence.get("wallet_address")
    if (asset not in _ASSETS or type(wallet) is not str
            or not _ADDR.fullmatch(wallet)
            or evidence.get("mode") != "SERVER_VERIFIED_EVM_CUSTODY_HISTORY"
            or evidence.get("wallet_ownership_verified") is not True
            or evidence.get("history_not_truncated") is not True
            or evidence.get("contains_paper") is not False
            or type(evidence.get("fills")) is not list
            or not evidence["fills"]):
        raise ValueError("Incomplete finalized wallet history")
    audit = evidence.get("asset_transfer_audit")
    if (type(audit) is not dict
            or audit.get("mode") != "INDEPENDENT_ONCHAIN_ASSET_TRANSFER_AUDIT"
            or audit.get("zero_initial_asset_balance_verified") is not True
            or audit.get("all_asset_movements_match_verified_orders") is not True
            or audit.get("external_inventory_reconciled") is not True
            or audit.get("no_other_execution_costs_verified") is not True
            or type(audit.get("start_block")) is not int
            or type(audit.get("end_block")) is not int
            or not 0 < audit["start_block"] <= audit["end_block"]):
        raise ValueError("Wallet transfers and external inventory not reconciled")
    with localcontext() as ctx:
        ctx.prec = 65
        lots = deque()
        seen = set()
        seen_intents = set()
        position = (-1, -1)
        closes = []
        for item in evidence["fills"]:
            if type(item) is not dict:
                raise ValueError("Noncanonical EVM trade evidence")
            side, stable = item.get("side"), item.get("stable")
            pair = _pair(asset, stable, side)
            block, tx_index, block_unix = (
                item.get("block_number"), item.get("transaction_index"),
                item.get("block_unix"))
            tx_hash = item.get("tx_hash")
            if (item.get("mode") != "INDEPENDENT_FINALIZED_EVM_EXECUTION"
                    or item.get("onchain_finalized") is not True
                    or item.get("asset") != asset or item.get("wallet_address") != wallet
                    or item.get("chain_id") != pair["chain_id"]
                    or item.get("network") != pair["network"]
                    or type(item.get("intent_key")) is not str
                    or not 8 <= len(item["intent_key"]) <= 96
                    or type(tx_hash) is not str or not _HASH.fullmatch(tx_hash)
                    or tx_hash in seen
                    or item["intent_key"] in seen_intents
                    or type(block) is not int or block <= 0
                    or type(tx_index) is not int or tx_index < 0
                    or type(block_unix) is not int or block_unix <= 0
                    or (block, tx_index) <= position
                    or not audit["start_block"] <= block <= audit["end_block"]):
                raise ValueError("Duplicate/out-of-order/unverified trade")
            if item.get("native_cycle_complete") is not True:
                raise ValueError("Native EVM order not economically complete")
            seen.add(tx_hash)
            seen_intents.add(item["intent_key"])
            position = (block, tx_index)
            source = _decimal(item.get("source_amount"))
            destination = _decimal(item.get("destination_amount"))
            gas = _decimal(item.get("network_fee_native"), allow_zero=True)
            native_usd = _historical_price(
                item.get("native_usd_proof"), asset if asset in _NATIVE else "ETH",
                block_unix)
            stable_usd = _historical_price(item.get("stable_usd_proof"),
                                           stable, block_unix)
            amount_native = destination if side == "BUY" else source
            conversion_cost = _conversion_fee(
                item, asset, side, block, amount_native, seen)
            if side == "BUY":
                lots.append([destination, source * stable_usd,
                             gas * native_usd, conversion_cost])
                continue
            if source > sum((lot[0] for lot in lots), Decimal(0)):
                raise ValueError("Cannot charge performance fee on unmatched SELL")
            outstanding = source
            acquire = buy_gas = buy_conversions = Decimal(0)
            while outstanding:
                units, dollars, network, conversion = lots[0]
                matched = min(units, outstanding)
                fraction = matched / units
                a, g, n = dollars*fraction, network*fraction, conversion*fraction
                acquire += a
                buy_gas += g
                buy_conversions += n
                if matched == units:
                    lots.popleft()
                else:
                    lots[0] = [units-matched, dollars-a, network-g, conversion-n]
                outstanding -= matched
            proceeds = destination*stable_usd
            net_evidence = {
                "mode": "SERVER_RECONCILED_EVM_NET_COST_EVIDENCE",
                "asset": asset, "stable": stable, "side": "SELL",
                "chain_id": pair["chain_id"], "network": pair["network"],
                "intent_key": item["intent_key"], "tx_hash": tx_hash,
                "wallet_address": wallet,
                "finalized_onchain_sell_proved": True,
                "complete_fifo_lot_provenance": True,
                "complete_historical_price_provenance": True,
                "stablecoin_usd_rates_at_each_fill_verified": True,
                "gas_native_usd_rates_at_each_fill_verified": True,
                "all_native_conversion_costs_reconciled": True,
                "external_inventory_reconciled": True,
                "history_not_truncated": True,
                "execution_fees_reconciled": True, "contains_paper": False,
                "confirmed_sell_proceeds_usd": _quantized(proceeds),
                "fifo_acquisition_cost_usd": _quantized(acquire, cost=True),
                "allocated_buy_network_gas_usd": _quantized(buy_gas, cost=True),
                "sell_network_gas_usd": _quantized(gas*native_usd, cost=True),
                "native_conversion_gas_usd": _quantized(
                    buy_conversions + conversion_cost, cost=True),
                "other_confirmed_execution_cost_usd": "0",
            }
            close = calculate_verified_evm_net_costs(net_evidence)
            close["net_pnl_is_read_only"] = True
            close["fee_assessed"] = False
            closes.append(close)
        return {
            "mode": "RECONCILED_EVM_FIFO_NET_READ_ONLY",
            "asset": asset, "wallet_address": wallet,
            "closed_trades": closes,
            "remaining_fifo_units": format(
                sum((lot[0] for lot in lots), Decimal(0)), "f"),
            "fee_assessed": False,
            "fee_paid_onchain": False,
            "automatic_fee_activated": False,
        }
