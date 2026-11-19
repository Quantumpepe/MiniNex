"""Shared conservative FIFO cost-basis preview across all seven EVM assets.

Uses only independently finalized Router02 swap legs, and NEVER treats
stablecoin-denominated gross spread as USD net profit. Actual native gas
transaction prices, wrap/unwrap costs, external deposits and wallet holdings
must be separately reconciled before any performance fee can be assessed.

ETH's existing historical preview stays intact; this common engine can
consume its verified fill shape as well as the five other EVM assets.
"""
from __future__ import annotations

from collections import deque
from decimal import Decimal, InvalidOperation, localcontext

from fastapi import HTTPException

from .shared_evm_swap_receipts import _pair

_ASSETS=frozenset(("ETH","BNB","POL","AVAX","LINK","UNI","ARB"))


def _units(value: object, *, allow_zero: bool=False) -> Decimal:
    if type(value) is not str or not 1<=len(value)<=78:
        raise ValueError("Missing exact on-chain amount")
    try:
        amount=Decimal(value)
    except InvalidOperation:
        raise ValueError("Invalid on-chain amount") from None
    if (not amount.is_finite() or amount<0 or (not allow_zero and amount==0)
            or amount>=Decimal("1e25")
            or amount.as_tuple().exponent < -18):
        raise ValueError("Invalid exact on-chain quantity")
    return amount


def evm_fifo_gross_preview(history: object) -> dict:
    """FIFO-match only a bounded, explicitly untruncated owner trade history.

    Native-chain assets are represented by wrapped ERC20 swap legs. This
    calculation is NOT native coin settlement, verified wallet inventory,
    USD PnL, or taxable/custodial accounting.
    """
    if (type(history) is not dict or history.get("mode")!="FINALIZED_EVM_REAL_FILLS_ONLY"
            or history.get("asset") not in _ASSETS
            or history.get("includes_simulation") is not False
            or type(history.get("history_truncated")) is not bool
            or type(history.get("fills")) is not list
            or len(history["fills"])>100):
        raise ValueError("Untrusted EVM finalized trade history")
    asset=history["asset"]
    pairs={s:_pair(asset,s,"BUY") for s in ("USDC","USDT") if _pair_supported(asset,s)}
    gross={s:Decimal(0) for s in pairs}
    unmatched={s:Decimal(0) for s in pairs}
    lots={s:deque() for s in pairs}
    seen=set()
    closes=[]
    gas_total=Decimal(0)
    unmatched_closes=0
    with localcontext() as ctx:
        ctx.prec=65
        for row in reversed(history["fills"]):
            if (type(row) is not dict or row.get("asset")!=asset
                    or row.get("onchain_finalized") is not True
                    or row.get("chain_id")!=_pair(asset,row.get("stable"),row.get("side"))["chain_id"]
                    or row.get("network")!=_pair(asset,row["stable"],row["side"])["network"]
                    or type(row.get("intent_key")) is not str
                    or not row["intent_key"] or row["intent_key"] in seen
                    or row.get("stable") not in pairs
                    or row.get("side") not in ("BUY","SELL")):
                raise ValueError("Unverified or duplicate EVM trade")
            key=row["intent_key"]
            seen.add(key)
            side=row["side"]
            stable=row["stable"]
            if row.get("spent_asset")!=(stable if side=="BUY" else asset):
                raise ValueError("Invalid EVM source asset")
            if row.get("received_asset")!=(asset if side=="BUY" else stable):
                raise ValueError("Invalid EVM output asset")
            source=_units(row.get("spent_amount"))
            dest=_units(row.get("received_amount"))
            gas=_units(row.get("network_fee_native"),allow_zero=True)
            gas_total+=gas
            if side=="BUY":
                # Native transaction fees are recorded but cannot be valued
                # as historical USD until a real execution-time rate exists.
                lots[stable].append([dest,source,gas])
                continue
            remaining=source
            available=sum((lot[0] for lot in lots[stable]),Decimal(0))
            if available<source:
                unmatched_closes+=1
                unmatched[stable]+=source-available
                closes.append({
                    "intent_key":key,"stable":stable,
                    "status":"UNMATCHED_COST_BASIS",
                    "gross_spread_stable":None,
                    "historical_network_fee_usd":None,
                })
            else:
                cost=Decimal(0)
                buy_gas=Decimal(0)
                # All amounts and matched historical gas are allocated pro
                # rata, including partial lots, with no float rounding.
                for lot in lots[stable]:
                    if remaining==0:
                        break
                    filled=min(remaining,lot[0])
                    ratio=filled/lot[0]
                    used_cost=lot[1]*ratio
                    used_gas=lot[2]*ratio
                    cost+=used_cost
                    buy_gas+=used_gas
                    lot[0]-=filled
                    lot[1]-=used_cost
                    lot[2]-=used_gas
                    remaining-=filled
                spread=dest-cost
                gross[stable]+=spread
                closes.append({
                    "intent_key":key,"stable":stable,
                    "status":"MATCHED_GROSS_ONLY",
                    "allocated_buy_cost_stable":format(cost,"f"),
                    "gross_sell_proceeds_stable":format(dest,"f"),
                    "gross_spread_stable":format(spread,"f"),
                    "allocated_swap_network_fee_native":format(buy_gas+gas,"f"),
                    "historical_network_fee_usd":None,
                })
            # Even an unmatched SELL consumes all known lots; never reuse
            # already sold inventory for a later trade.
            if available<source:
                remaining=min(source,available)
                while remaining>0 and lots[stable]:
                    lot=lots[stable][0]
                    used=min(remaining,lot[0])
                    ratio=used/lot[0]
                    lot[1]-=lot[1]*ratio
                    lot[2]-=lot[2]*ratio
                    lot[0]-=used
                    remaining-=used
                    if lot[0]==0:
                        lots[stable].popleft()
            else:
                while lots[stable] and lots[stable][0][0]==0:
                    lots[stable].popleft()
    blockers=["HISTORICAL_NATIVE_GAS_USD_PRICE_MISSING",
              "STABLECOIN_USD_CONVERSION_MISSING",
              "EXTERNAL_INVENTORY_AND_TRANSFERS_UNRECONCILED",
              "NATIVE_WRAP_UNWRAP_COSTS_UNRECONCILED"]
    if history["history_truncated"]:
        blockers.insert(0,"HISTORY_TRUNCATED")
    if unmatched_closes:
        blockers.insert(0,"UNMATCHED_FIFO_COST_BASIS")
    return {
        "mode":"SHARED_EVM_FIFO_GROSS_PREVIEW_ONLY",
        "asset":asset,"source":"FINALIZED_EVM_REAL_FILLS_ONLY",
        "history_truncated":history["history_truncated"],
        "unmatched_sell_count":unmatched_closes,
        "unmatched_sell_units_by_stable":{
            s:format(q,"f") for s,q in unmatched.items()},
        "closed_trades":list(reversed(closes)),
        "gross_spread_by_stable":{
            s:(format(q,"f") if not history["history_truncated"]
               and unmatched_closes==0 else None)
            for s,q in gross.items()},
        "remaining_fifo_units_not_wallet_balance":{
            s:format(sum((lot[0] for lot in queue),Decimal(0)),"f")
            for s,queue in lots.items()},
        "verified_swap_network_fee_native":format(gas_total,"f"),
        "network_fee_valued_in_usd":False,
        "native_cycle_settled":False,
        "verified_net_pnl_usd":None,
        "hwm_updated":False,
        "performance_fee_assessed":False,
        "fee_paid_onchain":False,
        "fee_blockers":blockers,
    }


def _pair_supported(asset:str,stable:str)->bool:
    try:
        _pair(asset,stable,"BUY")
        return True
    except (ValueError,KeyError):
        return False


def read_evm_fifo_gross_preview(did:str,asset:str)->dict:
    """Owner-only query for five additional EVM assets.

    Use the existing ETH legacy read-only cost preview for ETH. This
    shared pure function also supports ETH after verified normalization.
    """
    if asset=="ETH":
        from .eth_fifo_gross_preview import read_eth_fifo_gross_preview
        return read_eth_fifo_gross_preview(did)
    from .shared_evm_fill_history import get_owner_evm_fill_history
    history=get_owner_evm_fill_history(did,asset=asset,limit=50)
    try:
        return evm_fifo_gross_preview(history)
    except (ValueError,TypeError,ArithmeticError):
        raise HTTPException(503,"Verified EVM FIFO preview unavailable") from None
