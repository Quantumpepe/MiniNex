"""Shared on-chain EVM source balance and exact Router02 allowance for six coins.

Common read-only owner-wallet verification; per-network token contracts,
chain IDs and decimals are taken exclusively from the server's route registry.
Provides optional bounded owner-signable approval calldata without sending it.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
import os
import re

import httpx
from fastapi import HTTPException

from .live_eth_quotes import _quantity
from .multicoin_evm_balances import _RPC
from .native_evm_v3_quote import _rpc, _decode_quantity
from .privy_wallet_verification import fetch_privy_user
from .wallet_balances import verified_addresses
from .shared_evm_swap_receipts import _pair

_ADDR=re.compile(r"^0x[0-9a-fA-F]{40}$")
_APPROVE_SELECTOR="0x095ea7b3"
_BALANCE_SELECTOR="0x70a08231"
_ALLOWANCE_SELECTOR="0xdd62ed3e"


def _word(address: str) -> str:
    if type(address) is not str or not _ADDR.fullmatch(address):
        raise ValueError("Unverified EVM wallet or spender")
    return f"{int(address,16):064x}"


def owner_approval_draft(*, token: str, router: str, wallet: str,
                         chain_id: int, amount_atomic: int) -> dict:
    """One exact-amount token approval, strictly to the verified Router02."""
    if (type(chain_id) is not int
            or type(amount_atomic) is not int
            or not 0<amount_atomic<10**45):
        raise ValueError("Invalid approval chain or atomic value")
    _word(token);_word(router);_word(wallet)
    return {
        "from":wallet,"to":token,"chainId":chain_id,
        "value":"0x0",
        "data":_APPROVE_SELECTOR+_word(router)+f"{amount_atomic:064x}",
    }


async def check_evm_source_allowance(
    did: str, *, asset: str, side: str, stable: str, amount: str,
) -> dict:
    try:
        pair=_pair(asset,stable,side)
    except ValueError:
        raise HTTPException(422,"Unsupported EVM trade pair") from None
    decimals=pair["source_decimals"]
    _,amount_atomic=_quantity(amount,decimals)
    user=await fetch_privy_user(did)
    try:
        wallet=verified_addresses(user,did)["ETH"]
        if type(wallet) is not str or not _ADDR.fullmatch(wallet):
            raise ValueError("Privy EVM owner address unavailable")
    except (KeyError,TypeError,ValueError):
        raise HTTPException(503,"Verified EVM owner wallet required") from None
    source=pair["source_token"]
    router=pair["router"]
    if asset in _RPC:
        chain,network,fallback,env=_RPC[asset]
    else:
        chain,network,fallback,env=_RPC["ETH_TOKENS"]
    if (chain!=pair["chain_id"] or network!=pair["network"]):
        raise HTTPException(503,"Verified chain RPC route mismatch")
    url=os.getenv(env,"").strip() or fallback
    if not url.startswith("https://") or any(ch in url for ch in " \t\n#@"):
        raise HTTPException(503,"HTTPS network RPC required")

    owner_word=_word(wallet)
    spender_word=_word(router)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(9),follow_redirects=False,
        ) as client:
            chain_hex=await _rpc(client,url,"eth_chainId",[],1)
            if _decode_quantity(chain_hex)!=chain:
                raise ValueError("Wrong actual EVM network")
            balances=await asyncio.gather(
                _rpc(client,url,"eth_call",[
                    {"to":source,
                     "data":_BALANCE_SELECTOR+owner_word},"latest"],2),
                _rpc(client,url,"eth_call",[
                    {"to":source,
                     "data":_ALLOWANCE_SELECTOR+owner_word+spender_word},"latest"],3),
                _rpc(client,url,"eth_getBalance",[wallet,"latest"],4),
            )
        balance=_decode_quantity(balances[0],word=True)
        allowance=_decode_quantity(balances[1],word=True)
        native=_decode_quantity(balances[2])
    except (httpx.HTTPError,ValueError,TypeError,OverflowError):
        raise HTTPException(502,"Verified EVM source allowance unavailable") from None

    needed=int(amount_atomic)
    source_ok=balance>=needed
    allowance_ok=allowance>=needed
    draft=(owner_approval_draft(
        token=source,router=router,wallet=wallet,
        chain_id=chain,amount_atomic=needed
    ) if not allowance_ok else None)
    return {
        "mode":"SHARED_EVM_SOURCE_ALLOWANCE_READ_ONLY",
        "asset":asset,"network":network,"chain_id":chain,
        "side":side,"stable":stable,"input_amount":amount,
        "source_token_address":source,
        "router":router,"wallet_address":wallet.lower(),
        "source_balance_atomic":str(balance),
        "router_allowance_atomic":str(allowance),
        "required_input_atomic":amount_atomic,
        "source_balance":format(Decimal(balance)/(Decimal(10)**decimals),"f"),
        "router_allowance":format(Decimal(allowance)/(Decimal(10)**decimals),"f"),
        "source_funds_sufficient":source_ok,
        "router_allowance_sufficient":allowance_ok,
        "native_gas_balance_atomic":str(native),
        "native_gas_available":native>0,
        "manual_owner_approval_required":not allowance_ok,
        "owner_approval_transaction":draft,
        "manual_owner_approval_not_submitted":True,
        "wallet_identity_verified":True,
        "privy_auto_grant_authorized":False,
        "native_wrap_unwrap_settled":False,
        "execution_enabled":False,"can_submit":False,
    }
