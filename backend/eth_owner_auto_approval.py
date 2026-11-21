"""Owner-signable capped ERC20 approval plan for real ETH AUTO.

The private-key AUTO signer policy intentionally does NOT include unlimited
ERC20 approve calls. The user explicitly signs bounded approvals in Privy.
No backend signer can use this endpoint to grant ERC20 spending permissions.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal,InvalidOperation
import time

from fastapi import HTTPException

from .eth_privy_policy_plan import fetch_reference_prices,derive_policy_atomic_caps
from .eth_router02_allowance import inspect_router02_approval
from .eth_swaprouter02_preflight import SWAPROUTER02_MAINNET,WETH_MAINNET
from .live_eth_quotes import STABLES
from .live_eth_controls import get_live_eth_limits
from .wallet_balances import verified_addresses
from .privy_wallet_verification import fetch_privy_user
from .storage import _safe_did

TOKENS={"USDC":STABLES["USDC"],"USDT":STABLES["USDT"],"WETH":WETH_MAINNET}


def build_owner_approval_plan(*,token:str,wallet:str,limits:dict,
                              prices:dict,now:int)->dict:
    if token not in TOKENS or type(now) is not int or now<=0:
        raise ValueError("Unsupported owner token approval")
    if (type(wallet) is not str or len(wallet)!=42 or
            not wallet.startswith("0x")
            or any(c not in "0123456789abcdefABCDEF" for c in wallet[2:])):
        raise ValueError("Verified Ethereum wallet required")
    if (limits.get("trading_paused") is not True
            or type(limits.get("revision")) is not int):
        raise ValueError("Pause ETH AUTO before changing token allowances")
    try:
        capital=Decimal(str(limits["planned_capital_usd"]))
    except (InvalidOperation,KeyError,TypeError):
        raise ValueError("Invalid ETH owner capital") from None
    if not capital.is_finite() or capital<=0:
        raise ValueError("Owner must select positive ETH trading capital")
    # No extra order/day ceiling: permit a finite router allowance up to
    # the exact owner's own chosen capital. The remote Privy grant and
    # every actual source wallet balance still bind signed transactions.
    ceiling=capital
    caps=derive_policy_atomic_caps(
        max_order_usd=format(ceiling,"f"),snapshot=prices,now=now)
    field={"USDC":"usdc_max_input_micro","USDT":"usdt_max_input_micro",
           "WETH":"weth_max_input_wei"}[token]
    units=int(caps[field])
    if units<=0 or units>=10**45:
        raise ValueError("Bounded token allowance required")
    return {
        "mode":"ETH_OWNER_LIMITED_ROUTER02_APPROVAL",
        "asset":"ETH","network":"ethereum-mainnet","chain_id":1,
        "wallet_address":wallet.lower(),
        "token":token,"token_contract":TOKENS[token],
        "spender":SWAPROUTER02_MAINNET,"amount_atomic":str(units),
        "approval_cap_usd":format(ceiling,"f"),
        "live_limits_revision":limits["revision"],
        "transaction":{
            "from":wallet.lower(),"to":TOKENS[token],"chainId":1,
            "value":"0x0",
            "data":"0x095ea7b3"+
              SWAPROUTER02_MAINNET[2:].lower().rjust(64,"0")+
              f"{units:064x}",
        },
        "expires_at_unix":now+120,
        "requires_explicit_owner_privy_confirmation":True,
        "transaction_broadcast":False,
        "backend_can_sign":False,
    }


async def make_eth_owner_approval_plan(did:str,*,token:str)->dict:
    did=_safe_did(did)
    if token not in TOKENS:
        raise HTTPException(422,"Unsupported ETH token allowance")
    limits=await asyncio.to_thread(get_live_eth_limits,did)
    if limits.get("trading_paused") is not True:
        raise HTTPException(409,"Pause ETH LIVE before token approval")
    user=await fetch_privy_user(did)
    try:
        wallet=verified_addresses(user,did)["ETH"]
        if wallet is None:
            raise ValueError("Wallet not linked")
    except (ValueError,KeyError,TypeError):
        raise HTTPException(409,"Owner Privy ETH wallet unavailable") from None
    prices=await fetch_reference_prices()
    try:
        return build_owner_approval_plan(
            token=token,wallet=wallet,
            limits=limits,prices=prices,now=int(time.time()))
    except (ValueError,KeyError,TypeError,ArithmeticError):
        raise HTTPException(409,"ETH owner allowance plan cannot be verified") from None
