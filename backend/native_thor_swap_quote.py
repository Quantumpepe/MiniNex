"""Private, destination-bound THORNode quote terms for BTC/SOL/XRP.

The older public MCP quote contains no usable vault/memo. A live on-chain
swap needs those exact time-limited terms and current Asgard vault evidence.
This module reads them; it NEVER creates an executable transaction, signs,
broadcasts, charges a fee or publishes a vault/memo to the app.
"""
from __future__ import annotations

from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation
import hashlib
import json
import re
import time

import httpx
from fastapi import HTTPException

from .manual_swap_live_quotes import validate_live_quote_pair
from .wallet_balances import get_wallet_balances, verified_addresses
from .native_coin_balances import get_native_balances, verified_native_addresses
from .privy_wallet_verification import fetch_privy_user
from .storage import _safe_did

_API="https://gateway.liquify.com/chain/thorchain_api"
_ASSET_CHAIN={"BTC":"BTC","SOL":"SOL","XRP":"XRP"}
_ASSET_NETWORK={"BTC":"bitcoin-mainnet","SOL":"solana-mainnet",
                "XRP":"xrpl-mainnet"}
_INTEGER=re.compile(r"^[0-9]{1,28}$")


def _exact_owner_wallets(user:dict,did:str,asset:str)->tuple[str,str]:
    """Return native + Ethereum owner wallet, not external or ambiguous wallets."""
    if not isinstance(user,dict) or user.get("id")!=did:
        raise ValueError("Wrong Privy wallet user")
    eth=verified_addresses(user,did)["ETH"]
    if not eth:
        raise ValueError("Owner Ethereum settlement wallet missing")
    if asset=="BTC":
        accounts=user.get("linked_accounts")
        if not isinstance(accounts,list):
            raise ValueError("Missing original native BTC wallet identity")
        candidates={
            item.get("address") for item in accounts
            if isinstance(item,dict) and item.get("type")=="wallet"
               and item.get("chain_type")=="bitcoin-segwit"
               and item.get("wallet_client_type",item.get("wallet_client"))=="privy"
               and isinstance(item.get("address"),str)
        }
        btc=verified_addresses(user,did)["BTC"]
        if not btc or candidates!={btc}:
            raise ValueError("Native Bitcoin wallet not independently Privy-owned")
        native=btc
    else:
        native=verified_native_addresses(user,did)[asset]
        if not native:
            raise ValueError("Current native Privy wallet not uniquely owned")
    return native,eth


def _read_owner_balance(asset:str,side:str,stable:str,amount:str,
                        evm:dict,native:dict|None)->None:
    source=(evm.get("balances",{}).get(stable) if side=="BUY" else
            evm.get("balances",{}).get("BTC") if asset=="BTC" else
            (native or {}).get("assets",{}).get(asset))
    if (not isinstance(source,dict) or source.get("status")!="verified"
            or evm.get("wallet_ownership_verified") is not True):
        raise ValueError("Source mainnet balance unverified")
    if asset!="BTC" and side=="SELL" and (
            not isinstance(native,dict)
            or native.get("wallet_ownership_verified") is not True
            or source.get("network")!=_ASSET_NETWORK[asset]):
        raise ValueError("Unverified native source custody")
    try:
        value=Decimal(source.get("balance"))
        needed=Decimal(amount)
    except (InvalidOperation,TypeError,ValueError):
        raise ValueError("Malformed exact source wallet balance") from None
    if not value.is_finite() or not needed.is_finite() or needed<=0 or needed>value:
        raise ValueError("Original wallet source funds unavailable")


def validate_thornode_route_terms(
    *,asset:str,side:str,stable:str,amount:str,native_owner:str,
    eth_owner:str,quote:dict,inbounds:list,now:int,
)->dict:
    """Strict quote-vs-active-vault matching. No user-selected destination."""
    if asset not in _ASSET_CHAIN or side not in ("BUY","SELL"):
        raise ValueError("Native source chain unsupported")
    if stable not in ("USDC","USDT") or (asset=="XRP" and stable!="USDC"):
        raise ValueError("Native settlement stable invalid")
    src=stable if side=="BUY" else asset
    dst=asset if side=="BUY" else stable
    from_coin,to_coin,units=validate_live_quote_pair(src,dst,amount)
    chain="ETH" if side=="BUY" else _ASSET_CHAIN[asset]
    target=native_owner if side=="BUY" else eth_owner
    if (type(quote) is not dict or type(inbounds) is not list
            or not isinstance(now,int) or now<=0
            or type(quote.get("inbound_address")) is not str):
        raise ValueError("Unverified THORNode swap quote")
    expiry=quote.get("expiry")
    if type(expiry) is not int or not now<expiry<=now+600:
        raise ValueError("THORNode route already expired")
    if (not _INTEGER.fullmatch(str(quote.get("expected_amount_out","")))
            or int(quote["expected_amount_out"])<=0):
        raise ValueError("No verified destination liquidity")
    min_input=quote.get("recommended_min_amount_in")
    if not isinstance(min_input,str) or not _INTEGER.fullmatch(min_input):
        raise ValueError("Unverifiable provider minimum native amount")
    if int(units)<int(min_input):
        raise ValueError("Native source amount below network venue minimum")
    fees=quote.get("fees")
    if not isinstance(fees,dict) or not _INTEGER.fullmatch(str(fees.get("total",""))):
        raise ValueError("Original venue costs unavailable")
    # The exact current Asgard vault MUST match the quote's inbound address.
    candidates=[entry for entry in inbounds if
        isinstance(entry,dict) and entry.get("chain")==chain
        and isinstance(entry.get("address"),str)
        and entry["address"].lower()==quote["inbound_address"].lower()]
    if len(candidates)!=1:
        raise ValueError("Quoted inbound vault differs from active THORNode vault")
    active=candidates[0]
    for name in ("halted","global_trading_paused","chain_trading_paused"):
        if active.get(name) is not False:
            raise ValueError("THORChain source network not verifiably active")
    if side=="BUY" and not isinstance(active.get("router"),str):
        raise ValueError("ERC-20 inbound requires a current THORChain router")

    memo=quote.get("memo")
    if (type(memo) is not str or not 5<len(memo.encode("utf-8"))<=250
            or any(c in memo for c in "\r\n\x00")):
        raise ValueError("Unsupported original THORChain swap memo")
    parts=memo.split(":")
    refund_owner=eth_owner if side=="BUY" else native_owner
    fields=parts[2].split("/") if len(parts)>=3 else []
    target_matches=(fields[0]==target if side=="BUY" else
                    fields[0].lower()==target.lower()) if len(fields)==2 else False
    refund_matches=(fields[1].lower()==refund_owner.lower() if side=="BUY"
                    else fields[1]==refund_owner) if len(fields)==2 else False
    if (len(parts)<3 or parts[0].upper() not in ("=","SWAP","S")
            or parts[1].upper()!=to_coin.upper()
            or not target_matches or not refund_matches):
        raise ValueError("Wrong user destination or original source refund in THORChain memo")
    if side=="SELL" and asset=="BTC" and len(memo.encode("utf-8"))>80:
        # Extended multi-output OP_RETURN requires its own verified encoder.
        # Do not misrepresent a longer memo as a standard BTC transaction.
        memo_needs_extension=True
    else:
        memo_needs_extension=False
    return {
        "mode":"THORCHAIN_DESTINATION_BOUND_READ_ONLY",
        "source_asset":src,"destination_asset":dst,
        "amount":amount,"from_network":(
            "ethereum-mainnet" if side=="BUY" else _ASSET_NETWORK[asset]),
        "to_network":(
            _ASSET_NETWORK[asset] if side=="BUY" else "ethereum-mainnet"),
        "source_wallet":eth_owner if side=="BUY" else native_owner,
        "destination_wallet":target,
        "inbound_vault":quote["inbound_address"],
        "memo":memo,
        "quote_expires_at":datetime.fromtimestamp(expiry,tz=timezone.utc),
        "expected_output_units_1e8":quote["expected_amount_out"],
        "venue_fee_units_1e8":str(fees["total"]),
        "original_vault_verified":True,
        "required_chain_memo_verified":True,
        "exact_venue_fees_verified":True,
        "refund_path_verified":True,
        "needs_extended_btc_memo":memo_needs_extension,
        "needs_erc20_router":side=="BUY",
        "transaction_encoding_verified":False,
        "privy_native_signer_verified":False,
        "source_transaction_sent":False,
        "destination_settlement_finalized":False,
        "execution_enabled":False,"can_submit":False,
    }


async def get_private_native_thornode_terms(
    did:str,*,asset:str,side:str,stable:str,amount:str,
)->dict:
    """Fetch owner-bound protocol terms privately; not exposed as a public API."""
    did=_safe_did(did)
    if asset not in _ASSET_CHAIN or side not in ("BUY","SELL"):
        raise HTTPException(422,"Unsupported native trading asset")
    if stable not in ("USDC","USDT") or (asset=="XRP" and stable!="USDC"):
        raise HTTPException(422,"Unsupported native source stable")
    src=stable if side=="BUY" else asset
    dst=asset if side=="BUY" else stable
    from_coin,to_coin,units=validate_live_quote_pair(src,dst,amount)
    user=await fetch_privy_user(did)
    try:
        native_owner,eth_owner=_exact_owner_wallets(user,did,asset)
    except (ValueError,TypeError,KeyError):
        raise HTTPException(409,"Current Privy owner wallet unavailable") from None
    evm=await get_wallet_balances(did)
    native=await get_native_balances(did) if asset!="BTC" else None
    try:
        _read_owner_balance(asset,side,stable,amount,evm,native)
    except ValueError:
        raise HTTPException(409,"Original source balance insufficient or unverified") from None
    destination=native_owner if side=="BUY" else eth_owner
    refund_owner=eth_owner if side=="BUY" else native_owner
    params={"from_asset":from_coin,"to_asset":to_coin,
            "amount":units,"destination":destination,
            "refund_address":refund_owner}
    try:
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(9),follow_redirects=False) as client:
            rsp=await client.get(_API+"/thorchain/quote/swap",params=params)
            rsp.raise_for_status()
            if len(rsp.content)>40_000:
                raise ValueError("Oversized native quote")
            quote=rsp.json()
            active=await client.get(_API+"/thorchain/inbound_addresses")
            active.raise_for_status()
            if len(active.content)>250_000:
                raise ValueError("Oversized vault state")
            inbound=active.json()
        parsed=validate_thornode_route_terms(
            asset=asset,side=side,stable=stable,amount=amount,
            native_owner=native_owner,eth_owner=eth_owner,
            quote=quote,inbounds=inbound,now=int(time.time()))
    except (httpx.HTTPError,ValueError,TypeError,KeyError,OverflowError):
        raise HTTPException(503,"Validated original THORChain route unavailable") from None
    return parsed
