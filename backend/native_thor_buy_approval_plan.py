"""Read-only exact ERC20 approval preparation for native BTC/SOL/XRP BUY.

First native THORChain BUY from Ethereum USDC/USDT may need the ORIGINAL
Privy wallet's exact finite token allowance to the currently verified router.
This only constructs an ERC20 approval PREVIEW, NEVER sends or signs one.
The quote may expire while an approval mines; source BUY must be re-quoted
and independently re-verified afterwards. Never approve an arbitrary
spender, unlimited amount, unrelated token or another wallet.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import time

import httpx
from fastapi import HTTPException

from .eth_router02_live_quote import _rpc
from .live_eth_quotes import STABLES
from .native_eth_native_buy_preflight import _int_quantity, _WORD, _allowance_data
from .native_eth_thor_deposit_tx import encode_original_eth_native_buy_deposit
from .native_thor_swap_quote import get_private_native_thornode_terms
from .shared_evm_allowance import owner_approval_draft
from .storage import _safe_did

_ASSETS=frozenset(("BTC","SOL","XRP"))


def inspect_original_thor_buy_allowance(
    *,quote:object,unsigned:object,asset:str,stable:str,
    amount:str,now:int,chain_id:object,token_balance:object,
    token_allowance:object,eth_balance:object,
)->dict:
    """Return current owner approval need without granting any authority."""
    if (asset not in _ASSETS or stable not in STABLES
            or (asset=="XRP" and stable!="USDC")
            or type(now) is not int or now<=0
            or type(quote) is not dict or type(unsigned) is not dict):
        raise ValueError("Unverified original native approval context")
    # The original quote already came from the private owner-bound THOR
    # reader; reconstruct the same server-only ABI and refuse all mutation.
    expected=encode_original_eth_native_buy_deposit(
        quote,asset=asset,stable=stable,amount=amount,now=now)
    if unsigned!=expected:
        raise ValueError("Native approval does not bind original private quote")
    if _int_quantity(chain_id)!=1:
        raise ValueError("Original ERC20 approval cannot leave Ethereum mainnet")
    if (type(token_balance) is not str or not _WORD.fullmatch(token_balance)
            or type(token_allowance) is not str
            or not _WORD.fullmatch(token_allowance)):
        raise ValueError("Original owner's ERC20 funding or allowance unavailable")
    token_units=int(token_balance,16)
    allowance=int(token_allowance,16)
    eth_wei=_int_quantity(eth_balance)
    required=int(unsigned["source_amount_atomic"])
    expiry=quote.get("quote_expires_at")
    if (required<=0 or token_units<required or eth_wei<=0
            or not isinstance(expiry,datetime) or expiry.tzinfo is None
            or expiry.timestamp()<=now):
        raise ValueError("Original native BUY funding or current quote unavailable")
    needs_approval=allowance<required
    # This is a finite EXACT-amount ERC20 approve, not unlimited allowance.
    # The original contract/router/wallet are those independently verified
    # against THORNode and Privy in the private quote source.
    approval=(owner_approval_draft(
        token=STABLES[stable],router=unsigned["router"],
        wallet=unsigned["source_wallet"],chain_id=1,
        amount_atomic=required) if needs_approval else None)
    # USDT may require approve(0) before changing an existing nonzero
    # allowance. Both transactions need independently finalized receipts;
    # neither is signed or sent by this read-only owner plan.
    reset_required=bool(stable=="USDT" and 0<allowance<required)
    reset=({**approval,"data":approval["data"][:-64]+"0"*64}
           if reset_required else None)
    return {
        "mode":"THORCHAIN_ORIGINAL_ERC20_APPROVAL_PREVIEW_ONLY",
        "asset":asset,"side":"BUY","stable":stable,
        "network":"ethereum-mainnet","chain_id":1,
        "wallet_address":unsigned["source_wallet"],
        "official_stable_token_address":STABLES[stable],
        "original_router":unsigned["router"],
        "original_inbound_vault":unsigned["vault"],
        "original_quote_expires_at":unsigned["original_quote_expires_at"],
        "original_buy_unsigned_sha256":unsigned["unsigned_transaction_sha256"],
        "source_balance_atomic":str(token_units),
        "existing_router_allowance_atomic":str(allowance),
        "approval_amount_atomic":str(required),
        "source_eth_gas_balance_wei":str(eth_wei),
        "owner_wallet_needs_erc20_approval":needs_approval,
        "existing_usdt_allowance_requires_zero_reset":reset_required,
        "exact_owner_approval_reset_transaction":reset,
        "exact_owner_approval_transaction":approval,
        "required_sequential_approval_receipts":(
            2 if reset_required else 1 if needs_approval else 0),
        "exact_approval_gas_verified":False,
        "current_remote_privy_trade_policy_verified":False,
        "durable_approval_send_claimed":False,
        "real_token_approval_sent":False,
        "actual_buy_source_sent":False,
        "source_trade_receipt_finalized":False,
        "source_trade_refund_verified":False,
        "execution_enabled":False,"can_submit":False,
    }


async def read_original_thor_buy_allowance(
    did:str,*,asset:str,stable:str,amount:str,
)->dict:
    """Three owner-scoped live RPC reads; never transfer or authorize funds."""
    did=_safe_did(did)
    if (asset not in _ASSETS or stable not in STABLES
            or (asset=="XRP" and stable!="USDC")):
        raise HTTPException(422,"Unsupported original native approval pair")
    quote=await get_private_native_thornode_terms(
        did,asset=asset,side="BUY",stable=stable,amount=amount)
    try:
        now=int(time.time())
        unsigned=encode_original_eth_native_buy_deposit(
            quote,asset=asset,stable=stable,amount=amount,now=now)
        wallet=unsigned["source_wallet"]
        token=STABLES[stable]
        owner_word=wallet[2:].lower().rjust(64,"0")
        approved=_allowance_data(wallet,unsigned["router"])
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(9),follow_redirects=False,
        ) as client:
            chain=await _rpc(client,method="eth_chainId",params=[],req_id=1)
            if _int_quantity(chain)!=1:
                raise ValueError("Wrong original THORChain source network")
            height=_int_quantity(await _rpc(
                client,method="eth_blockNumber",params=[],req_id=2))
            if height<=0:
                raise ValueError("Original Ethereum source block unavailable")
            pinned=hex(height)
            balance,allowance,eth_balance=await asyncio.gather(
                _rpc(client,method="eth_call",params=[
                    {"to":token,"data":"0x70a08231"+owner_word},pinned],
                    req_id=3),
                _rpc(client,method="eth_call",params=[
                    {"to":token,"data":approved},pinned],req_id=4),
                _rpc(client,method="eth_getBalance",
                    params=[wallet,pinned],req_id=5),
            )
        if int(time.time())>=quote["quote_expires_at"].timestamp():
            raise ValueError("Original THORChain allowance quote expired")
        return inspect_original_thor_buy_allowance(
            quote=quote,unsigned=unsigned,asset=asset,stable=stable,
            amount=amount,now=now,chain_id=chain,
            token_balance=balance,token_allowance=allowance,
            eth_balance=eth_balance)
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError,
            httpx.HTTPError):
        raise HTTPException(
            409,"Original native BUY ERC20 approval status unavailable") from None
