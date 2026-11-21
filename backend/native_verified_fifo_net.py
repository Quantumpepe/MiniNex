"""Exact native BTC/SOL/XRP FIFO and HWM math on already-finalized fills.

Pure internal calculation. It consumes rows supplied by an independent
complete-wallet-history reconciler; a row or client boolean alone cannot prove
a Blockchain fill, USD price, network fee or wallet ownership. This module
never books PnL, assesses a fee obligation, sends funds or changes balances.
BUY capitalizes original Ethereum network fees into the native FIFO lot.
SELL subtracts actual original native-chain network costs from sale proceeds.
THORChain fees are already reflected in confirmed net destination amounts:
do NOT charge them a second time. 5% HWM is informational until authenticated
chain evidence and historical prices are independently audited and persisted.
"""
from __future__ import annotations

from collections import deque
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_CEILING, localcontext
import re

from .fees import RATE, assess_realized_gain

_ASSETS=frozenset(("BTC","SOL","XRP"))
_PRECISION={"BTC":8,"SOL":9,"XRP":6}
_SOURCE_NETWORK={"BTC":"bitcoin-mainnet","SOL":"solana-mainnet","XRP":"xrpl-mainnet"}
_SHA=re.compile(r"^[0-9a-f]{64}$")
_INTENT=re.compile(r"^native_[0-9a-f]{64}$")
_ETH_HASH=re.compile(r"^0x[0-9a-f]{64}$")
_NATIVE_HASH=re.compile(r"^[A-Za-z0-9]{64,90}$")
_POSITIVE=re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_CENT12=Decimal("0.000000000001")


def _decimal(value:object,*,allow_zero:bool=False)->Decimal:
    if type(value) is not str or len(value)>78 or not _POSITIVE.fullmatch(value):
        raise ValueError("Exact owner on-chain source amount required")
    try:
        quantity=Decimal(value)
    except InvalidOperation:
        raise ValueError("Invalid confirmed source quantity") from None
    if (not quantity.is_finite() or quantity<0
            or (quantity==0 and not allow_zero) or quantity>=Decimal("1e30")):
        raise ValueError("Unusable confirmed native accounting amount")
    return quantity


def _reference(proof:object,*,asset:str,unix:int)->Decimal:
    """Only bounded historical block-time USD reference evidence may be used."""
    if (type(proof) is not dict or
            proof.get("mode")!="HISTORICAL_EXECUTION_USD_REFERENCE_ONLY"
            or proof.get("provider")!="COINGECKO_MARKET_CHART_RANGE"
            or proof.get("asset")!=asset
            or proof.get("execution_block_unix")!=unix
            or type(proof.get("rate_observed_unix")) is not int
            or abs(proof["rate_observed_unix"]-unix)>180):
        raise ValueError("Missing historical block-time USD reference")
    rate=_decimal(proof.get("usd_price"))
    if asset in ("USDC","USDT") and not Decimal("0.95")<=rate<=Decimal("1.05"):
        raise ValueError("Cannot assess depegged stablecoin automatically")
    return rate


def _exact_cost(row:dict,asset:str,side:str,unix:int)->Decimal:
    cost=row.get("network_fee")
    source="ETH" if side=="BUY" else asset
    source_chain="ethereum-mainnet" if side=="BUY" else _SOURCE_NETWORK[asset]
    if (type(cost) is not dict
            or cost.get("mode")!="CONFIRMED_NATIVE_ORIGINAL_SOURCE_NETWORK_COST"
            or cost.get("asset")!=source or cost.get("chain")!=source_chain
            or cost.get("source_tx_hash")!=row.get("source_tx_hash")
            or cost.get("execution_block_unix")!=unix
            or cost.get("original_receipt_proof_sha256")!=row.get("source_proof_sha256")):
        raise ValueError("Original chain fee not independently matched")
    fee=_decimal(cost.get("fee_asset_amount"),allow_zero=True)
    return fee*_reference(cost.get("fee_asset_usd_reference"),asset=source,unix=unix)


def _quantize(value:Decimal,*,cost:bool)->str:
    return format(value.quantize(_CENT12,
        rounding=ROUND_CEILING if cost else ROUND_FLOOR),"f")


def preview_native_fifo_net_hwm(history:object)->dict:
    """Price historical complete owner trade sequence; not a fee collection.

    Caller must supply independently validated, complete and chronological
    original journal records joined with both original Blockchain receipts
    and block-time pricing proofs; this calculation cannot establish that
    completeness from a payload and does not claim that it did.
    """
    if (type(history) is not dict
            or history.get("asset") not in _ASSETS
            or type(history.get("privy_did")) is not str
            or not history["privy_did"].startswith("did:privy:")
            or type(history.get("fills")) is not list
            or not history["fills"]):
        raise ValueError("Unverified native FIFO input format")
    asset=history["asset"]
    did=history["privy_did"]
    seen=set()
    lots=deque()
    closes=[]
    previous_unix=-1  # Original fills may legitimately share a block timestamp.
    cumulative=Decimal(0)
    hwm=Decimal(0)
    with localcontext() as ctx:
        ctx.prec=70
        for row in history["fills"]:
            if (type(row) is not dict
                    or row.get("asset")!=asset
                    or row.get("privy_did")!=did
                    or row.get("side") not in ("BUY","SELL")
                    or row.get("status")!="DESTINATION_FINALIZED"
                    or row.get("stable") not in ("USDC","USDT")
                    or (asset=="XRP" and row.get("stable")!="USDC")
                    or type(row.get("intent_key")) is not str
                    or not _INTENT.fullmatch(row["intent_key"])
                    or row["intent_key"] in seen
                    or any(type(row.get(key)) is not str
                           or not _SHA.fullmatch(row[key]) for key in (
                               "payload_sha256","source_proof_sha256",
                               "destination_proof_sha256"))
                    or type(row.get("execution_block_unix")) is not int
                    or row["execution_block_unix"]<previous_unix):
                raise ValueError("Unverified or unordered owner native settlement")
            seen.add(row["intent_key"])
            unix=row["execution_block_unix"]
            previous_unix=unix
            side=row["side"]
            source_hash=row.get("source_tx_hash")
            destination_hash=row.get("destination_tx_hash")
            if (type(source_hash) is not str
                    or type(destination_hash) is not str
                    or not (_ETH_HASH.fullmatch(source_hash)
                            if side=="BUY" else _NATIVE_HASH.fullmatch(source_hash))
                    or not (_ETH_HASH.fullmatch(destination_hash)
                            if side=="SELL" else _NATIVE_HASH.fullmatch(destination_hash))):
                raise ValueError("Original native source/destination transaction missing")
            rate=_reference(row.get("stable_usd_reference"),
                            asset=row["stable"],unix=unix)
            network_cost=_exact_cost(row,asset,side,unix)
            incoming=row.get("destination_received_atomic")
            if (type(incoming) is not str or not re.fullmatch(r"[1-9][0-9]{0,76}",incoming)):
                raise ValueError("Exact confirmed original destination amount missing")
            atomic=Decimal(incoming)
            if side=="BUY":
                quantity=atomic/(Decimal(10)**_PRECISION[asset])
                paid=_decimal(row.get("input_amount"))
                if paid.as_tuple().exponent < -6:
                    raise ValueError("ETH stablecoin source precision invalid")
                total_cost=paid*rate+network_cost
                lots.append([quantity,total_cost,row["intent_key"]])
            else:
                sold=_decimal(row.get("input_amount"))
                if sold.as_tuple().exponent < -_PRECISION[asset]:
                    raise ValueError("Native original SELL precision invalid")
                stable_received=atomic/Decimal(10**6)
                proceeds=stable_received*rate-network_cost
                remaining=sold
                cost=Decimal(0)
                consumed=[]
                while remaining>0:
                    if not lots:
                        raise ValueError("Cannot assess SELL without complete original FIFO acquisition")
                    qty,value,key=lots[0]
                    take=min(qty,remaining)
                    allocated=value if take==qty else value*take/qty
                    cost+=allocated
                    consumed.append({"buy_intent_key":key,
                                     "quantity_native":format(take,"f")})
                    qty-=take
                    value-=allocated
                    remaining-=take
                    if qty==0:
                        lots.popleft()
                    else:
                        lots[0]=[qty,value,key]
                net=proceeds-cost
                assessed=assess_realized_gain(str(cumulative),str(hwm),str(net))
                cumulative=assessed.cumulative_usd
                hwm=assessed.high_water_mark_usd
                closes.append({
                    "asset":asset,
                    "sell_intent_key":row["intent_key"],
                    "quantity_sold_native":format(sold,"f"),
                    "sold_fiat_received_usd":_quantize(stable_received*rate,cost=False),
                    "original_source_network_fee_usd":_quantize(network_cost,cost=True),
                    "buy_fifo_allocated_cost_usd":_quantize(cost,cost=True),
                    "realized_net_usd":format(net.quantize(_CENT12,rounding=ROUND_FLOOR),"f"),
                    "cumulative_realized_net_usd":str(cumulative),
                    "high_water_mark_usd":str(hwm),
                    "net_gain_above_previous_hwm_usd":str(assessed.new_profit_above_hwm_usd),
                    "indicative_5pct_hwm_usd":str(assessed.fee_assessed_usd),
                    "matched_buy_lots":consumed,
                    "fee_assessed":False,
                    "fee_transfer_submitted":False,
                })
        return {
            "mode":"INTERNAL_NATIVE_FIFO_NET_HWM_CALCULATION_ONLY",
            "asset":asset,"privy_did":did,
            "coin_trades":len(history["fills"]),
            "sell_trades":len(closes),
            "close_previews":closes,
            "remaining_native_quantity":format(sum((q for q,_,_ in lots),Decimal(0)),"f"),
            "cumulative_realized_net_usd":str(cumulative),
            "high_water_mark_usd":str(hwm),
            "profit_fee_rate":str(RATE),
            "complete_original_wallet_history_independently_audited":False,
            "realized_profit_recorded":False,
            "fee_obligation_created":False,
            "fee_payment_transferred":False,
            "native_live_trading_enabled":False,
        }
