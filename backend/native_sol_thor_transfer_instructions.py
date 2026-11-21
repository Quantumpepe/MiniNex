"""Exact unsigned Solana SystemTransfer + THORChain Memo instruction plan.

This is a pure, deterministic instruction constructor for a future Privy
authorized message compiler. No signing, broadcast, hidden wallet creation,
retry or spending. A verified recent blockhash and current chain fee must
be re-read by the eventual execution worker immediately before signing.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import base64
import hashlib
import json
import re
import struct

from .native_coin_balances import _decode_b58,_BASE58,_valid_address
from .manual_swap_live_quotes import THOR_ASSETS

_SOL_SYSTEM_PROGRAM="11111111111111111111111111111111"
_SOL_MEMO_PROGRAM="MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
_AMOUNT=re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_INTEGER=re.compile(r"^(?:0|[1-9][0-9]*)$")


def _atomic(value:object,label:str,*,allow_zero:bool=False)->int:
    if type(value) is not str or len(value)>30 or not _INTEGER.fullmatch(value):
        raise ValueError("Invalid finalized Solana "+label)
    n=int(value)
    if n<0 or (n==0 and not allow_zero) or n>=2**64:
        raise ValueError("Solana "+label+" out of range")
    return n


def build_unsigned_sol_thor_source_instructions(
    quote:object,*,amount:str,now:int,
    source_account:object,network_terms:object,
)->dict:
    """Return exact program IDs and raw instruction bytes, not an executable TX."""
    if (type(quote) is not dict or type(source_account) is not dict
            or type(network_terms) is not dict
            or type(amount) is not str or len(amount)>55
            or not _AMOUNT.fullmatch(amount)
            or type(now) is not int or now<=0):
        raise ValueError("Invalid native Solana funding request")
    try:
        decimal_amount=Decimal(amount)
        lamports=decimal_amount*Decimal(10**9)
    except (InvalidOperation,ValueError):
        raise ValueError("Solana native amount cannot be represented exactly") from None
    if (not decimal_amount.is_finite() or decimal_amount<=0
            or lamports!=lamports.to_integral_value()
            or not 0<lamports<2**64):
        raise ValueError("Solana input amount precision invalid")
    owner=quote.get("source_wallet")
    vault=quote.get("inbound_vault")
    receiver=quote.get("destination_wallet")
    memo=quote.get("memo")
    expiry=quote.get("quote_expires_at")
    if (quote.get("mode")!="THORCHAIN_DESTINATION_BOUND_READ_ONLY"
            or quote.get("source_asset")!="SOL"
            or quote.get("destination_asset") not in ("USDC","USDT")
            or quote.get("from_network")!="solana-mainnet"
            or quote.get("to_network")!="ethereum-mainnet"
            or quote.get("amount")!=amount
            or not _valid_address("SOL",owner)
            or not _valid_address("SOL",vault)
            or owner==vault
            or type(receiver) is not str
            or not re.fullmatch(r"0x[0-9a-fA-F]{40}",receiver)
            or type(memo) is not str
            or not 6<len(memo.encode("utf-8"))<=250
            or any(char in memo for char in "\r\n\x00")
            or type(expiry) is not datetime or expiry.tzinfo is None
            or not now<expiry.timestamp()<=now+600
            or quote.get("original_vault_verified") is not True
            or quote.get("refund_path_verified") is not True
            or quote.get("required_chain_memo_verified") is not True
            or quote.get("exact_venue_fees_verified") is not True
            or quote.get("transaction_encoding_verified") is not False
            or quote.get("source_transaction_sent") is not False
            or quote.get("can_submit") is not False
            or quote.get("execution_enabled") is not False):
        raise ValueError("Untrusted Solana source or swap quote")
    parts=memo.split(":")
    destination=parts[2].split("/") if len(parts)>=3 else []
    if (len(parts)<3 or parts[0].upper() not in ("=","S","SWAP")
            or parts[1].upper()!=THOR_ASSETS[quote["destination_asset"]][0].upper()
            or len(destination)!=2
            or destination[0].lower()!=receiver.lower()
            or destination[1]!=owner):
        raise ValueError("Solana THORChain memo redirects destination/refund")

    if (source_account.get("owner")!=owner
            or source_account.get("network")!="solana-mainnet"
            or source_account.get("commitment")!="finalized"
            or type(source_account.get("slot")) is not int
            or source_account["slot"]<1):
        raise ValueError("Unverified Solana source ownership snapshot")
    balance=_atomic(source_account.get("balance_lamports"),"wallet balance",
                    allow_zero=True)
    blockhash=network_terms.get("recent_blockhash")
    if (type(blockhash) is not str
            or len(_decode_b58(blockhash,_BASE58) or b"")!=32
            or type(network_terms.get("context_slot")) is not int
            or network_terms["context_slot"]<source_account["slot"]
            or type(network_terms.get("last_valid_block_height")) is not int
            or network_terms["last_valid_block_height"]<1
            or type(network_terms.get("current_block_height")) is not int
            or not 0<network_terms["current_block_height"]<
            network_terms["last_valid_block_height"]):
        raise ValueError("Recent finalized Solana blockhash unavailable")
    fee=_atomic(network_terms.get("fee_lamports"),"network fee",
                allow_zero=True)
    if int(lamports)+fee>balance:
        raise ValueError("Solana source fee exceeds confirmed wallet balance")
    transfer=struct.pack("<IQ",2,int(lamports))
    memo_bytes=memo.encode("utf-8")
    instructions=[
        {
            "program_id":_SOL_SYSTEM_PROGRAM,
            "accounts":[
                {"pubkey":owner,"signer":True,"writable":True},
                {"pubkey":vault,"signer":False,"writable":True},
            ],
            "data_base64":base64.b64encode(transfer).decode("ascii"),
        },
        {
            "program_id":_SOL_MEMO_PROGRAM,
            "accounts":[{"pubkey":owner,"signer":True,"writable":False}],
            "data_base64":base64.b64encode(memo_bytes).decode("ascii"),
        },
    ]
    fingerprint=hashlib.sha256(json.dumps(
        [owner,vault,receiver,amount,blockhash,
         network_terms["last_valid_block_height"],
         int(expiry.timestamp()),instructions],
        sort_keys=True,separators=(",",":"),ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    return {
        "mode":"ORIGINAL_SOL_THORCHAIN_UNSIGNED_INSTRUCTIONS",
        "asset":"SOL","side":"SELL",
        "source_wallet":owner,"inbound_vault":vault,
        "destination_wallet":receiver,
        "source_amount_lamports":str(int(lamports)),
        "source_balance_lamports":str(balance),
        "network_fee_lamports":str(fee),
        "recent_blockhash":blockhash,
        "last_valid_block_height":network_terms["last_valid_block_height"],
        "quote_expires_at":expiry.isoformat(),
        "instructions":instructions,
        "unsigned_instructions_sha256":fingerprint,
        "original_quote_verified":True,
        "source_fee_and_balance_checked":True,
        "message_compiled":False,
        "current_privy_policy_verified":False,
        "wallet_signed":False,
        "ready_to_submit":False,
        "source_transaction_sent":False,
        "execution_enabled":False,"can_submit":False,
    }
