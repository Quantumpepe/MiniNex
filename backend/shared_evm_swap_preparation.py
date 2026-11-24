"""One owner-bound, exact-amount EVM swap preparation for six MiniNex assets.

The same operation joins a fresh owner-wallet V3 quote to independent
on-chain balance, allowance and gas proof. Unlike disconnected endpoints,
it never describes an unsigned swap as ready when a different chain,
source token, wallet or required amount was reviewed.

This module does NOT sign, broadcast, grant AUTO policies or book profits.
"""
from __future__ import annotations

import asyncio
import time
from fastapi import HTTPException

from .shared_evm_swap_receipts import _pair
from .shared_evm_allowance import check_evm_source_allowance
from .eth_router02_live_quote import make_policy_compatible_eth_quote
from .eth_erc20_trade_quote import quote_owner_erc20_trade
from .native_evm_v3_quote import quote_native_evm_trade

_EVM_ASSETS=frozenset(("ETH","BNB","POL","AVAX","LINK","QNT","UNI","ARB"))


def _approved_quote_pair(quote: object, allowance: object, *,
                         asset: str, side: str, stable: str,
                         amount: str, pair: dict) -> dict:
    """Fail closed on differing owner, route, exact source and amounts."""
    if type(quote) is not dict or type(allowance) is not dict:
        raise ValueError("Missing independent owner wallet proofs")
    tx=quote.get("transaction")
    if (type(tx) is not dict
            or type(tx.get("data")) is not str
            or not tx["data"].lower().startswith("0x04e45aaf")
            or len(tx["data"])!=2+8+7*64
            or type(tx.get("chainId")) is not int
            or tx["chainId"]!=pair["chain_id"]
            or type(tx.get("from")) is not str
            or type(tx.get("to")) is not str
            or tx["to"].lower()!=pair["router"].lower()
            or tx.get("value") not in ("0x0","0")
            or type(allowance.get("wallet_address")) is not str
            or tx["from"].lower()!=allowance["wallet_address"].lower()
            or allowance.get("asset")!=asset
            or allowance.get("network")!=pair["network"]
            or allowance.get("chain_id")!=pair["chain_id"]
            or allowance.get("side")!=side
            or allowance.get("stable")!=stable
            or allowance.get("input_amount")!=amount
            or allowance.get("mode")!="SHARED_EVM_SOURCE_ALLOWANCE_READ_ONLY"
            or allowance.get("wallet_identity_verified") is not True
            or allowance.get("router","").lower()!=pair["router"].lower()
            or allowance.get("source_token_address","").lower()!=pair["source_token"].lower()
            or allowance.get("execution_enabled") is not False
            or allowance.get("can_submit") is not False
            or quote.get("asset")!=asset
            or quote.get("side")!=side
            or quote.get("stable")!=stable
            or quote.get("input_amount")!=amount
            or quote.get("network")!=pair["network"]
            or quote.get("wallet_identity_verified") is not True
            or quote.get("execution_enabled") is not False
            or quote.get("can_submit") is not False):
        raise ValueError("Unmatched owner, quote and on-chain source proof")
    words=[int(tx["data"][10+i*64:10+(i+1)*64],16) for i in range(7)]
    token_in,token_out,fee,recipient,amount_in,min_out,price_limit=words
    required=allowance.get("required_input_atomic")
    if (token_in!=int(pair["source_token"],16)
            or token_out!=int(pair["output_token"],16)
            or fee not in pair["fee_levels"]
            or recipient!=int(tx["from"],16)
            or type(required) is not str or required!=str(amount_in)
            or not 0<amount_in<10**45 or not 0<min_out<10**45
            or price_limit!=0):
        raise ValueError("Quoted EVM ABI differs from verified source balance")
    expires=quote.get("transaction_expires_at")
    if (type(expires) is not int or
            not int(time.time()) < expires <= int(time.time())+45):
        raise ValueError("Current exact-amount swap quote expired")
    if (quote.get("onchain_swap_quote") is not True
            and quote.get("onchain_quote_verified") is not True):
        raise ValueError("Real on-chain DEX quote not independently established")
    source_ok=allowance.get("source_funds_sufficient") is True
    allowed=allowance.get("router_allowance_sufficient") is True
    gas=allowance.get("native_gas_available") is True
    ready=source_ok and allowed and gas
    return {
        "mode":"SHARED_EVM_OWNER_SWAP_PREPARATION",
        "asset":asset,"network":pair["network"],
        "chain_id":pair["chain_id"],
        "side":side,"stable":stable,"input_amount":amount,
        "source_token_address":pair["source_token"],
        "output_token_address":pair["output_token"],
        "router":pair["router"],
        "wallet_address":allowance["wallet_address"],
        "source_funds_sufficient":source_ok,
        "source_balance_atomic":allowance.get("source_balance_atomic"),
        "router_allowance_sufficient":allowed,
        "native_gas_available":gas,
        "owner_approval_transaction":(
            allowance.get("owner_approval_transaction")
            if source_ok and gas and not allowed else None
        ),
        "owner_approval_required":not allowed,
        "wallet_identity_verified":True,
        "exact_swap_abi_and_amount_verified":True,
        "unsigned_transaction":tx if ready else None,
        "transaction_expires_at":quote["transaction_expires_at"] if ready else None,
        "owner_wallet_signing_possible":ready,
        "requires_separate_user_signature":True,
        "native_wrap_unwrap_complete":False,
        "onchain_receipt_finalized":False,
        "auto_signer_grant_verified":False,
        "fee_assessed":False,"real_order_created":False,
        "execution_enabled":False,"can_submit":False,
        "wallet_signed":False,"funds_transferred":False,
    }


async def prepare_owner_evm_swap(
    did: str, *, asset: str, side: str, stable: str, amount: str,
) -> dict:
    if type(asset) is not str or asset not in _EVM_ASSETS:
        raise HTTPException(404,"No supported EVM execution adapter")
    try:
        pair=_pair(asset,stable,side)
    except ValueError:
        raise HTTPException(422,"Unsupported EVM swap pair") from None
    if asset=="ETH":
        quote_fn=make_policy_compatible_eth_quote(
            did,side=side,stable=stable,amount=amount,
            include_user_transaction=True)
    elif asset in ("LINK","QNT","UNI"):
        quote_fn=quote_owner_erc20_trade(
            did,asset=asset,side=side,stable=stable,amount=amount,
            include_user_transaction=True)
    else:
        # ARB is an Arbitrum ERC-20, not a native coin: its verified V3
        # USDC/USDT0 on-chain quote lives in this shared chain-specific
        # quoter, while LINK/UNI quote against Ethereum mainnet above.
        # BNB/POL/AVAX require separate native conversion finality.
        quote_fn=quote_native_evm_trade(
            did,asset=asset,side=side,stable=stable,amount=amount,
            include_user_transaction=True)
    quote,allowance=await asyncio.gather(
        quote_fn,
        check_evm_source_allowance(
            did,asset=asset,side=side,stable=stable,amount=amount),
    )
    if asset=="ETH":
        quote={
            **quote,
            "network":"ethereum-mainnet",
            "transaction":quote.get("transaction"),
            "transaction_expires_at":quote.get("transaction_expires_at"),
        }
    try:
        return _approved_quote_pair(
            quote,allowance,asset=asset,side=side,
            stable=stable,amount=amount,pair=pair)
    except (ValueError,TypeError,KeyError,OverflowError):
        raise HTTPException(502,"EVM swap quote and wallet proofs do not match") from None
