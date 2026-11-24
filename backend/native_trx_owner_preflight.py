"""Original TRX SELL source construction, with exact signed-byte binding.

TRON CreateTransaction accepts extra_data as the hexadecimal memo. Rebuild
the expected protobuf raw_data from the restricted transfer fields before
any later signer sees its hash. This module never signs or broadcasts.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from datetime import datetime
from fastapi import HTTPException
import httpx

from .native_trx_user_signed_send import _tron_rpc, _check_unsigned_transaction
from .native_trx_manual_send import tron_address_hex, trx_to_sun
from .native_thor_swap_quote import get_private_native_thornode_terms, _exact_owner_wallets
from .privy_wallet_verification import fetch_privy_user
from .manual_swap_live_quotes import THOR_ASSETS
from .native_coin_balances import _valid_address
from .storage import _safe_did


def _varint(number):
    if type(number) is not int or not 0<=number<2**63:
        raise ValueError("Invalid TRON integer")
    result=bytearray()
    while number>=128:
        result.append((number&127)|128)
        number>>=7
    result.append(number)
    return bytes(result)


def _number(field,value):
    return _varint(field<<3)+_varint(value)


def _bytes(field,value):
    return _varint((field<<3)|2)+_varint(len(value))+value


def _hex(value,size):
    if type(value) is not str or len(value)!=2*size or not re.fullmatch(r"[0-9a-fA-F]+",value):
        raise ValueError("Invalid TRON reference")
    return bytes.fromhex(value)


def verify_tron_swap_wire(tx,*,source,destination,sun,memo):
    """Check both node JSON and the exact bytes that will be signed."""
    txid,unsigned=_check_unsigned_transaction(tx,source,destination,sun)
    raw=unsigned["raw_data"]
    if set(raw)!={"contract","ref_block_bytes","ref_block_hash","expiration","timestamp","data"}:
        raise ValueError("Unexpected original TRON raw fields")
    memo_bytes=memo.encode("utf-8")
    if not memo_bytes or not 5<len(memo_bytes)<=250 or _hex(raw["data"],len(memo_bytes))!=memo_bytes:
        raise ValueError("Original TRON swap memo changed")
    entry=raw["contract"][0]
    if set(entry)!={"type","parameter"} or set(entry["parameter"])!={"type_url","value"}:
        raise ValueError("Unexpected original TRON transfer parameters")
    if entry["parameter"]["type_url"]!="type.googleapis.com/protocol.TransferContract":
        raise ValueError("Original TRON contract type changed")
    if set(entry["parameter"]["value"])!={"owner_address","to_address","amount"}:
        raise ValueError("Unexpected original TRON transfer fields")
    transfer=(_bytes(1,bytes.fromhex(tron_address_hex(source)))+
              _bytes(2,bytes.fromhex(tron_address_hex(destination)))+_number(3,sun))
    any_message=_bytes(1,b"type.googleapis.com/protocol.TransferContract")+_bytes(2,transfer)
    contract=_number(1,1)+_bytes(2,any_message)
    expected=(_bytes(1,_hex(raw["ref_block_bytes"],2))+
              _bytes(4,_hex(raw["ref_block_hash"],8))+
              _number(8,raw["expiration"])+_bytes(10,memo_bytes)+
              _bytes(11,contract)+_number(14,raw["timestamp"]))
    if bytes.fromhex(unsigned["raw_data_hex"])!=expected:
        raise ValueError("Original TRON signed bytes differ from owner transfer")
    if hashlib.sha256(expected).hexdigest().upper()!=txid:
        raise ValueError("Original TRON transaction digest changed")
    return txid,unsigned


def _nonnegative_int(value):
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError("Invalid TRON resource value")
    return value


def _trx_fee_snapshot(unsigned, *, source, vault, sun, parameters, sender, recipient, resources):
    """Estimate the one-signature transfer; actual fees require a solid receipt."""
    if (sender.get("address", "").upper() != tron_address_hex(source)
            or recipient.get("address", "").upper() != tron_address_hex(vault)
            or "Error" in resources or "error" in resources):
        raise ValueError("TRON sender or active vault unavailable")
    chain_parameters = parameters.get("chainParameter")
    if type(chain_parameters) is not list:
        raise ValueError("TRON chain fees unavailable")
    fees = {}
    for item in chain_parameters:
        if type(item) is dict and item.get("key") in ("getTransactionFee", "getMemoFee"):
            key = item["key"]
            if key in fees:
                raise ValueError("Duplicate TRON chain fee")
            fees[key] = _nonnegative_int(item.get("value", 0))
    if set(fees) != {"getTransactionFee", "getMemoFee"}:
        raise ValueError("TRON chain fees unavailable")
    # Transaction protobuf raw_data + one 65-byte signature + 64-byte result allowance.
    bandwidth = len(_bytes(1, bytes.fromhex(unsigned["raw_data_hex"]))) + len(_bytes(2, bytes(65))) + 64
    staked = max(0, _nonnegative_int(resources.get("NetLimit", 0)) -
                 _nonnegative_int(resources.get("NetUsed", 0)))
    free = max(0, _nonnegative_int(resources.get("freeNetLimit", 0)) -
               _nonnegative_int(resources.get("freeNetUsed", 0)))
    balance = _nonnegative_int(sender.get("balance", 0))
    memo_fee = fees["getMemoFee"]
    full_burn = bandwidth * fees["getTransactionFee"]
    estimated = memo_fee + (0 if max(staked, free) >= bandwidth else full_burn)
    # Free bandwidth is also subject to a shared network quota. Keep a
    # full-burn fallback estimate separately; never report it as a paid fee.
    reserve = memo_fee + full_burn
    if balance < sun + reserve:
        raise HTTPException(409, "Insufficient TRX for transfer and network fee reserve")
    return {"source_balance_sun": str(balance), "bandwidth_required": bandwidth,
            "staked_bandwidth_available": staked, "free_bandwidth_available": free,
            "memo_fee_sun": str(memo_fee), "estimated_source_fee_sun": str(estimated),
            "source_fee_reserve_sun": str(reserve), "fee_reserve_verified": True,
            "actual_source_fee_sun": None}


async def prepare_original_trx_unsigned_owner_transfer(did,*,stable,amount,
        _private_quote=None,_internal_bundle=False):
    did=_safe_did(did)
    if stable!="USDT" or (_private_quote is not None and not _internal_bundle):
        raise HTTPException(422,"Unsupported TRON settlement request")
    quote=_private_quote if _private_quote is not None else await get_private_native_thornode_terms(
        did,asset="TRX",side="SELL",stable=stable,amount=amount)
    source,eth=_exact_owner_wallets(await fetch_privy_user(did),did,"TRX")
    vault=quote.get("inbound_vault")
    memo=quote.get("memo")
    end=quote.get("quote_expires_at")
    now=int(time.time())
    if (quote.get("mode")!="THORCHAIN_DESTINATION_BOUND_READ_ONLY"
            or quote.get("source_asset")!="TRX" or quote.get("destination_asset")!=stable
            or quote.get("from_network")!="tron-mainnet" or quote.get("to_network")!="ethereum-mainnet"
            or quote.get("source_wallet")!=source or quote.get("destination_wallet")!=eth
            or quote.get("amount")!=amount or not _valid_address("TRX",vault) or vault==source
            or quote.get("inbound_router") is not None
            or type(end) is not datetime or end.tzinfo is None or not now<end.timestamp()<=now+600
            or any(quote.get(k) is not True for k in ("original_vault_verified",
                "required_chain_memo_verified","refund_path_verified","exact_venue_fees_verified"))
            or any(quote.get(k) is not False for k in ("source_transaction_sent","execution_enabled","can_submit"))
            or type(memo) is not str or not 5<len(memo.encode("utf-8"))<=250
            or any(c in memo for c in ("\r","\n","\x00"))):
        raise HTTPException(409,"Original TRON quote unavailable")
    parts=memo.split(":")
    destinations=parts[2].split("/") if len(parts)>=3 else []
    if (len(parts)<3 or parts[0].upper() not in ("=","S","SWAP")
            or parts[1].upper()!=THOR_ASSETS[stable][0].upper()
            or len(destinations)!=2 or destinations[0].lower()!=eth.lower() or destinations[1]!=source):
        raise HTTPException(409,"Original TRON swap destination changed")
    sun=trx_to_sun(amount)
    async with httpx.AsyncClient(timeout=httpx.Timeout(9),follow_redirects=False) as client:
        tx,parameters,sender,recipient,resources=await asyncio.gather(
            _tron_rpc(client,"/wallet/createtransaction",{
            "owner_address":tron_address_hex(source),"to_address":tron_address_hex(vault),
            "amount":sun,"visible":False,"extra_data":memo.encode("utf-8").hex()}),
            _tron_rpc(client,"/wallet/getchainparameters",{}),
            _tron_rpc(client,"/wallet/getaccount",{"address":tron_address_hex(source),"visible":False}),
            _tron_rpc(client,"/wallet/getaccount",{"address":tron_address_hex(vault),"visible":False}),
            _tron_rpc(client,"/wallet/getaccountresource",{"address":tron_address_hex(source),"visible":False}),
        )
    try:
        txid,unsigned=verify_tron_swap_wire(tx,source=source,destination=vault,sun=sun,memo=memo)
        fee_snapshot=_trx_fee_snapshot(unsigned,source=source,vault=vault,sun=sun,
            parameters=parameters,sender=sender,recipient=recipient,resources=resources)
        if time.time()>=end.timestamp():
            raise ValueError("Original quote expired")
    except (ValueError,KeyError,TypeError,OverflowError):
        raise HTTPException(409,"Original TRON unsigned transaction unavailable") from None
    return {"mode":"ORIGINAL_TRX_SOURCE_UNSIGNED_PREFLIGHT","asset":"TRX","side":"SELL","stable":stable,
            "source_wallet":source,"destination_wallet":eth,"inbound_vault":vault,
            "source_amount_sun":str(sun),"original_transaction_sha256":txid.lower(),
            "unsigned_transaction":unsigned,**fee_snapshot,
            "wallet_signed":False,"source_transaction_sent":False,"execution_enabled":False,"can_submit":False,
            **({"_private_quote":quote} if _internal_bundle else {})}
