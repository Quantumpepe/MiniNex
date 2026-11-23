"""LINK and UNI use the exact same Ethereum V3 quote/ABI execution core as ETH.

This module adds actual owner-wallet-bound Router02 unsigned ERC-20 trades.
It does not sign, approve, broadcast or mark a trade filled. Any live sender
must separately verify owner approval, allowance, policy and durable journal.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
import hashlib
import re
import time

import httpx
from fastapi import HTTPException

from .eth_router02_live_quote import (
    _rpc, _single_v3_quote, _get_balance, _ALLOWED_FEES,
    _USD_BPS_SLIPPAGE,
)
from .eth_swaprouter02_preflight import SWAPROUTER02_MAINNET
from .live_eth_quotes import STABLES, ETH_GAS_RESERVE, _quantity
from .multicoin_evm_balances import _ETH_TOKENS
from .privy_wallet_verification import fetch_privy_user
from .wallet_balances import get_wallet_balances, verified_addresses
from .shared_evm_execution import EvmPair, unsigned_exact_input_single
from .live_asset_registry import get_asset

_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_WORD = re.compile(r"^0x[0-9a-fA-F]{64}$")


def build_erc20_router02_swap(*, asset: str, wallet: str, side: str,
                              stable: str, input_atomic: int,
                              minimum_output_atomic: int, fee: int) -> dict:
    """One verified server-owned token pair, no arbitrary user contracts."""
    if (asset not in ("LINK", "QNT", "UNI") or side not in ("BUY", "SELL")
            or stable not in get_asset(asset).stablecoin_candidates):
        raise ValueError("Unsupported Ethereum ERC-20 trade")
    token = _ETH_TOKENS[asset]
    pair = EvmPair(
        asset=asset,chain_id=1,network="ethereum-mainnet",
        token_in=STABLES[stable] if side=="BUY" else token,
        token_out=token if side=="BUY" else STABLES[stable],
        router=SWAPROUTER02_MAINNET,fee=fee,
    )
    return unsigned_exact_input_single(
        pair=pair,wallet=wallet,amount_in=input_atomic,
        min_out=minimum_output_atomic,
    )


async def quote_owner_erc20_trade(
    did: str, *, asset: str, side: str, stable: str, amount: str,
    include_user_transaction: bool = False,
) -> dict:
    """Actual V3 on-chain quote for a Privy-owned LINK/QNT/UNI ERC-20 route.

    The returned unsigned transaction is NOT an authorized AUTO order.
    Signer/policy/allowance checks and actual finality are separate stages.
    """
    if (asset not in ("LINK", "QNT", "UNI") or side not in ("BUY", "SELL")
            or stable not in get_asset(asset).stablecoin_candidates):
        raise HTTPException(422, "Unsupported owner ERC-20 trade pair")
    requested, units = _quantity(amount,6 if side=="BUY" else 18)
    user, balance = await asyncio.gather(
        fetch_privy_user(did),get_wallet_balances(did),
    )
    try:
        wallet=verified_addresses(user,did)["ETH"]
        if (type(wallet) is not str or not _ADDR.fullmatch(wallet)
                or balance.get("wallet_ownership_verified") is not True
                or balance.get("mode")!="VERIFIED_READ_ONLY"
                or balance.get("wallets",{}).get("ETH","").lower()!=wallet.lower()):
            raise ValueError("Not an independently verified Privy wallet")
        holdings=balance["balances"]
        native=_get_balance(holdings["ETH"])
        if native <= ETH_GAS_RESERVE:
            raise HTTPException(409, "Insufficient ETH for network gas")
        if side=="BUY" and requested>_get_balance(holdings[stable]):
            raise HTTPException(409, "Insufficient verified settlement balance")
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(503,"Ethereum ERC-20 wallet verification unavailable") from None

    token=_ETH_TOKENS[asset]
    token_in=STABLES[stable] if side=="BUY" else token
    token_out=token if side=="BUY" else STABLES[stable]
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(9),follow_redirects=False,
        ) as client:
            chain=await _rpc(client,method="eth_chainId",params=[],req_id=1)
            if chain != "0x1":
                raise ValueError("Not Ethereum mainnet")
            if side=="SELL":
                # Owner's actual ERC-20 source balance, not a PAPER position.
                owner_word=wallet[2:].lower().rjust(64,"0")
                raw=await _rpc(client,method="eth_call",req_id=4,params=[{
                    "to":token,"data":"0x70a08231"+owner_word
                },"latest"])
                if type(raw) is not str or not _WORD.fullmatch(raw):
                    raise ValueError("Invalid ERC-20 balance proof")
                if int(raw,16)<int(units):
                    raise HTTPException(409,"Insufficient verified ERC-20 token")
            options=await asyncio.gather(*(
                _single_v3_quote(client,token_in,token_out,int(units),fee)
                for fee in _ALLOWED_FEES
            ),return_exceptions=True)
        candidates=[v for v in options if type(v) is tuple]
        if not candidates:
            raise ValueError("No actual on-chain V3 liquidity quote")
        fee,expected,gas=max(candidates,key=lambda x:(x[1],-x[2],-x[0]))
        minimum=max(1,expected*(10_000-_USD_BPS_SLIPPAGE)//10_000)
        tx=build_erc20_router02_swap(
            asset=asset,wallet=wallet,side=side,stable=stable,
            input_atomic=int(units),minimum_output_atomic=minimum,fee=fee,
        )
    except (httpx.HTTPError,ValueError,TypeError,OverflowError):
        raise HTTPException(502,"Verified LINK/QNT/UNI V3 quote unavailable") from None

    return {
        **({
            "transaction":{
                "from":wallet,"to":tx["to"],"chainId":1,
                "value":"0x0","data":tx["data"],
            },
            "transaction_expires_at":int(time.time())+40,
            "requires_owner_wallet_signature":True,
            "requires_separate_erc20_approval":True,
        } if include_user_transaction else {}),
        "mode":"SHARED_EVM_ERC20_ROUTER02_QUOTE",
        "asset":asset,"network":"ethereum-mainnet","chain_id":1,
        "side":side,"stable":stable,"input_amount":amount,
        "quote_fee_tier":fee,
        "expected_output_amount":format(
            Decimal(expected)/Decimal(10**(18 if side=="BUY" else 6)),"f"),
        "minimum_output_amount":format(
            Decimal(minimum)/Decimal(10**(18 if side=="BUY" else 6)),"f"),
        "router":SWAPROUTER02_MAINNET,
        "unsigned_swap_hash":hashlib.sha256(bytes.fromhex(tx["data"][2:])).hexdigest(),
        "wallet_identity_verified":True,
        "wallet_funds_verified":True,
        "onchain_quote_verified":True,
        "quote_ttl_seconds":40 if include_user_transaction else 15,
        "allowance_not_yet_verified":True,
        "network_gas_spent":False,
        "execution_enabled":False,
        "can_submit":False,
        "wallet_signed":False,
        "funds_transferred":False,
    }
