"""Finalized Ethereum WETH9 native deposit/withdraw event observation.

A native ETH trade through SwapRouter02 requires WETH9 wrap or unwrap on one
side. This checks a real owner-wallet WETH9 tx + emitted Deposit/Withdrawal
event after finalization. It never assumes the two separate transactions
comprise one atomic swap; linking them into a single LIVE order still needs a
durable executor and state machine, Privy policies and independent reconciliation.
No signer, private key, ERC20 approval, trade order write or broadcast.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
import re

import httpx
from eth_utils import keccak
from fastapi import HTTPException

from .eth_native_weth9_review import (
    WETH9_DEPOSIT_SELECTOR, WETH9_WITHDRAW_SELECTOR,
)
from .eth_swaprouter02_preflight import WETH_MAINNET
from .live_eth_receipts import _rpc, _num
from .privy_wallet_verification import fetch_privy_user
from .wallet_balances import verified_addresses

_HASH=re.compile(r"^0x[0-9a-fA-F]{64}$")
_ADDRESS=re.compile(r"^0x[0-9a-fA-F]{40}$")
_DATA=re.compile(r"^0x[0-9a-fA-F]+$")
_DEPOSIT_TOPIC="0x"+keccak(text="Deposit(address,uint256)").hex()
_WITHDRAW_TOPIC="0x"+keccak(text="Withdrawal(address,uint256)").hex()


def _requested_amount(tx: dict, operation: str) -> int:
    raw=tx.get("input")
    value=_num(tx.get("value"))
    if type(raw) is not str or not _DATA.fullmatch(raw):
        raise ValueError("Unknown WETH9 calldata")
    if operation=="DEPOSIT":
        if raw.lower()!=WETH9_DEPOSIT_SELECTOR or not 0<value<10**45:
            raise ValueError("Invalid native ETH deposit")
        return value
    if (raw.lower()[:10]!=WETH9_WITHDRAW_SELECTOR or
            len(raw)!=10+64 or value!=0):
        raise ValueError("Invalid WETH9 withdrawal")
    amount=int(raw[10:],16)
    if not 0<amount<10**45:
        raise ValueError("Invalid WETH9 amount")
    return amount


def _weth_event_amount(receipt: dict, *, wallet: str,
                       operation: str) -> int:
    entries=receipt.get("logs")
    if type(entries) is not list or len(entries)>400:
        raise ValueError("Invalid WETH9 receipt logs")
    expected=_DEPOSIT_TOPIC if operation=="DEPOSIT" else _WITHDRAW_TOPIC
    found=[]
    for row in entries:
        if type(row) is not dict:
            raise ValueError("Malformed WETH9 contract log")
        contract=row.get("address")
        if type(contract) is not str or not _ADDRESS.fullmatch(contract):
            raise ValueError("Unidentified WETH9 log contract")
        if contract.lower()!=WETH_MAINNET.lower():
            continue
        topics=row.get("topics")
        if type(topics) is not list or not topics:
            raise ValueError("Missing WETH9 event topics")
        if type(topics[0]) is not str or topics[0].lower()!=expected:
            continue
        if len(topics)!=2 or not _HASH.fullmatch(topics[1]):
            raise ValueError("Invalid WETH9 wallet event")
        encoded=int(topics[1],16)
        if encoded>=2**160 or encoded!=int(wallet,16):
            raise ValueError("WETH9 event belongs to another wallet")
        value=row.get("data")
        if type(value) is not str or not _HASH.fullmatch(value):
            raise ValueError("Malformed WETH9 atomic units")
        amount=int(value,16)
        if not 0<amount<10**45:
            raise ValueError("Invalid WETH9 transfer amount")
        found.append(amount)
    if len(found)!=1:
        raise ValueError("Missing or duplicate native ETH conversion event")
    return found[0]


def inspect_weth9_receipt(
    tx_hash: str, *, wallet: str, operation: str,
    chain_id: object, transaction: object, receipt: object,
    latest: object, finalized: object,
) -> dict:
    if (type(tx_hash) is not str or not _HASH.fullmatch(tx_hash)
            or type(wallet) is not str or not _ADDRESS.fullmatch(wallet)
            or operation not in ("DEPOSIT","WITHDRAW")):
        raise ValueError("Invalid user-owned WETH9 transaction")
    if _num(chain_id)!=1:
        raise ValueError("Invalid ETH mainnet RPC identity")
    head=_num(latest)
    report={
        "mode":"ETH_MAINNET_WETH9_CONVERSION_RECEIPT",
        "asset":"ETH","network":"ethereum-mainnet",
        "tx_hash":tx_hash.lower(),"operation":operation,
        "state":"NOT_FOUND","transaction_seen":False,
        "transaction_finalized":False,
        "weth9_event_amount":None,
        "converted_asset":"WETH" if operation=="DEPOSIT" else "ETH",
        "confirmed_native_conversion":False,
        "swap_leg_linked":False,"native_eth_trade_cycle_complete":False,
        "network_fee_eth":None,"confirmations":0,
        "privy_policy_verified":False,"live_order_recorded":False,
        "execution_enabled":False,"can_submit":False,
    }
    if transaction is None:
        if receipt is not None:
            raise ValueError("Receipt without WETH9 transaction")
        return report
    if (type(transaction) is not dict
            or str(transaction.get("hash","")).lower()!=tx_hash.lower()
            or str(transaction.get("from","")).lower()!=wallet.lower()
            or str(transaction.get("to","")).lower()!=WETH_MAINNET.lower()
            or _num(transaction.get("chainId"))!=1):
        raise ValueError("Unexpected WETH9 sender/contract/network")
    units=_requested_amount(transaction,operation)
    report["transaction_seen"]=True
    report["state"]="PENDING"
    if receipt is None:
        if transaction.get("blockHash") not in (None,"0x") or transaction.get("blockNumber") is not None:
            raise ValueError("Inconsistent pending WETH9 transaction")
        return report
    if (type(receipt) is not dict
            or str(receipt.get("transactionHash","")).lower()!=tx_hash.lower()
            or str(receipt.get("from","")).lower()!=wallet.lower()
            or str(receipt.get("to","")).lower()!=WETH_MAINNET.lower()):
        raise ValueError("Wrong owner WETH9 receipt")
    block_hash=receipt.get("blockHash")
    if type(block_hash) is not str or not _HASH.fullmatch(block_hash):
        raise ValueError("Missing WETH9 block hash")
    block=_num(receipt.get("blockNumber"))
    if (not 0<block<=head or
            str(transaction.get("blockHash","")).lower()!=block_hash.lower() or
            _num(transaction.get("blockNumber"))!=block):
        raise ValueError("WETH9 receipt does not match its transaction")
    status=_num(receipt.get("status"))
    if status not in (0,1):
        raise ValueError("WETH9 receipt status invalid")
    gas=_num(receipt.get("gasUsed"))
    gas_price=_num(receipt.get("effectiveGasPrice"))
    if not 0<gas<=30_000_000 or not 0<gas_price<=10**12:
        raise ValueError("Unverifiable native conversion gas")
    report["network_fee_eth"]=format(
        Decimal(gas*gas_price)/Decimal(10**18),"f")
    report["confirmations"]=min(head-block+1,1_000_000)
    report["state"]="INCLUDED_SUCCESS" if status else "INCLUDED_REVERTED"
    if finalized is None:
        return report
    if type(finalized) is not dict:
        raise ValueError("Missing finalized ETH checkpoint")
    final_number=_num(finalized.get("number"))
    if final_number>head:
        raise ValueError("Finalized checkpoint exceeds chain tip")
    if final_number<block:
        return report
    report["transaction_finalized"]=True
    if status==0:
        report["state"]="FINALIZED_REVERTED"
        return report
    observed=_weth_event_amount(receipt,wallet=wallet,operation=operation)
    if units!=observed:
        raise ValueError("Finalized WETH9 event amount differs from actual transaction")
    report["state"]="FINALIZED_WETH9_CONVERSION"
    report["weth9_event_amount"]=format(
        Decimal(observed)/Decimal(10**18),"f")
    report["confirmed_native_conversion"]=True
    return report


async def check_user_weth9_receipt(did: str, tx_hash: str,
                                   *, operation: str) -> dict:
    if type(tx_hash) is not str or not _HASH.fullmatch(tx_hash):
        raise HTTPException(422,"Invalid ETH conversion receipt hash")
    if operation not in ("DEPOSIT","WITHDRAW"):
        raise HTTPException(422,"Invalid WETH9 conversion operation")
    user=await fetch_privy_user(did)
    try:
        wallet=verified_addresses(user,did)["ETH"]
        if type(wallet) is not str or not _ADDRESS.fullmatch(wallet):
            raise ValueError("No independently verified ETH wallet")
    except (ValueError,KeyError,TypeError):
        raise HTTPException(503,"Privy ETH wallet not verified") from None
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8),follow_redirects=False
        ) as client:
            chain,tx,receipt,head=await asyncio.gather(
                _rpc(client,"eth_chainId",[],1),
                _rpc(client,"eth_getTransactionByHash",[tx_hash],2),
                _rpc(client,"eth_getTransactionReceipt",[tx_hash],3),
                _rpc(client,"eth_blockNumber",[],4),
            )
            final=(await _rpc(client,"eth_getBlockByNumber",["finalized",False],5)
                   if receipt is not None else None)
        return inspect_weth9_receipt(
            tx_hash,wallet=wallet,operation=operation,
            chain_id=chain,transaction=tx,receipt=receipt,
            latest=head,finalized=final)
    except (httpx.HTTPError,ValueError,TypeError,OverflowError):
        raise HTTPException(502,"WETH9 mainnet receipt cannot be verified") from None
