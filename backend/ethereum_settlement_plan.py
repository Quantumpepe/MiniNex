"""Canonical settlement topology and exact sizing for all twelve MiniNex coins.

This module is pure: route classification is not liquidity, execution readiness,
a grant, or an accounting entry. Both directions use Ethereum mainnet stablecoins.
"""
from decimal import Decimal, ROUND_DOWN, localcontext
from types import MappingProxyType

ETHEREUM_CHAIN_ID=1
ETHEREUM_STABLES=MappingProxyType({
    "USDC":"0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    "USDT":"0xdac17f958d2ee523a2206206994597c13d831ec7",
})
COIN_NETWORKS=MappingProxyType({
    "BTC":("bitcoin-mainnet",None,8,"NATIVE"),
    "ETH":("ethereum-mainnet",1,18,"ETHEREUM"),
    "BNB":("bsc-mainnet",56,18,"CROSS_EVM"),
    "SOL":("solana-mainnet",None,9,"NATIVE"),
    "XRP":("xrpl-mainnet",None,6,"NATIVE"),
    "TRX":("tron-mainnet",None,6,"NATIVE"),
    "POL":("polygon-pos-mainnet",137,18,"CROSS_EVM"),
    "AVAX":("avalanche-c-mainnet",43114,18,"CROSS_EVM"),
    "LINK":("ethereum-mainnet",1,18,"ETHEREUM"),
    "QNT":("ethereum-mainnet",1,18,"ETHEREUM"),
    "UNI":("ethereum-mainnet",1,18,"ETHEREUM"),
    "ARB":("arbitrum-one-mainnet",42161,18,"CROSS_EVM"),
})


def settlement_route(asset,side,stable):
    if asset not in COIN_NETWORKS or side not in ("BUY","SELL") or stable not in ETHEREUM_STABLES:
        raise ValueError("Invalid Ethereum settlement pair")
    network,chain,decimals,family=COIN_NETWORKS[asset]
    buy=side=="BUY"
    return {
        "asset":asset,"side":side,"stable":stable,"family":family,
        "source_asset":stable if buy else asset,"destination_asset":asset if buy else stable,
        "source_network":"ethereum-mainnet" if buy else network,
        "destination_network":network if buy else "ethereum-mainnet",
        "source_chain_id":1 if buy else chain,"destination_chain_id":chain if buy else 1,
        "source_decimals":6 if buy else decimals,"destination_decimals":decimals if buy else 6,
        "settlement_chain_id":1,"settlement_contract":ETHEREUM_STABLES[stable],
        "settlement_decimals":6,
    }


def check_evm_quote_route(terms):
    route=settlement_route(terms["asset"],terms["side"],terms["stable"])
    if route["family"]!="CROSS_EVM" or (
        terms["source_chain_id"],terms["destination_chain_id"]
    )!=(route["source_chain_id"],route["destination_chain_id"]):
        raise ValueError("Quote is not the canonical Ethereum cross-EVM route")
    key="source_token" if terms["side"]=="BUY" else "destination_token"
    if str(terms[key]).lower()!=route["settlement_contract"]:
        raise ValueError("Quote uses a noncanonical settlement token")
    return route


def _decimal(value,*,positive=False):
    if isinstance(value,bool) or not isinstance(value,(Decimal,int,str)):
        raise ValueError("Exact decimal evidence required")
    amount=Decimal(value)
    if not amount.is_finite() or amount<0 or (positive and amount==0):
        raise ValueError("Invalid funding evidence")
    return amount


def _atomic(value,*,positive=False):
    if isinstance(value,bool) or not isinstance(value,(int,str)):
        raise ValueError("Exact atomic evidence required")
    if isinstance(value,str) and (not value.isascii() or not value.isdecimal()):
        raise ValueError("Invalid atomic evidence")
    n=int(value)
    if n<0 or n>=2**256 or (positive and n==0):
        raise ValueError("Invalid atomic evidence")
    return n


def ethereum_buy_amount(*,budget_usd,position_usd,pending_coin_usd,
                        wallet_stable_atomic,pending_wallet_stable_atomic,
                        stable_usd,estimated_cost_usd=0):
    """Size one coin from the owner's budget and shared Ethereum stable funds.

    The reservation caller must lock/recheck current balances and pending orders
    across ALL engines before signing. Cost estimate includes all known fees
    budgeted separately, not fees already contained in the quoted source spend.
    No synthetic balance, implicit percentage or platform trade-size cap.
    """
    with localcontext() as ctx:
        ctx.prec=100
        budget=_decimal(budget_usd,positive=True)
        exposure=_decimal(position_usd)
        pending=_decimal(pending_coin_usd)
        cost=_decimal(estimated_cost_usd)
        rate=_decimal(stable_usd,positive=True)
        wallet=_atomic(wallet_stable_atomic)
        reserved=_atomic(pending_wallet_stable_atomic)
        available=max(0,wallet-reserved)
        budget_available=max(Decimal(0),budget-exposure-pending-cost)
        budget_atomic=int((budget_available/rate*10**6).to_integral_value(rounding=ROUND_DOWN))
        amount=min(available,budget_atomic)
        return {"input_atomic":str(amount),"input_amount":format(Decimal(amount).scaleb(-6),"f"),
                "budget_available_usd":format(budget_available,"f"),
                "wallet_available_atomic":str(available),"can_submit":False}


def ethereum_sell_amount(*,asset,wallet_coin_atomic,owned_position_atomic,pending_sell_atomic,
                         gas_reserve_atomic=0):
    """Never interpret every coin in a wallet as a strategy-owned position."""
    if asset not in COIN_NETWORKS:
        raise ValueError("Unsupported position")
    wallet=_atomic(wallet_coin_atomic)
    owned=_atomic(owned_position_atomic)
    pending=_atomic(pending_sell_atomic)
    gas=_atomic(gas_reserve_atomic)
    amount=min(max(0,wallet-gas),max(0,owned-pending))
    with localcontext() as ctx:
        ctx.prec=100
        return {"input_atomic":str(amount),
                "input_amount":format(Decimal(amount).scaleb(-COIN_NETWORKS[asset][2]),"f"),
                "can_submit":False}
