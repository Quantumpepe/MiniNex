"""Exact per-realized-trade net accounting, independent of retired HWM.

Internal calculation only; no signer, cash release, payment or current-price
fallback. The caller must supply canonical ordered finalized fills and
independently verified historical USD evidence for the original transactions.
"""
from collections import defaultdict,deque
from decimal import Decimal
from fractions import Fraction
from .ethereum_settlement_plan import COIN_NETWORKS,ETHEREUM_STABLES

NATIVE_GAS={1:"ETH",56:"BNB",137:"POL",43114:"AVAX",42161:"ETH"}

def exact(value,*,positive=False):
    if isinstance(value,bool) or not isinstance(value,(str,int,Decimal)):
        raise ValueError("Exact ledger decimal required")
    result=Decimal(value)
    if not result.is_finite() or result<0 or (positive and not result):
        raise ValueError("Invalid ledger decimal")
    return Fraction(result)

def decimal_text(value,places=12):
    # Round toward zero at display precision, never round a fee upwards.
    scale=10**places
    units=int(value*scale)
    sign="-" if units<0 else ""
    whole,fraction=divmod(abs(units),scale)
    return f"{sign}{whole}.{fraction:0{places}d}"

def historical_cost_usd(costs,rates):
    """Price only exact owner-paid costs; bridge spread is already in fills.

    Rates keyed by (chain, original transaction hash, native asset). Missing
    historical rates leave the cost unknown. No current-price substitutions.
    """
    seen=set()
    total=Fraction(0)
    missing=False
    for cost in costs:
        chain=cost["chain_id"];tx=cost["tx_hash"]
        if type(chain) is not int or chain not in NATIVE_GAS or type(tx) is not str or not tx.startswith("0x") or len(tx)!=66:
            raise ValueError("Invalid historical cost identity")
        try:
            int(tx[2:],16)
        except ValueError:
            raise ValueError("Invalid historical cost transaction hash") from None
        if tx!=tx.lower() or (chain,tx) in seen:
            raise ValueError("Duplicate or noncanonical cost transaction")
        seen.add((chain,tx))
        gas=exact(cost["gas_atomic"])
        bridge=exact(cost["native_bridge_fee_atomic"])
        if gas.denominator!=1 or bridge.denominator!=1:
            raise ValueError("Noninteger original gas amount")
        if not gas+bridge:
            continue
        key=(chain,tx,NATIVE_GAS[chain])
        if key not in rates:
            missing=True
            continue
        total+=(gas+bridge)*exact(rates[key],positive=True)/10**18
    return None if missing else total

def calculate_realized_net(rows):
    """FIFO lots, grouped by asset, actual inventory family and wallet.

    Each row: canonical finalized fill identity, side, stable, amounts,
    automatic flag, historical stable USD rate, and total historical
    owner-paid network cost in USD. None means genuinely unavailable.
    Manual SELL consumes inventory but never assesses a fee. Unmatched or
    unpriced cost basis must not be treated as zero. No loss carryforward.
    """
    lots=defaultdict(deque)
    seen=set()
    results=[]
    for row in rows:
        asset=row["asset"];family=row["family"];wallet=row["coin_wallet"]
        side=row["side"];stable=row["stable"]
        if (asset not in COIN_NETWORKS or stable not in ETHEREUM_STABLES
                or family not in ((COIN_NETWORKS[asset][3],"WRAPPED_SPOT") if asset in ("BTC","SOL","XRP","TRX") else (COIN_NETWORKS[asset][3],))
                or type(wallet) is not str or not wallet or type(row["automatic"]) is not bool
                or side not in ("BUY","SELL") or type(row["fill_key"]) is not str or not row["fill_key"]):
            raise ValueError("Original finalized fill scope differs")
        key=(asset,family,row["fill_key"])
        if key in seen:
            raise ValueError("Duplicate original finalized fill")
        seen.add(key)
        source=exact(row["source_amount"],positive=True)
        destination=exact(row["destination_amount"],positive=True)
        rate=None if row.get("stable_usd") is None else exact(row["stable_usd"],positive=True)
        cost_value=row.get("network_cost_usd")
        cost=None if cost_value is None else (cost_value if isinstance(cost_value,Fraction) else exact(cost_value))
        if cost is not None and cost<0:
            raise ValueError("Negative network cost")
        q=lots[(asset,family,wallet)]
        if side=="BUY":
            if row["automatic"]:
                q.append([destination,None if rate is None or cost is None else source*rate+cost])
            continue
        remaining=source;basis=Fraction(0);priced=True
        while remaining and q:
            lot=q[0];take=min(remaining,lot[0])
            if lot[1] is None:
                priced=False
            else:
                used=lot[1]*take/lot[0]
                basis+=used;lot[1]-=used
            lot[0]-=take;remaining-=take
            if not lot[0]:
                q.popleft()
        if not row["automatic"]:
            continue
        verified=not remaining and priced and rate is not None and cost is not None
        net=destination*rate-basis-cost if verified else None
        fee=max(net,Fraction(0))/20 if verified else None
        # Canonical ETH USDC/USDT settlement, 6 decimals. A sub-atomic fee
        # remains zero rather than rounding above the agreed 5 percent.
        fee_atomic=int(fee/rate*10**6) if verified else None
        results.append({"asset":asset,"family":family,"fill_key":row["fill_key"],
            "stable":stable,"net_verified":verified,
            "realized_net_usd":decimal_text(net) if verified else None,
            "fee_due_usd":decimal_text(fee) if verified else None,
            "fee_due_stable_atomic":str(fee_atomic) if verified else None,
            "unmatched_inventory":bool(remaining),
            "reason":None if verified else ("UNMATCHED_INVENTORY" if remaining else "HISTORICAL_COST_EVIDENCE_REQUIRED"),
            "payment_authorized":False})
    return results
