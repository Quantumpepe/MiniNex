"""Exact unsigned Ethereum ERC20 THORChain source deposit for native BUY.

Internal-only chain-specific transaction encoding. No Privy approval, signer
grant, token transfer, broadcast or CREATE ORDER. This code cannot call a
wallet. A future executor must independently reverify quote freshness,
original user permission, ERC20 allowance, gas, nonce and original journal.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from eth_abi import encode
from eth_utils import keccak

from .live_eth_quotes import STABLES
from .manual_swap_live_quotes import THOR_ASSETS

_ASSETS=frozenset(("BTC","SOL","XRP","TRX","DOGE"))
_WALLET=re.compile(r"^0x[a-fA-F0-9]{40}$")
_AMOUNT=re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_SELECTOR=keccak(text="depositWithExpiry(address,address,uint256,string,uint256)")[:4]
_MIN_ROUTER_EXPIRY_SECONDS=60*60


def encode_original_eth_native_buy_deposit(
    quote:object,*,asset:str,stable:str,amount:str,now:int,
)->dict:
    """Produce the exact unsigned source tx, refusing unsafe route ambiguity.

    The real quote TTL controls *when to submit*. The Router expiry is a
    separate one-hour-plus protocol parameter for when a mined tx expires.
    """
    if (asset not in _ASSETS or stable not in STABLES
            or (asset=="XRP" and stable!="USDC")
            or type(now) is not int or now<=0
            or type(amount) is not str or not _AMOUNT.fullmatch(amount)
            or len(amount)>55 or type(quote) is not dict):
        raise ValueError("Invalid native BUY calldata request")
    try:
        units=Decimal(amount)*Decimal(10**6)
    except (InvalidOperation,ValueError):
        raise ValueError("Cannot encode original ERC20 stable amount") from None
    if (not units.is_finite() or units<=0
            or units!=units.to_integral_value()):
        raise ValueError("ERC20 stable input cannot be encoded exactly")
    quote_end=quote.get("quote_expires_at")
    if (not isinstance(quote_end,datetime) or quote_end.tzinfo is None
            or not now<quote_end.timestamp()<=now+600):
        raise ValueError("Original THORChain route quote is expired or stale")
    source=quote.get("source_wallet")
    receiver=quote.get("destination_wallet")
    vault=quote.get("inbound_vault")
    router=quote.get("inbound_router")
    memo=quote.get("memo")
    if (quote.get("mode")!="THORCHAIN_DESTINATION_BOUND_READ_ONLY"
            or quote.get("source_asset")!=stable
            or quote.get("destination_asset")!=asset
            or quote.get("amount")!=amount
            or quote.get("from_network")!="ethereum-mainnet"
            or quote.get("to_network")!={
                "BTC":"bitcoin-mainnet","SOL":"solana-mainnet",
                "XRP":"xrpl-mainnet","TRX":"tron-mainnet",
                "DOGE":"dogecoin-mainnet",
            }[asset]
            or any(type(x) is not str or not _WALLET.fullmatch(x)
                   for x in (source,vault,router))
            or type(receiver) is not str
            or receiver==source or receiver==vault or receiver==router
            or type(memo) is not str
            or not 7<=len(memo.encode("utf-8"))<=250
            or any(c in memo for c in "\r\n\x00")
            or quote.get("original_vault_verified") is not True
            or quote.get("refund_path_verified") is not True
            or quote.get("required_chain_memo_verified") is not True
            or quote.get("exact_venue_fees_verified") is not True
            or quote.get("needs_erc20_router") is not True
            or quote.get("transaction_encoding_verified") is not False
            or quote.get("source_transaction_sent") is not False
            or quote.get("execution_enabled") is not False
            or quote.get("can_submit") is not False):
        raise ValueError("Untrusted native ETH router deposit quote")
    parts=memo.split(":")
    destination=parts[2].split("/") if len(parts)>=3 else []
    if (len(parts)<3 or parts[0].upper() not in ("=","S","SWAP")
            or parts[1].upper()!=THOR_ASSETS[asset][0].upper()
            or len(destination)!=2 or destination[0]!=receiver
            or destination[1].lower()!=source.lower()):
        raise ValueError("Original BUY destination/refund not owner-bound")
    # THORChain Router expiry is separate from the venue quote's expiry.
    router_expiry=now+90*60
    if router_expiry<now+_MIN_ROUTER_EXPIRY_SECONDS:
        raise ValueError("Router expiry shorter than required protocol window")
    encoded=_SELECTOR+encode(
        ["address","address","uint256","string","uint256"],
        [vault,STABLES[stable],int(units),memo,router_expiry],
    )
    tx={
        "from":source,"to":router,"chainId":1,
        "value":"0x0","data":"0x"+encoded.hex(),
    }
    fingerprint=hashlib.sha256(json.dumps(
        [asset,stable,amount,source,receiver,vault,router,memo,
         int(quote_end.timestamp()),router_expiry,tx["data"]],
        separators=(",",":"),ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    return {
        "mode":"THORCHAIN_ETH_ORIGINAL_BUY_UNSIGNED_PREVIEW",
        "asset":asset,"stable":stable,
        "source_network":"ethereum-mainnet",
        "target_network":quote["to_network"],
        "source_wallet":source,"destination_wallet":receiver,
        "router":router,"vault":vault,
        "source_amount_atomic":str(int(units)),
        "original_quote_expires_at":quote_end.isoformat(),
        "router_expires_at":router_expiry,
        "unsigned_transaction":tx,
        "unsigned_transaction_sha256":fingerprint,
        "original_quote_verified":True,
        "source_wallet_fee_balance_verified":False,
        "token_allowance_or_permit_verified":False,
        "remote_privy_owner_grant_verified":False,
        "gas_and_nonce_current":False,
        "ready_to_sign":False,
        "execution_enabled":False,"can_submit":False,
        "wallet_signed":False,"source_transaction_sent":False,
    }
