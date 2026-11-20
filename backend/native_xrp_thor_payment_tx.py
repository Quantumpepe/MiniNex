"""Exact unsigned XRPL Payment for native XRP -> Ethereum stablecoin SELL.

XRP Memos must carry THORChain's exact quote memo, and an active Asgard
destination is mandatory. Pure deterministic serialization for a future
Privy-authorized submitter; neither private key nor broadcast capability here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from .native_coin_balances import _valid_address
from .manual_swap_live_quotes import THOR_ASSETS

_ASSET="XRP"
_DECIMAL=re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_DROPS=re.compile(r"^(?:0|[1-9][0-9]*)$")
_ETH=re.compile(r"^0x[0-9a-fA-F]{40}$")
_MAX_LEDGER=2**32-1


def _positive_integer(value:object, label:str, *,allow_zero:bool=False)->int:
    if (type(value) is not str or len(value)>28
            or not _DROPS.fullmatch(value)):
        raise ValueError("Malformed XRPL "+label)
    amount=int(value)
    if amount<0 or (amount==0 and not allow_zero) or amount>=10**25:
        raise ValueError("Invalid XRPL "+label)
    return amount


def build_original_unsigned_xrp_thor_payment(
    quote:object,*,amount:str,now:int,
    account_info:object,network_terms:object,
)->dict:
    """Validate an owner wallet + quote + recent network state, then encode.

    Caller must obtain account_info and network_terms from separately verified
    current XRP mainnet RPC. This function grants no spend authority.
    """
    if (type(quote) is not dict or type(account_info) is not dict
            or type(network_terms) is not dict
            or type(amount) is not str or len(amount)>54
            or not _DECIMAL.fullmatch(amount)
            or type(now) is not int or now<=0):
        raise ValueError("Invalid XRPL original payment request")
    try:
        number=Decimal(amount)
        unit=number*Decimal(10**6)
    except (InvalidOperation,ValueError):
        raise ValueError("XRP input quantity not exactly payable") from None
    if (not number.is_finite() or number<=0
            or unit!=unit.to_integral_value() or unit>=10**25):
        raise ValueError("XRP source amount precision or magnitude invalid")
    owner=quote.get("source_wallet")
    stable=quote.get("destination_asset")
    receiver=quote.get("destination_wallet")
    vault=quote.get("inbound_vault")
    memo=quote.get("memo")
    expiry=quote.get("quote_expires_at")
    if (quote.get("mode")!="THORCHAIN_DESTINATION_BOUND_READ_ONLY"
            or quote.get("source_asset")!="XRP"
            or stable!="USDC"
            or quote.get("from_network")!="xrpl-mainnet"
            or quote.get("to_network")!="ethereum-mainnet"
            or quote.get("amount")!=amount
            or not _valid_address("XRP",owner)
            or not _valid_address("XRP",vault)
            or not _ETH.fullmatch(receiver or "")
            or owner==vault
            or type(memo) is not str
            or len(memo.encode("utf-8"))>250
            or any(char in memo for char in "\r\n\x00")
            or type(expiry) is not datetime or expiry.tzinfo is None
            or not now<expiry.timestamp()<=now+600
            or quote.get("original_vault_verified") is not True
            or quote.get("required_chain_memo_verified") is not True
            or quote.get("refund_path_verified") is not True
            or quote.get("exact_venue_fees_verified") is not True
            or quote.get("transaction_encoding_verified") is not False
            or quote.get("source_transaction_sent") is not False
            or quote.get("execution_enabled") is not False
            or quote.get("can_submit") is not False):
        raise ValueError("XRP THORChain quote not bound to original owner")
    parts=memo.split(":")
    destination_and_refund=parts[2].split("/") if len(parts)>=3 else []
    if (len(parts)<3 or parts[0].upper() not in ("=","SWAP","S")
            or parts[1].upper()!=THOR_ASSETS[stable][0].upper()
            or len(destination_and_refund)!=2
            or destination_and_refund[0].lower()!=receiver.lower()
            or destination_and_refund[1]!=owner):
        raise ValueError("XRP swap/refund memo is not owner-bound")

    info=account_info.get("result")
    if (type(info) is not dict or info.get("validated") is not True
            or info.get("status")!="success"
            or type(info.get("account_data")) is not dict
            or info["account_data"].get("Account")!=owner):
        raise ValueError("Original XRP account state not validated")
    acct=info["account_data"]
    bal=_positive_integer(acct.get("Balance"),"confirmed balance",allow_zero=True)
    sequence=acct.get("Sequence")
    owner_count=acct.get("OwnerCount")
    ledger=info.get("ledger_index")
    if (type(sequence) is not int or sequence<1
            or type(owner_count) is not int or owner_count<0
            or type(ledger) is not int or not 0<ledger<_MAX_LEDGER-40):
        raise ValueError("XRPL sequence or confirmed ledger state missing")

    state_ledger=network_terms.get("validated_ledger_index")
    if (type(state_ledger) is not int or state_ledger!=ledger):
        raise ValueError("XRPL validated ledger checkpoint changed")
    fee=_positive_integer(network_terms.get("network_fee_drops"),"network fee")
    base_reserve=_positive_integer(
        network_terms.get("reserve_base_drops"),"base reserve")
    owner_reserve=_positive_integer(
        network_terms.get("reserve_increment_drops"),"owner reserve",
        allow_zero=True)
    reserve=base_reserve+owner_count*owner_reserve
    payable=int(unit)
    if bal-payable-fee<reserve:
        raise ValueError("XRP account reserve/fee not covered by source balance")

    ledger_expiry=ledger+20
    payment={
        "TransactionType":"Payment",
        "Flags":0x80000000,  # Fully canonical signature; no additional payment flags.
        "Account":owner,"Destination":vault,
        "Amount":str(payable),"Fee":str(fee),
        "Sequence":sequence,"LastLedgerSequence":ledger_expiry,
        "Memos":[{"Memo":{"MemoData":memo.encode("utf-8").hex().upper()}}],
    }
    if "DestinationTag" in payment:
        raise ValueError("Cannot infer a destination tag from a wallet address")
    fingerprint=hashlib.sha256(json.dumps(
        [payment,stable,receiver,int(expiry.timestamp()),reserve],
        sort_keys=True,separators=(",",":"),ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    return {
        "mode":"ORIGINAL_XRP_THORCHAIN_UNSIGNED_PAYMENT",
        "asset":"XRP","side":"SELL",
        "source_wallet":owner,"destination_wallet":receiver,
        "inbound_vault":vault,"source_amount_drops":str(payable),
        "network_fee_drops":str(fee),
        "account_reserve_drops":str(reserve),
        "account_sequence":sequence,
        "account_validated_ledger":ledger,
        "quote_expires_at":expiry.isoformat(),
        "unsigned_payment":payment,
        "unsigned_payment_sha256":fingerprint,
        "original_quote_verified":True,
        "source_balance_and_reserve_checked":True,
        "destination_tag_required_verified":False,
        "fresh_privy_policy_verified":False,
        "wallet_signed":False,
        "ready_to_submit":False,
        "source_transaction_sent":False,
        "can_submit":False,"execution_enabled":False,
    }
