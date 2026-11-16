"""Canonical finalized WETH9-compatible native conversion proofs on four EVM chains.

Reads real Privy-owned Ethereum, BNB, Polygon and Avalanche receipts. Strict
chain, contract, caller, amount, event, transaction block and finalized
checkpoint checks. A finalized wrap/unwrap is NOT a completed trade, does
not grant a Privy signer or authorize an automatic transaction.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
import os
import re

import httpx
from fastapi import HTTPException

from .eth_weth9_receipts import (
    _requested_amount, _DEPOSIT_TOPIC, _WITHDRAW_TOPIC,
)
from .live_eth_receipts import _num
from .multicoin_evm_balances import _RPC
from .shared_evm_swap_receipts import _pair, _receipt_rpc
from .privy_wallet_verification import fetch_privy_user
from .wallet_balances import verified_addresses

_HASH=re.compile(r"^0x[0-9a-fA-F]{64}$")
_ADDR=re.compile(r"^0x[0-9a-fA-F]{40}$")
_NATIVE=("ETH","BNB","POL","AVAX")
_STABLE={"ETH":"USDC","BNB":"USDT","POL":"USDC","AVAX":"USDC"}


def _native_pair(asset:str)->dict:
    if asset not in _NATIVE or type(asset) is not str:
        raise ValueError("Unsupported native chain")
    # SELL's canonical source is always the native chain wrapped ERC20.
    return _pair(asset,_STABLE[asset],"SELL")


def _conversion_event_amount(logs:object,*,wallet:str,wrapped:str,
                             operation:str)->int:
    if type(logs) is not list or len(logs)>400:
        raise ValueError("Untrusted native conversion receipt logs")
    topic=_DEPOSIT_TOPIC if operation=="DEPOSIT" else _WITHDRAW_TOPIC
    found=[]
    for entry in logs:
        if type(entry) is not dict:
            raise ValueError("Malformed native chain event")
        address=entry.get("address")
        if type(address) is not str or not _ADDR.fullmatch(address):
            raise ValueError("Unverified EVM event contract")
        if address.lower()!=wrapped.lower():
            continue
        topics=entry.get("topics")
        if type(topics) is not list or not topics:
            raise ValueError("Missing wrapped native event topics")
        if type(topics[0]) is not str or topics[0].lower()!=topic:
            continue
        if (len(topics)!=2 or type(topics[1]) is not str
                or not _HASH.fullmatch(topics[1])
                or int(topics[1],16)!=int(wallet,16)):
            raise ValueError("Unowned native conversion event")
        data=entry.get("data")
        if type(data) is not str or not _HASH.fullmatch(data):
            raise ValueError("Invalid native conversion event units")
        amount=int(data,16)
        if not 0<amount<10**45:
            raise ValueError("Unbounded native conversion quantity")
        found.append(amount)
    if len(found)!=1:
        raise ValueError("Expected one wallet-owned native conversion event")
    return found[0]


def inspect_finalized_native_evm_conversion(
    tx_hash:str,*,asset:str,operation:str,wallet:str,
    chain_id:object,transaction:object,receipt:object,
    latest:object,finalized:object,
)->dict:
    if (type(tx_hash) is not str or not _HASH.fullmatch(tx_hash)
            or type(wallet) is not str or not _ADDR.fullmatch(wallet)
            or operation not in ("DEPOSIT","WITHDRAW")):
        raise ValueError("Invalid owner native conversion request")
    pair=_native_pair(asset)
    if _num(chain_id)!=pair["chain_id"]:
        raise ValueError("Wrong EVM native conversion chain")
    head=_num(latest)
    wrapped=pair["source_token"]
    result={
        "mode":"SHARED_FINALIZED_NATIVE_EVM_CONVERSION",
        "asset":asset,"network":pair["network"],"chain_id":pair["chain_id"],
        "tx_hash":tx_hash.lower(),"wallet_address":wallet.lower(),
        "operation":operation,
        "wrapped_token_address":wrapped,
        "state":"NOT_FOUND","transaction_seen":False,
        "transaction_finalized":False,
        "confirmed_native_conversion":False,
        "amount_native":None,"network_fee_native":None,
        "confirmations":0,
        "linked_trade_order":False,
        "full_native_trade_cycle_settled":False,
        "privy_signer_grant_verified":False,
        "can_submit":False,"execution_enabled":False,
    }
    if transaction is None:
        if receipt is not None:
            raise ValueError("Orphan native transaction receipt")
        return result
    if (type(transaction) is not dict
            or str(transaction.get("hash","")).lower()!=tx_hash.lower()
            or str(transaction.get("from","")).lower()!=wallet.lower()
            or str(transaction.get("to","")).lower()!=wrapped.lower()
            or _num(transaction.get("chainId"))!=pair["chain_id"]):
        raise ValueError("Owner, chain, wrapped contract or hash mismatch")
    atomic=_requested_amount(transaction,operation)
    result["transaction_seen"]=True
    result["state"]="PENDING"
    if receipt is None:
        if (transaction.get("blockHash") not in (None,"0x")
                or transaction.get("blockNumber") is not None):
            raise ValueError("Inconsistent pending native conversion")
        return result
    if (type(receipt) is not dict
            or str(receipt.get("transactionHash","")).lower()!=tx_hash.lower()
            or str(receipt.get("from","")).lower()!=wallet.lower()
            or str(receipt.get("to","")).lower()!=wrapped.lower()):
        raise ValueError("Owner native conversion receipt mismatch")
    block_hash=receipt.get("blockHash")
    if type(block_hash) is not str or not _HASH.fullmatch(block_hash):
        raise ValueError("Invalid native conversion block hash")
    block=_num(receipt.get("blockNumber"))
    if (not 0<block<=head
            or str(transaction.get("blockHash","")).lower()!=block_hash.lower()
            or _num(transaction.get("blockNumber"))!=block):
        raise ValueError("Native conversion transaction and receipt differ")
    status=_num(receipt.get("status"))
    gas=_num(receipt.get("gasUsed"))
    price=_num(receipt.get("effectiveGasPrice"))
    if status not in (0,1) or not 0<gas<=30_000_000 or not 0<price<=10**12:
        raise ValueError("Invalid conversion status or gas")
    result["network_fee_native"]=format(Decimal(gas*price)/Decimal(10**18),"f")
    result["block_number"]=block
    result["confirmations"]=min(head-block+1,1_000_000)
    result["state"]="INCLUDED_SUCCESS" if status else "INCLUDED_REVERTED"
    if finalized is None:
        return result
    if type(finalized) is not dict:
        raise ValueError("Malformed EVM finalized checkpoint")
    checkpoint=_num(finalized.get("number"))
    if checkpoint>head:
        raise ValueError("Finalized checkpoint beyond chain tip")
    if checkpoint<block:
        return result
    result["transaction_finalized"]=True
    if status==0:
        result["state"]="FINALIZED_REVERTED"
        return result
    observed=_conversion_event_amount(
        receipt.get("logs"),wallet=wallet,wrapped=wrapped,
        operation=operation)
    if observed!=atomic:
        raise ValueError("Native conversion event differs from calldata")
    result["state"]="FINALIZED_NATIVE_CONVERSION"
    result["confirmed_native_conversion"]=True
    result["amount_native"]=format(Decimal(observed)/Decimal(10**18),"f")
    return result


async def check_user_native_evm_conversion(
    did:str,tx_hash:str,*,asset:str,operation:str,
)->dict:
    if type(tx_hash) is not str or not _HASH.fullmatch(tx_hash):
        raise HTTPException(422,"Invalid native EVM transaction hash")
    try:
        pair=_native_pair(asset)
    except ValueError:
        raise HTTPException(422,"Unsupported native EVM conversion") from None
    if operation not in ("DEPOSIT","WITHDRAW"):
        raise HTTPException(422,"Unsupported native EVM conversion direction")
    user=await fetch_privy_user(did)
    try:
        wallet=verified_addresses(user,did)["ETH"]
        if type(wallet) is not str or not _ADDR.fullmatch(wallet):
            raise ValueError("Owner Privy EVM wallet missing")
    except (ValueError,KeyError,TypeError):
        raise HTTPException(503,"Verified owner EVM wallet required") from None
    chain,network,fallback,env=(_RPC[asset] if asset!="ETH" else _RPC["ETH_TOKENS"])
    if chain!=pair["chain_id"] or network!=pair["network"]:
        raise HTTPException(503,"Wrong configured chain registry")
    url=os.getenv(env,"").strip() or fallback
    if not url.startswith("https://") or any(ch in url for ch in " \n\r\t#@"):
        raise HTTPException(503,"HTTPS EVM network RPC required")
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(9),follow_redirects=False,
        ) as client:
            chain_id,tx,receipt,head=await asyncio.gather(
                _receipt_rpc(client,url,"eth_chainId",[],1),
                _receipt_rpc(client,url,"eth_getTransactionByHash",[tx_hash],2),
                _receipt_rpc(client,url,"eth_getTransactionReceipt",[tx_hash],3),
                _receipt_rpc(client,url,"eth_blockNumber",[],4),
            )
            final=(await _receipt_rpc(
                client,url,"eth_getBlockByNumber",["finalized",False],5)
                if receipt is not None else None)
        return inspect_finalized_native_evm_conversion(
            tx_hash,asset=asset,operation=operation,wallet=wallet,
            chain_id=chain_id,transaction=tx,receipt=receipt,
            latest=head,finalized=final)
    except (httpx.HTTPError,ValueError,TypeError,OverflowError):
        raise HTTPException(502,"Native EVM conversion receipt unverified") from None
