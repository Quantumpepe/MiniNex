"""Owner-selected native coin BUY sizing from verified real wallet exposures.

This pure calculation is NOT spending permission, a rate limit or an order.
The Ethereum source wallet holds USDC/USDT; BTC/SOL/XRP/TRX/DOGE already
held on their original native wallets count against their own user's budget.
Unsettled original BUY funding also counts until the real native chain shows
a final credit or a verified refund.  No paper balances or estimated fills.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN

_NATIVE={
    "BTC":("bitcoin-mainnet",8,("USDC","USDT")),
    "SOL":("solana-mainnet",9,("USDC","USDT")),
    "XRP":("xrpl-mainnet",6,("USDC",)),
    "TRX":("tron-mainnet",6,("USDT",)),
    "DOGE":("dogecoin-mainnet",8,("USDC","USDT")),
}
_USD=Decimal("1")
_MICRO=Decimal("0.000001")


def _nonnegative_exact(value:object,field:str)->Decimal:
    if type(value) not in (int,str,Decimal):
        raise ValueError("Unverified "+field)
    if type(value) is str and (
        not value or len(value)>80 or "e" in value.lower()
        or not value.isascii()):
        raise ValueError("Unverified "+field)
    try:
        n=Decimal(value)
    except (InvalidOperation,TypeError,ValueError):
        raise ValueError("Unverified "+field) from None
    if not n.is_finite() or n<0:
        raise ValueError("Unverified "+field)
    return n


def exact_owner_native_buy_amount(
    *,asset:str,stable:str,budget_usd:int,
    native_wallet_evidence:dict,stable_wallet_evidence:dict,
    market_price_usd:object,stable_price_usd:object,
    pending_buy_stable:object=0,pending_buy_usd:object=0,
    observed_source_fees_usd:object=0,
)->str|None:
    """Calculate precise stablecoin BUY amount within actual selected budget.

    A missing explorer balance is NEVER zero. The asset wallet is distinct
    from the ETH USDC/USDT source wallet; no capital rotates across coins.
    Existing native holdings and unsettled committed BUYs share one budget.
    Fees, if independently known, reduce remaining budget instead of being
    silently financed outside the user's chosen allocation.
    """
    cfg=_NATIVE.get(asset)
    if (cfg is None or stable not in cfg[2]
            or type(budget_usd) is not int or budget_usd<=0
            or type(native_wallet_evidence) is not dict
            or type(stable_wallet_evidence) is not dict
            or native_wallet_evidence.get("asset")!=asset
            or native_wallet_evidence.get("network")!=cfg[0]
            or native_wallet_evidence.get("status")!="verified"
            or stable_wallet_evidence.get("status")!="verified"
            or stable_wallet_evidence.get("network")!="ethereum-mainnet"):
        return None
    try:
        owned=_nonnegative_exact(native_wallet_evidence.get("balance"),"native balance")
        liquid=_nonnegative_exact(stable_wallet_evidence.get("balance"),"ETH source stable")
        coin_usd=_nonnegative_exact(market_price_usd,"coin USD rate")
        stable_usd=_nonnegative_exact(stable_price_usd,"stable USD rate")
        pending=_nonnegative_exact(pending_buy_stable,"unsettled native BUY")
        pending_usd=_nonnegative_exact(pending_buy_usd,"unsettled native BUY USD")
        fees=_nonnegative_exact(observed_source_fees_usd,"real source fees")
        if coin_usd<=0 or stable_usd<=0 or not Decimal("0.95")<=stable_usd<=Decimal("1.05"):
            return None
        held_value=owned*coin_usd
        unclosed_value=pending*stable_usd+pending_usd
        available=max(Decimal(0),Decimal(budget_usd)-held_value-unclosed_value-fees)
        allowed=min(liquid,available/stable_usd)
        amount=allowed.quantize(_MICRO,rounding=ROUND_DOWN)
        return format(amount,"f").rstrip("0").rstrip(".") if amount>0 else None
    except (InvalidOperation,ValueError,TypeError,ArithmeticError):
        return None


def native_pending_buy_stable_total(rows:object,*,asset:str,stable:str)->Decimal:
    """Aggregate only unresolved original wallet BUY source claims.

    The DB selector is owner- and coin-scoped. Completed native payouts are
    valued through their actual native wallet balance, not double-counted.
    """
    cfg=_NATIVE.get(asset)
    if cfg is None or stable not in cfg[2] or type(rows) is not list:
        raise ValueError("Native pending source evidence unavailable")
    result=Decimal(0)
    pending=frozenset((
        "PREPARED","SENDING","SUBMITTED",
        "RECONCILIATION_REQUIRED","SOURCE_FINALIZED",
    ))
    for row in rows:
        if (type(row) is not dict or row.get("asset")!=asset
                or row.get("side")!="BUY" or row.get("stable")!=stable):
            raise ValueError("Cross-coin or cross-stable pending source")
        if row.get("status") in pending:
            amount=_nonnegative_exact(row.get("input_amount"),"pending source")
            if amount<=0:
                raise ValueError("Zero native committed BUY")
            result+=amount
        elif row.get("status") not in (
                "DESTINATION_FINALIZED","REFUNDED_VERIFIED","CANCELLED"):
            raise ValueError("Unknown native funding journal status")
    return result
