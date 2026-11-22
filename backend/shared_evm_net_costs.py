"""Exact-decimal USD settlement arithmetic for the common EVM fee engine.

Runs only on a fully reconciled cost-breakdown object produced by a future
trusted, independently verified historical-rate and FIFO reconciler. This
file intentionally has NO endpoint, remote quote dependency, wallet signer,
HWM write or token transfer. Client-reported prices are never trusted.

It distinguishes actual native gas in chain units from historical USD value:
without the latter a 'gross' trade must not become a 5% fee obligation.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
import re

from .shared_evm_swap_receipts import _pair

_HASH=re.compile(r"^0x[0-9a-f]{64}$")
_ADDR=re.compile(r"^0x[0-9a-f]{40}$")
_KEY=re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,95}$")
_ASSETS=frozenset(("ETH","BNB","POL","AVAX","LINK","QNT","UNI","ARB"))
_COST_FIELDS=(
    "fifo_acquisition_cost_usd",
    "allocated_buy_network_gas_usd",
    "sell_network_gas_usd",
    "native_conversion_gas_usd",
    "other_confirmed_execution_cost_usd",
)


def _usd(value:object,*,positive:bool=False)->Decimal:
    """No floats, exponents, unsafe magnitude or hidden rounding."""
    if (type(value) is not str or not value
            or len(value)>55 or "e" in value.lower()):
        raise ValueError("Exact historical USD amount required")
    try:
        result=Decimal(value)
    except InvalidOperation:
        raise ValueError("Invalid historical USD amount") from None
    if (not result.is_finite() or result<0
            or positive and result<=0
            or result>=Decimal("1e22")
            or result.as_tuple().exponent< -12):
        raise ValueError("Invalid historical USD amount or precision")
    return result


def calculate_verified_evm_net_costs(evidence:object)->dict:
    """Read-only arithmetic after independently sourced chain and USD proofs.

    Flags here must be supplied by a trusted *server-only* reconciler, not
    from requests or strategy/PAPER. They are necessary but not sufficient
    authorization for an HWM update; no such update is exposed here.
    """
    if type(evidence) is not dict:
        raise ValueError("Reconciled EVM closure proof required")
    asset=evidence.get("asset")
    stable=evidence.get("stable")
    if asset not in _ASSETS or type(stable) is not str:
        raise ValueError("Unknown asset or settlement")
    route=_pair(asset,stable,"SELL")
    if (evidence.get("mode")!="SERVER_RECONCILED_EVM_NET_COST_EVIDENCE"
            or evidence.get("side")!="SELL"
            or evidence.get("chain_id")!=route["chain_id"]
            or evidence.get("network")!=route["network"]
            or type(evidence.get("intent_key")) is not str
            or not _KEY.fullmatch(evidence["intent_key"])
            or type(evidence.get("tx_hash")) is not str
            or not _HASH.fullmatch(evidence["tx_hash"])
            or type(evidence.get("wallet_address")) is not str
            or not _ADDR.fullmatch(evidence["wallet_address"])
            or evidence.get("finalized_onchain_sell_proved") is not True
            or evidence.get("complete_fifo_lot_provenance") is not True
            or evidence.get("complete_historical_price_provenance") is not True
            or evidence.get("stablecoin_usd_rates_at_each_fill_verified") is not True
            or evidence.get("gas_native_usd_rates_at_each_fill_verified") is not True
            or evidence.get("all_native_conversion_costs_reconciled") is not True
            or evidence.get("external_inventory_reconciled") is not True
            or evidence.get("history_not_truncated") is not True
            or evidence.get("execution_fees_reconciled") is not True
            or evidence.get("contains_paper") is not False):
        raise ValueError("Historical on-chain/FX/cost evidence incomplete")
    with localcontext() as ctx:
        ctx.prec=65
        proceeds=_usd(evidence.get("confirmed_sell_proceeds_usd"),positive=True)
        costs={field:_usd(evidence.get(field)) for field in _COST_FIELDS}
        total=sum(costs.values(),Decimal(0))
        net=proceeds-total
    return {
        "mode":"RECONCILED_EVM_NET_USD_MATH_ONLY",
        "asset":asset,"network":route["network"],"chain_id":route["chain_id"],
        "intent_key":evidence["intent_key"],"tx_hash":evidence["tx_hash"],
        "stable":stable,"wallet_address":evidence["wallet_address"],
        "verified_input_prices_required":True,
        "sell_proceeds_usd":format(proceeds,"f"),
        "total_execution_cost_usd":format(total,"f"),
        "fifo_acquisition_cost_usd":format(costs["fifo_acquisition_cost_usd"],"f"),
        "network_and_conversion_cost_usd":format(
            costs["allocated_buy_network_gas_usd"]+
            costs["sell_network_gas_usd"]+
            costs["native_conversion_gas_usd"],"f"),
        "other_confirmed_execution_cost_usd":format(
            costs["other_confirmed_execution_cost_usd"],"f"),
        "realized_net_usd":format(net,"f"),
        "hwm_updated":False,"fee_assessed":False,
        "fee_paid_onchain":False,"wallet_signed":False,
    }
