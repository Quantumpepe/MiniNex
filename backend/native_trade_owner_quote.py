"""User-owned SOL/XRP real quote and funds provenance for future LIVE orders.

This is an actual owner-scoped wallet/quote adapter, but signing, executable
provider instructions, source-chain finality and destination-chain settlement
are separate requirements. Neither a verified balance nor a quote can send.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re

from fastapi import HTTPException

from .native_coin_balances import get_native_balances
from .wallet_balances import get_wallet_balances
from .manual_swap_live_quotes import get_native_swap_quote
from .storage import _safe_did

_ASSETS=frozenset(("SOL","XRP"))
_STABLES=frozenset(("USDC","USDT"))
_AMOUNT=re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


def _positive_decimal(raw:object)->Decimal:
    if type(raw) is not str or not 0<len(raw)<=48 or not _AMOUNT.fullmatch(raw):
        raise ValueError("Invalid account-owned source quantity")
    try:
        amount=Decimal(raw)
    except InvalidOperation:
        raise ValueError("Invalid account-owned source quantity") from None
    if not amount.is_finite() or amount<0:
        raise ValueError("Invalid account-owned source quantity")
    return amount


async def quote_owner_native_trade(
    did:str,*,asset:str,side:str,stable:str,amount:str,
)->dict:
    """Check the owner's actual source balance BEFORE any native provider quote.

    SOL and XRP have independent chain wallets. MiniNex stablecoins live on
    the owner's Ethereum wallet, never on a fabricated Solana/XRPL address.
    Full spendability (gas, account reserves, venue refunds) is NOT inferred.
    """
    did=_safe_did(did)
    if asset not in _ASSETS or side not in ("BUY","SELL") or stable not in _STABLES:
        raise HTTPException(422,"Unsupported native trade request")
    try:
        requested=_positive_decimal(amount)
    except ValueError:
        raise HTTPException(422,"Invalid exact native input") from None
    if requested<=0:
        raise HTTPException(422,"Invalid exact native input")

    native=await get_native_balances(did)
    eth=await get_wallet_balances(did)
    owned=native.get("assets",{}).get(asset)
    wallet=eth.get("wallets",{})
    if (type(owned) is not dict or owned.get("status")!="verified"
            or owned.get("network")!=("solana-mainnet" if asset=="SOL" else "xrpl-mainnet")
            or not wallet.get("ETH")
            or eth.get("wallet_ownership_verified") is not True):
        raise HTTPException(409,"Native source and Ethereum settlement wallets not verified")

    source=stable if side=="BUY" else asset
    balance=(eth.get("balances",{}).get(stable) if side=="BUY" else owned)
    if type(balance) is not dict or balance.get("status")!="verified":
        raise HTTPException(409,"Owner's source balance not independently verified")
    try:
        available=_positive_decimal(balance.get("balance"))
    except ValueError:
        raise HTTPException(503,"Malformed verified owner balance") from None
    if requested>available:
        raise HTTPException(409,"Insufficient verified owner balance")

    target=asset if side=="BUY" else stable
    quote=await get_native_swap_quote(source,target,amount)
    if (type(quote) is not dict
            or quote.get("mode")!="NATIVE_MULTICHAIN_REAL_MARKET_QUOTE_ONLY"
            or quote.get("source_asset")!=source
            or quote.get("destination_asset")!=target
            or quote.get("input_amount")!=amount
            or quote.get("execution_enabled") is not False
            or quote.get("can_submit") is not False):
        raise HTTPException(502,"Native provider quote failed owner binding")

    return {
        "mode":"OWNER_VERIFIED_NATIVE_MULTICHAIN_QUOTE_ONLY",
        "asset":asset,"side":side,"stable":stable,
        "source_asset":source,"destination_asset":target,
        "input_amount":amount,
        "estimated_output_amount":quote.get("estimated_output_amount"),
        "quote_checked_at":quote.get("quote_checked_at"),
        "quote_expires_at":quote.get("quote_expires_at"),
        "quote_provider":quote.get("quote_provider"),
        "wallet_identity_verified":True,
        "native_wallet_balance_verified":True,
        "ethereum_settlement_wallet_verified":True,
        "source_chain_balance_verified":True,
        "source_balance_amount":str(available),
        "network_fee_and_xrpl_reserve_spendability_verified":False,
        "signed_source_transaction_available":False,
        "source_receipt_finalized":False,
        "destination_settlement_finalized":False,
        "provider_refund_path_verified":False,
        "wallet_signed":False,"funds_transferred":False,
        "execution_enabled":False,"can_submit":False,
    }
