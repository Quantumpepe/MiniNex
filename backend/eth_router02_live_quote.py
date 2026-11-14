"""Read-only, policy-ABI-compatible Ethereum v3 quote and unsigned swap leg.

Instead of the existing UniversalRouter execute(bytes,bytes[],uint256)
route (which does NOT match the drafted Privy exactInputSingle policy), quote
the real mainnet QuoterV2, then build an exactInputSingle call constrained
by the already-tested SwapRouter02 decoder.

This ONLY constructs the ERC20/WETH leg. Native ETH requires a different,
non-atomic wrap/unwrap stage verified after real on-chain receipt. Does NOT
submit approvals, create a Privy policy, delegate a signer or move funds.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
import hashlib
import re
import time

import httpx
from eth_utils import keccak
from fastapi import HTTPException

from .eth_native_weth9_review import native_eth_swap_stages
from .eth_swaprouter02_preflight import (
    EXACT_INPUT_SINGLE_SELECTOR, SWAPROUTER02_MAINNET, WETH_MAINNET,
    inspect_swaprouter02_exact_input,
)
from .live_eth_quotes import ETH_GAS_RESERVE, STABLES, _quantity
from .privy_wallet_verification import fetch_privy_user
from .wallet_balances import ETH_RPC, get_wallet_balances, verified_addresses

QUOTER_V2_MAINNET = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
_QUOTER_SELECTOR = "0x"+keccak(
    text="quoteExactInputSingle((address,address,uint256,uint24,uint160))"
)[:4].hex()
_ALLOWED_FEES=(500,3000)
_USD_BPS_SLIPPAGE=50
_HEX = re.compile(r"^0x[0-9a-fA-F]+$")
_WALLET = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _word(raw: int) -> str:
    if type(raw) is not int or raw<0 or raw>=2**256:
        raise ValueError("Invalid transaction word")
    return f"{raw:064x}"


def _quote_calldata(*, token_in: str, token_out: str,
                    amount: int, fee: int) -> str:
    if (not _WALLET.fullmatch(token_in) or not _WALLET.fullmatch(token_out)
            or fee not in _ALLOWED_FEES or not 0<amount<10**45):
        raise ValueError("Invalid single-pool V3 quote")
    return _QUOTER_SELECTOR+''.join([
        _word(int(token_in,16)),_word(int(token_out,16)),
        _word(amount),_word(fee),_word(0),
    ])


def _decode_quote(raw: object) -> tuple[int,int]:
    """QuoterV2 ABI returns 4 statically encoded words."""
    if (type(raw) is not str or len(raw)!=2+4*64 or
            not _HEX.fullmatch(raw)):
        raise ValueError("Invalid QuoterV2 ABI")
    words = [int(raw[2+i*64:2+(i+1)*64],16) for i in range(4)]
    amount_out, sqrt_price, ticks_crossed, gas_estimate = words
    if (not 0 < amount_out < 10**45 or not 0 < sqrt_price < 2**160
            or ticks_crossed > 2**32-1
            or not 0 < gas_estimate <= 5_000_000):
        raise ValueError("Unusable verified V3 quote")
    return amount_out,gas_estimate


async def _rpc(client: httpx.AsyncClient, *, method: str,
               params: list, req_id: int) -> object:
    response=await client.post(ETH_RPC,json={
        "jsonrpc":"2.0","method":method,"params":params,"id":req_id,
    },headers={"Accept":"application/json","Content-Type":"application/json"})
    response.raise_for_status()
    if len(response.content)>16_384:
        raise ValueError("Oversized Ethereum RPC response")
    body=response.json()
    if (type(body) is not dict or body.get("id")!=req_id
            or body.get("jsonrpc")!="2.0" or "error" in body
            or "result" not in body):
        raise ValueError("Ethereum RPC query mismatch")
    return body["result"]


async def _single_v3_quote(client: httpx.AsyncClient,
                           token_in: str, token_out: str,
                           amount_in: int, fee: int) -> tuple[int,int,int]:
    raw=await _rpc(client,method="eth_call",req_id=fee,params=[{
        "to":QUOTER_V2_MAINNET,
        "data":_quote_calldata(token_in=token_in,
                               token_out=token_out,
                               amount=amount_in,fee=fee),
    },"latest"])
    amount_out,gas_estimate=_decode_quote(raw)
    return fee,amount_out,gas_estimate


def build_unsigned_router02_swap(*, wallet: str, side: str,
                                  stable: str, amount_in: int,
                                  min_out: int, fee: int) -> dict:
    """Prepare precisely one contract call, NO Privy signing or network send."""
    if (type(wallet) is not str or not _WALLET.fullmatch(wallet)
            or stable not in STABLES or side not in ("BUY","SELL")
            or fee not in _ALLOWED_FEES
            or not 0<amount_in<10**45 or not 0<min_out<10**45):
        raise ValueError("Invalid user-owned ETH swap draft")
    token_in=STABLES[stable] if side=="BUY" else WETH_MAINNET
    token_out=WETH_MAINNET if side=="BUY" else STABLES[stable]
    data=EXACT_INPUT_SINGLE_SELECTOR+''.join([
        _word(int(token_in,16)),_word(int(token_out,16)),
        _word(fee),_word(int(wallet,16)),_word(amount_in),
        _word(min_out),_word(0),
    ])
    tx={"from":wallet,"to":SWAPROUTER02_MAINNET,
        "chainId":1,"value":"0","data":data}
    review=inspect_swaprouter02_exact_input(
        tx,wallet=wallet,stable=stable,side=side,
        input_atomic=str(amount_in),minimum_output_atomic=str(min_out))
    if review.get("token_pair_verified") is not True:
        raise ValueError("Router02 review rejected swap call")
    return tx


def _get_balance(value: object) -> Decimal:
    if type(value) is not dict or value.get("status")!="verified":
        raise ValueError("No verified on-chain balance")
    raw=value.get("balance")
    if type(raw) is not str or len(raw)>80:
        raise ValueError("Invalid verified wallet units")
    try:
        amount=Decimal(raw)
    except InvalidOperation:
        raise ValueError("Invalid verified wallet units") from None
    if not amount.is_finite() or amount<0:
        raise ValueError("Invalid verified wallet units")
    return amount


async def make_policy_compatible_eth_quote(
    did: str, *, side: str, stable: str, amount: str,
    include_user_transaction: bool = False,
) -> dict:
    if side not in ("BUY","SELL") or stable not in STABLES:
        raise HTTPException(422,"Unsupported ETH swap pair")
    requested, units = _quantity(amount,6 if side=="BUY" else 18)
    user,balance=await asyncio.gather(
        fetch_privy_user(did),get_wallet_balances(did))
    try:
        wallet=verified_addresses(user,did)["ETH"]
        if (type(wallet) is not str or not _WALLET.fullmatch(wallet)
                or balance.get("wallets",{}).get("ETH","").lower()!=wallet.lower()
                or balance.get("mode")!="VERIFIED_READ_ONLY"
                or balance.get("wallet_ownership_verified") is not True):
            raise ValueError("Wallet identity was not independently verified")
        holdings=balance["balances"]
        native=_get_balance(holdings["ETH"])
        available=_get_balance(holdings[stable if side=="BUY" else "ETH"])
        # In the explicit user-signing route SELL spends WETH (wrapped by
        # the owner in a preceding transaction), not native ETH directly.
        if side=="SELL" and include_user_transaction is True:
            available_native_after_input=native
            available=requested  # Verified real WETH balance below via RPC.
        else:
            available_native_after_input=native-requested if side=="SELL" else native
        if requested>available or available_native_after_input<=ETH_GAS_RESERVE:
            raise HTTPException(409,"Insufficient verified source or ETH gas")
    except (KeyError,TypeError,ValueError,AttributeError):
        raise HTTPException(503,"ETH wallet balance unavailable") from None

    token_in=STABLES[stable] if side=="BUY" else WETH_MAINNET
    token_out=WETH_MAINNET if side=="BUY" else STABLES[stable]
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(9),follow_redirects=False,
        ) as client:
            chain=await _rpc(client,method="eth_chainId",params=[],req_id=1)
            if chain!="0x1":
                raise ValueError("Not Ethereum mainnet")
            if side=="SELL" and include_user_transaction is True:
                # Source is real ERC20 WETH; native ETH alone cannot fund
                # SwapRouter02 exactInputSingle. Read fresh actual balance.
                owner_word=wallet[2:].lower().rjust(64,"0")
                raw_weth=await _rpc(client,method="eth_call",req_id=4,
                    params=[{"to":WETH_MAINNET,
                             "data":"0x70a08231"+owner_word},"latest"])
                if (type(raw_weth) is not str or
                        not re.fullmatch(r"0x[0-9a-fA-F]{64}",raw_weth)):
                    raise ValueError("Unverified WETH source balance")
                if int(raw_weth,16)<int(units):
                    raise HTTPException(409,"Wrap native ETH into WETH before SELL")
            candidates=await asyncio.gather(*(
                _single_v3_quote(client,token_in,token_out,int(units),fee)
                for fee in _ALLOWED_FEES
            ),return_exceptions=True)
        options=[result for result in candidates
                 if isinstance(result,tuple)]
        if not options:
            raise ValueError("No viable actual V3 liquidity route")
        # Max output first; equal output favors less quoted gas, then fee.
        fee,expected,quote_gas=max(options,key=lambda q:(q[1],-q[2],-q[0]))
        minimum=max(1,expected*(10_000-_USD_BPS_SLIPPAGE)//10_000)
        tx=build_unsigned_router02_swap(
            wallet=wallet,side=side,stable=stable,
            amount_in=int(units),min_out=minimum,fee=fee)
    except (httpx.HTTPError,ValueError,TypeError,OverflowError):
        raise HTTPException(502,"ETH Router02 mainnet quote unavailable") from None

    stages=native_eth_swap_stages(side=side,stable=stable)
    # Never export executable calldata or claim a signed route from this API.
    return {
        **({
            "transaction": {
                "from": tx["from"], "to": tx["to"],
                "chainId": tx["chainId"],
                "value": "0x0", "data": tx["data"],
            },
            "transaction_expires_at": int(time.time()) + 15,
            "requires_user_wallet_signature": True,
            "requires_separate_erc20_approval": True,
            "native_eth_settlement_complete": False,
        } if include_user_transaction is True else {}),
        "mode":"ETH_ROUTER02_POLICY_ABI_QUOTE_ONLY",
        "asset":"ETH","network":"ethereum-mainnet",
        "side":side,"stable":stable,"input_amount":amount,
        "route":"UNISWAP_V3_SINGLE_POOL",
        "quote_fee_tier":fee,
        "expected_swap_output_amount":format(
            Decimal(expected)/Decimal(10**(18 if side=="BUY" else 6)),"f"),
        "minimum_swap_output_amount":format(
            Decimal(minimum)/Decimal(10**(18 if side=="BUY" else 6)),"f"),
        "swap_output_token":"WETH" if side=="BUY" else stable,
        "native_eth_cycle_stages":stages["stages"],
        "native_eth_cycle_non_atomic":True,
        "wallet_identity_verified":True,
        "onchain_swap_quote":True,
        "swap_call_abi":"exactInputSingle",
        "router":SWAPROUTER02_MAINNET,
        "unsigned_swap_hash":hashlib.sha256(bytes.fromhex(tx["data"][2:])).hexdigest(),
        "unsigned_swap_validated":True,
        "approval_required_not_yet_checked":True,
        "native_wrap_unwrap_not_yet_settled":True,
        "independent_gas_estimate_not_yet_verified":True,
        "quote_ttl_seconds":15,"quoted_at":int(time.time()),
        "privy_policy_attached":False,
        "privy_signer_authorized":False,
        "can_submit":False,"execution_enabled":False,
        "wallet_signed":False,"funds_transferred":False,
    }
