"""Finalized original BTC/SOL/XRP BUY refund to the user's Ethereum stable.

This is an ERC20 source-chain REFUND, not a native BUY and not a profit.
The exact original THORChain refund outbound must be confirmed in an actual
canonical Ethereum-mainnet ERC20 Transfer to the original user's wallet.
No signing, rebroadcast, new order, new fee or approval is possible here.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
import hashlib
import json
import re

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .eth_router02_receipts import _TRANSFER_TOPIC, _topic_address
from .live_eth_quotes import STABLES
from .live_eth_receipts import _num, _rpc
from .native_thor_transaction_status import (
    _VALID_INTENT, get_private_original_protocol_status,
)
from .native_thor_swap_quote import _exact_owner_wallets
from .privy_wallet_verification import fetch_privy_user
from .storage import _db_url, _ensure_schema, _safe_did

_ASSETS=frozenset(("BTC","SOL","XRP"))
_SHA=re.compile(r"^[0-9a-f]{64}$")
_ADDR=re.compile(r"^0x[0-9a-fA-F]{40}$")
_HASH=re.compile(r"^0x[0-9a-fA-F]{64}$")
_WORD=re.compile(r"^0x[0-9a-fA-F]{64}$")


def inspect_original_eth_buy_refund(
    original:object, protocol:object,*,chain_id:object,
    receipt:object, finalized:object, included_block:object,
)->dict:
    """Reject partial/outdated THOR refunds and any unconfirmed owner credit."""
    if (type(original) is not dict or type(protocol) is not dict
            or original.get("asset") not in _ASSETS
            or original.get("side")!="BUY"
            or original.get("status")!="SOURCE_FINALIZED"
            or original.get("stable") not in STABLES
            or (original["asset"]=="XRP" and original["stable"]!="USDC")
            or type(original.get("source_wallet")) is not str
            or not _ADDR.fullmatch(original["source_wallet"])
            or type(original.get("intent_key")) is not str
            or not _VALID_INTENT.fullmatch(original["intent_key"])
            or type(original.get("source_proof_sha256")) is not str
            or not _SHA.fullmatch(original["source_proof_sha256"])
            or type(original.get("payload_sha256")) is not str
            or not _SHA.fullmatch(original["payload_sha256"])):
        raise ValueError("Native BUY source does not have original finalized custody")
    if (protocol.get("mode")!="THORCHAIN_ORIGINAL_TRANSACTION_READ_ONLY"
            or protocol.get("asset")!=original["asset"]
            or protocol.get("side")!="BUY"
            or protocol.get("source_tx_hash")!=original.get("source_tx_hash")
            or protocol.get("source_wallet_original") is not True
            or protocol.get("destination_owner_bound") is not True
            or protocol.get("protocol_inbound_observed") is not True
            or protocol.get("protocol_inbound_finalized") is not True
            or protocol.get("refund_original_source_chain")!="ETH"
            or protocol.get("refund_independently_finalized") is not False
            or protocol.get("destination_chain_receipts_verified") is not False
            or protocol.get("realized_pnl_verified") is not False
            or protocol.get("fee_assessed") is not False
            or protocol.get("retry_permitted") is not False
            or protocol.get("new_transactions_sent")!=0
            or protocol.get("protocol_outbound_hashes")!=[]):
        raise ValueError("No proven one-sided original ETH refund")
    refunds=protocol.get("protocol_refund_hashes")
    amounts=protocol.get("protocol_refund_units_1e8")
    if (type(refunds) is not list or len(refunds)!=1
            or type(refunds[0]) is not str or not _SHA.fullmatch(refunds[0])
            or type(amounts) is not list or len(amounts)!=1
            or type(amounts[0]) is not str
            or not re.fullmatch(r"[1-9][0-9]{0,35}",amounts[0])):
        raise ValueError("Original THOR ETH refund hashes/values unavailable")
    units_1e8=int(amounts[0])
    if units_1e8%100:
        raise ValueError("Source stable refund has fractional ERC20 micro-unit")
    atomic=units_1e8//100
    try:
        original_atomic=Decimal(original["input_amount"])*Decimal(10**6)
    except (KeyError,ValueError,TypeError,ArithmeticError):
        raise ValueError("Original stable amount cannot be verified") from None
    if (not original_atomic.is_finite() or original_atomic<=0
            or original_atomic!=original_atomic.to_integral_value()
            or not 0<atomic<=original_atomic):
        raise ValueError("Refund exceeds original user stable funding")
    tx_hash="0x"+refunds[0]
    if (_num(chain_id)!=1 or type(receipt) is not dict
            or type(finalized) is not dict or type(included_block) is not dict
            or str(receipt.get("transactionHash","")).lower()!=tx_hash
            or _num(receipt.get("status"))!=1):
        raise ValueError("Original ETH refund transaction not successful")
    block_hash=receipt.get("blockHash")
    if type(block_hash) is not str or not _HASH.fullmatch(block_hash):
        raise ValueError("Unverified ETH refund block hash")
    block_no=_num(receipt.get("blockNumber"))
    if (not 0<block_no<=_num(finalized.get("number"))
            or block_no!=_num(included_block.get("number"))
            or str(included_block.get("hash","")).lower()!=block_hash.lower()):
        raise ValueError("Original ETH refund is not in canonical final chain")
    logs=receipt.get("logs")
    if type(logs) is not list or len(logs)>500:
        raise ValueError("No canonical ETH refund stable token event")
    wallet=original["source_wallet"].lower()
    stable=STABLES[original["stable"]].lower()
    found=[]
    for log in logs:
        if type(log) is not dict:
            raise ValueError("Malformed original refund receipt event")
        if str(log.get("address","")).lower()!=stable:
            continue
        topics=log.get("topics")
        if type(topics) is not list or not topics:
            raise ValueError("No original ERC20 refund Transfer event")
        if str(topics[0]).lower()!=_TRANSFER_TOPIC:
            continue
        if len(topics)!=3:
            raise ValueError("Malformed stablecoin refund Transfer topics")
        sender=_topic_address(topics[1])
        recipient=_topic_address(topics[2])
        if recipient!=wallet:
            continue
        value=log.get("data")
        if (sender==wallet or type(value) is not str
                or not _WORD.fullmatch(value)
                or str(log.get("transactionHash","")).lower()!=tx_hash
                or str(log.get("blockHash","")).lower()!=block_hash.lower()
                or log.get("removed") is True):
            raise ValueError("Original stable refund not actually credited")
        found.append(int(value,16))
    if found!=[atomic]:
        raise ValueError("Refund amount does not match actual owner ERC20 credit")
    digest=hashlib.sha256(json.dumps([
        original["privy_did"],original["asset"],original["intent_key"],
        original["payload_sha256"],original["source_proof_sha256"],
        original["source_tx_hash"],original["stable"],
        wallet,refunds[0],block_hash.lower(),block_no,str(atomic),
    ],separators=(",",":"),ensure_ascii=True).encode("ascii")).hexdigest()
    return {
        "mode":"ORIGINAL_NATIVE_BUY_ETH_STABLE_REFUND_FINALITY",
        "asset":original["asset"],"side":"BUY",
        "intent_key":original["intent_key"],
        "stable":original["stable"],
        "original_order_sha256":original["payload_sha256"],
        "original_source_proof_sha256":original["source_proof_sha256"],
        "source_tx_hash":original["source_tx_hash"],
        "refund_tx_hash":refunds[0],
        "refund_amount_atomic":str(atomic),
        "refund_proof_sha256":digest,
        "original_ethereum_owner_wallet_credited":True,
        "source_chain_refund_finalized":True,
        "native_buy_settlement_complete":False,
        "native_sell_settlement_complete":False,
        "realized_profit_verified":False,
        "fee_assessed":False,"new_transactions_sent":0,
        "retry_permitted":False,"can_submit":False,
    }


async def get_private_original_native_buy_refund(
    did:str,*,asset:str,intent_key:str,
)->dict:
    """Original owner only; Ethereum/THORChain data are independently read."""
    did=_safe_did(did)
    if (asset not in _ASSETS or type(intent_key) is not str
            or not _VALID_INTENT.fullmatch(intent_key)):
        raise HTTPException(422,"Invalid original ETH stable refund reference")
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT privy_did,asset,intent_key,status,side,stable,
                          input_amount,source_wallet,destination_wallet,
                          source_tx_hash,payload_sha256,source_proof_sha256
                   FROM mininex_live_native_order_intents
                   WHERE privy_did=%s AND asset=%s AND intent_key=%s""",
                (did,asset,intent_key))
            original=cur.fetchone()
    if (type(original) is not dict
            or original.get("privy_did")!=did
            or original.get("asset")!=asset
            or original.get("side")!="BUY"
            or original.get("status")!="SOURCE_FINALIZED"):
        raise HTTPException(409,"No original ETH BUY source refund to inspect")
    user=await fetch_privy_user(did)
    try:
        native,ethereum=_exact_owner_wallets(user,did,asset)
    except (KeyError,TypeError,ValueError):
        raise HTTPException(409,"Original ETH source owner unavailable") from None
    if (original["destination_wallet"]!=native
            or original["source_wallet"].lower()!=ethereum.lower()):
        raise HTTPException(409,"Native BUY refund owner changed")
    protocol=await get_private_original_protocol_status(
        did,asset=asset,intent_key=intent_key)
    hashes=protocol.get("protocol_refund_hashes")
    if (type(hashes) is not list or len(hashes)!=1
            or protocol.get("protocol_outbound_hashes")!=[]):
        raise HTTPException(409,"Original THORChain stable refund unavailable")
    tx="0x"+hashes[0]
    try:
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(9),follow_redirects=False) as client:
            chain,receipt,finalized=await asyncio.gather(
                _rpc(client,"eth_chainId",[],1),
                _rpc(client,"eth_getTransactionReceipt",[tx],2),
                _rpc(client,"eth_getBlockByNumber",["finalized",False],3))
            if type(receipt) is not dict:
                raise ValueError("Unmined original ETH refund")
            block_number=receipt.get("blockNumber")
            _num(block_number)
            included=await _rpc(
                client,"eth_getBlockByNumber",[block_number,False],4)
        return inspect_original_eth_buy_refund(
            original,protocol,chain_id=chain,receipt=receipt,
            finalized=finalized,included_block=included)
    except (ValueError,TypeError,KeyError,httpx.HTTPError):
        raise HTTPException(503,"Original ETH stable refund unverified") from None


def _persist_original_native_buy_refund_locked(
    cur,*,original:dict,verified:dict,
)->str:
    """Atomic source-finalized BUY -> verified refund; never a trade fill."""
    if (type(original) is not dict or type(verified) is not dict
            or original.get("side")!="BUY"
            or original.get("status")!="SOURCE_FINALIZED"
            or verified.get("mode")!="ORIGINAL_NATIVE_BUY_ETH_STABLE_REFUND_FINALITY"
            or verified.get("asset")!=original.get("asset")
            or verified.get("side")!="BUY"
            or verified.get("intent_key")!=original.get("intent_key")
            or verified.get("source_tx_hash")!=original.get("source_tx_hash")
            or verified.get("original_order_sha256")!=original.get("payload_sha256")
            or verified.get("original_source_proof_sha256")!=original.get("source_proof_sha256")
            or verified.get("stable")!=original.get("stable")
            or verified.get("original_ethereum_owner_wallet_credited") is not True
            or verified.get("source_chain_refund_finalized") is not True
            or verified.get("native_buy_settlement_complete") is not False
            or verified.get("realized_profit_verified") is not False
            or verified.get("fee_assessed") is not False
            or verified.get("new_transactions_sent")!=0
            or verified.get("retry_permitted") is not False
            or verified.get("can_submit") is not False):
        raise HTTPException(409,"Original native BUY refund proof differs")
    did=_safe_did(original["privy_did"])
    asset,key=original["asset"],original["intent_key"]
    tx=verified.get("refund_tx_hash")
    digest=verified.get("refund_proof_sha256")
    atomic=verified.get("refund_amount_atomic")
    if (type(tx) is not str or not _SHA.fullmatch(tx)
            or type(digest) is not str or not _SHA.fullmatch(digest)
            or type(atomic) is not str
            or not re.fullmatch(r"[1-9][0-9]{0,77}",atomic)):
        raise ValueError("Invalid original ETH refund data")
    cur.execute(
        """SELECT status,side,stable,source_wallet,source_tx_hash,
                  payload_sha256,source_proof_sha256,destination_tx_hash,
                  destination_proof_sha256
           FROM mininex_live_native_order_intents
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
           FOR UPDATE""",(did,asset,key))
    saved=cur.fetchone()
    if (type(saved) is not dict or saved.get("status")!="SOURCE_FINALIZED"
            or saved.get("side")!="BUY"
            or saved.get("stable")!=original.get("stable")
            or saved.get("source_wallet")!=original.get("source_wallet")
            or saved.get("source_tx_hash")!=original.get("source_tx_hash")
            or saved.get("payload_sha256")!=original.get("payload_sha256")
            or saved.get("source_proof_sha256")!=original.get("source_proof_sha256")
            or saved.get("destination_tx_hash") is not None
            or saved.get("destination_proof_sha256") is not None):
        raise HTTPException(409,"Original ETH BUY refund changed")
    cur.execute(
        """UPDATE mininex_live_native_order_intents
           SET status='REFUNDED_VERIFIED',destination_tx_hash=%s,
               destination_proof_sha256=%s,destination_received_atomic=%s,
               destination_finalized_at=now(),updated_at=now()
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
             AND status='SOURCE_FINALIZED' AND side='BUY'
             AND source_tx_hash=%s AND source_proof_sha256=%s
             AND destination_proof_sha256 IS NULL RETURNING status""",
        (tx,digest,atomic,did,asset,key,
         original["source_tx_hash"],original["source_proof_sha256"]))
    result=cur.fetchone()
    if type(result) is not dict or result.get("status")!="REFUNDED_VERIFIED":
        raise HTTPException(409,"Original native BUY refund persistence conflict")
    return "REFUNDED_VERIFIED"
