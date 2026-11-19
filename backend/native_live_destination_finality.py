"""Independent ERC-20 destination finality for original BTC/SOL/XRP SELL swaps.

A THORChain outbound hash is only a lead. The actual Ethereum-mainnet receipt
must contain a finalized, canonical Transfer of the official USDC/USDT token
into the *original* Privy-owned destination wallet. This module cannot sign,
broadcast, retry, charge fees, or infer a completed BUY / trading profit.
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
from .privy_wallet_verification import fetch_privy_user
from .storage import _db_url, _ensure_schema, _safe_did
from .wallet_balances import verified_addresses

_ASSETS=frozenset(("BTC","SOL","XRP"))
_SHA=re.compile(r"^[0-9a-f]{64}$")
_HASH=re.compile(r"^0x[a-fA-F0-9]{64}$")
_WORD=re.compile(r"^0x[a-fA-F0-9]{64}$")
_ADDRESS=re.compile(r"^0x[a-fA-F0-9]{40}$")


def inspect_original_eth_stable_destination(
    original:object,protocol:object,*,chain_id:object,
    receipt:object,finalized:object,included_block:object,
)->dict:
    """Verify one original native SELL's real stablecoin delivery on Ethereum.

    Requires *three* independent bindings: owner journal source finality,
    THORChain exact original/outbound ID, and the actual finalized ERC20
    transfer to the owner. Does not book a profit or charge a fee.
    """
    if (type(original) is not dict or type(protocol) is not dict
            or original.get("asset") not in _ASSETS
            or original.get("side")!="SELL"
            or original.get("status")!="SOURCE_FINALIZED"
            or original.get("stable") not in STABLES
            or (original.get("asset")=="XRP" and original.get("stable")!="USDC")
            or type(original.get("intent_key")) is not str
            or not _VALID_INTENT.fullmatch(original["intent_key"])
            or type(original.get("privy_did")) is not str
            or type(original.get("payload_sha256")) is not str
            or not _SHA.fullmatch(original["payload_sha256"])
            or type(original.get("source_proof_sha256")) is not str
            or not _SHA.fullmatch(original["source_proof_sha256"])
            or type(original.get("destination_wallet")) is not str
            or not _ADDRESS.fullmatch(original["destination_wallet"])):
        raise ValueError("Original native owner order is not source-finalized")
    tx=original.get("source_tx_hash")
    if (type(tx) is not str
            or protocol.get("mode")!="THORCHAIN_ORIGINAL_TRANSACTION_READ_ONLY"
            or protocol.get("asset")!=original["asset"]
            or protocol.get("source_tx_hash")!=tx
            or protocol.get("source_wallet_original") is not True
            or protocol.get("destination_owner_bound") is not True
            or protocol.get("protocol_inbound_observed") is not True
            or protocol.get("protocol_inbound_finalized") is not True
            or protocol.get("protocol_reports_swap_finalized") is not True
            or protocol.get("protocol_refund_hashes",[])!=[]
            or protocol.get("destination_chain_receipts_verified") is not False
            or protocol.get("realized_pnl_verified") is not False
            or protocol.get("fee_assessed") is not False
            or protocol.get("retry_permitted") is not False
            or protocol.get("new_transactions_sent")!=0):
        raise ValueError("Protocol has not proven original outbound identity")
    out=protocol.get("protocol_outbound_hashes")
    if (type(out) is not list or len(out)!=1
            or type(out[0]) is not str or not _SHA.fullmatch(out[0])):
        # Future multiple outbound transactions require a multi-part proof.
        raise ValueError("Single exact original Ethereum outbound not established")
    outbound="0x"+out[0]
    if _num(chain_id)!=1:
        raise ValueError("Wrong Ethereum blockchain")
    if (type(receipt) is not dict or type(finalized) is not dict
            or type(included_block) is not dict
            or type(receipt.get("transactionHash")) is not str
            or receipt["transactionHash"].lower()!=outbound
            or _num(receipt.get("status"))!=1):
        raise ValueError("Ethereum outbound transaction not successfully mined")
    block_hash=receipt.get("blockHash")
    if type(block_hash) is not str or not _HASH.fullmatch(block_hash):
        raise ValueError("Ethereum canonical block hash missing")
    block_number=_num(receipt.get("blockNumber"))
    observed_number=_num(included_block.get("number"))
    finalized_number=_num(finalized.get("number"))
    if (not 0<block_number<=finalized_number
            or observed_number!=block_number
            or str(included_block.get("hash","")).lower()!=block_hash.lower()):
        raise ValueError("Ethereum outbound is not in a finalized canonical block")
    logs=receipt.get("logs")
    if type(logs) is not list or len(logs)>500:
        raise ValueError("Ethereum stablecoin transfer logs unavailable")
    wallet=original["destination_wallet"].lower()
    token=STABLES[original["stable"]].lower()
    matches=[]
    for log in logs:
        if type(log) is not dict:
            raise ValueError("Malformed Ethereum event")
        address=log.get("address")
        if type(address) is not str or not _ADDRESS.fullmatch(address):
            raise ValueError("Invalid Ethereum event contract")
        if address.lower()!=token:
            continue
        topics=log.get("topics")
        if type(topics) is not list or not topics:
            raise ValueError("Official stablecoin event topics missing")
        if type(topics[0]) is not str or topics[0].lower()!=_TRANSFER_TOPIC:
            continue
        if len(topics)!=3:
            raise ValueError("Unexpected stablecoin Transfer ABI")
        sender=_topic_address(topics[1])
        recipient=_topic_address(topics[2])
        if recipient!=wallet:
            continue
        if sender==wallet:
            raise ValueError("Self-transfer is not net cross-chain settlement")
        word=log.get("data")
        if type(word) is not str or not _WORD.fullmatch(word):
            raise ValueError("Invalid official token transfer value")
        atomic=int(word,16)
        if not 0<atomic<2**256:
            raise ValueError("Empty stablecoin destination credit")
        if (str(log.get("transactionHash","")).lower()!=outbound
                or str(log.get("blockHash","")).lower()!=block_hash.lower()
                or log.get("removed") is True):
            raise ValueError("Stablecoin transfer belongs to another chain event")
        matches.append(atomic)
    if len(matches)!=1:
        raise ValueError("No unique owner USDC/USDT credit in final outbound")
    amount=matches[0]
    encoded=[
        original["privy_did"],original["asset"],original["intent_key"],
        original["payload_sha256"],original["source_proof_sha256"],
        original["stable"],wallet,outbound,block_hash.lower(),
        block_number,str(amount),token,
    ]
    digest=hashlib.sha256(json.dumps(
        encoded,separators=(",",":"),ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    return {
        "mode":"OWNER_NATIVE_ETHEREUM_STABLE_DESTINATION_FINALITY",
        "asset":original["asset"],"stable":original["stable"],
        "intent_key":original["intent_key"],
        "original_order_sha256":original["payload_sha256"],
        "original_source_proof_sha256":original["source_proof_sha256"],
        "source_tx_hash":tx,
        "destination_tx_hash":out[0],
        "destination_block_number":block_number,
        "destination_block_hash":block_hash.lower(),
        "destination_token_contract":token,
        "destination_received_atomic":str(amount),
        "destination_received_amount":format(
            Decimal(amount)/Decimal(10**6),"f",
        ),
        "destination_proof_sha256":digest,
        "ethereum_finalized_token_credit":True,
        "protocol_swap_finalized":True,
        "native_sell_settlement_complete":True,
        "native_buy_settlement_complete":False,
        "trading_profit_verified":False,
        "fee_assessed":False,
        "new_transactions_sent":0,
        "retry_permitted":False,
        "can_submit":False,
    }


def _read_original_native_destination(cur,*,did:str,asset:str,key:str)->dict:
    cur.execute(
        """SELECT privy_did,asset,intent_key,payload_sha256,
                  source_proof_sha256,source_tx_hash,source_wallet,
                  destination_wallet,stable,side,status
           FROM mininex_live_native_order_intents
           WHERE privy_did=%s AND asset=%s AND intent_key=%s""",
        (did,asset,key),
    )
    row=cur.fetchone()
    if (type(row) is not dict or row.get("privy_did")!=did
            or row.get("asset")!=asset or row.get("intent_key")!=key
            or row.get("status")!="SOURCE_FINALIZED"):
        raise HTTPException(409,"Original native source not ready to settle")
    return row


async def get_private_native_ethereum_destination(
    did:str,*,asset:str,intent_key:str,
)->dict:
    """One authenticated, read-only original order settlement inspection."""
    did=_safe_did(did)
    if (asset not in _ASSETS or type(intent_key) is not str
            or not _VALID_INTENT.fullmatch(intent_key)):
        raise HTTPException(422,"Invalid original native settlement identity")
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            row=_read_original_native_destination(
                cur,did=did,asset=asset,key=intent_key)
    user=await fetch_privy_user(did)
    try:
        current=verified_addresses(user,did).get("ETH")
    except (ValueError,TypeError,KeyError):
        current=None
    if (type(current) is not str
            or current.lower()!=row["destination_wallet"].lower()):
        raise HTTPException(409,"Original Ethereum receiving wallet changed")
    protocol=await get_private_original_protocol_status(
        did,asset=asset,intent_key=intent_key)
    ids=protocol.get("protocol_outbound_hashes")
    if type(ids) is not list or len(ids)!=1 or not _SHA.fullmatch(str(ids[0])):
        raise HTTPException(409,"No unique original protocol outbound available")
    tx="0x"+ids[0]
    try:
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(9),follow_redirects=False) as client:
            chain,receipt,finalized=await asyncio.gather(
                _rpc(client,"eth_chainId",[],1),
                _rpc(client,"eth_getTransactionReceipt",[tx],2),
                _rpc(client,"eth_getBlockByNumber",["finalized",False],3),
            )
            if type(receipt) is not dict:
                raise ValueError("Unmined outbound transaction")
            block_number=receipt.get("blockNumber")
            _num(block_number)
            included=await _rpc(
                client,"eth_getBlockByNumber",[block_number,False],4)
        return inspect_original_eth_stable_destination(
            row,protocol,chain_id=chain,receipt=receipt,
            finalized=finalized,included_block=included)
    except (ValueError,TypeError,KeyError,OverflowError,httpx.HTTPError):
        raise HTTPException(
            503,"Independently finalized native destination unavailable") from None


def _persist_native_destination_locked(
    cur,*,original:dict,verified:dict,
)->str:
    """One irreversible, compare-and-swap database settlement transition.

    Only trusted worker code can call this. Public GET never invokes this.
    No automatic fee or realized PnL is posted from a single SELL.
    """
    if (type(original) is not dict or type(verified) is not dict
            or verified.get("mode")!="OWNER_NATIVE_ETHEREUM_STABLE_DESTINATION_FINALITY"
            or verified.get("asset")!=original.get("asset")
            or verified.get("intent_key")!=original.get("intent_key")
            or verified.get("original_order_sha256")!=original.get("payload_sha256")
            or verified.get("original_source_proof_sha256")!=original.get("source_proof_sha256")
            or verified.get("source_tx_hash")!=original.get("source_tx_hash")
            or verified.get("stable")!=original.get("stable")
            or verified.get("ethereum_finalized_token_credit") is not True
            or verified.get("protocol_swap_finalized") is not True
            or verified.get("native_sell_settlement_complete") is not True
            or verified.get("native_buy_settlement_complete") is not False
            or verified.get("trading_profit_verified") is not False
            or verified.get("fee_assessed") is not False
            or verified.get("new_transactions_sent")!=0
            or verified.get("retry_permitted") is not False):
        raise HTTPException(409,"Finalized native destination not bound to original")
    did=_safe_did(original["privy_did"])
    asset=original["asset"]
    key=original["intent_key"]
    tx=verified.get("destination_tx_hash")
    digest=verified.get("destination_proof_sha256")
    atomic=verified.get("destination_received_atomic")
    if (type(tx) is not str or not _SHA.fullmatch(tx)
            or type(digest) is not str or not _SHA.fullmatch(digest)
            or type(atomic) is not str or not re.fullmatch(r"[1-9][0-9]{0,77}",atomic)):
        raise ValueError("Original Ethereum stablecoin proof invalid")
    cur.execute(
        """SELECT status,side,stable,source_tx_hash,payload_sha256,
                  source_proof_sha256,destination_wallet,destination_tx_hash,
                  destination_proof_sha256
           FROM mininex_live_native_order_intents
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
           FOR UPDATE""",
        (did,asset,key))
    row=cur.fetchone()
    if (type(row) is not dict or row.get("status")!="SOURCE_FINALIZED"
            or row.get("side")!="SELL"
            or row.get("stable")!=original["stable"]
            or row.get("source_tx_hash")!=original["source_tx_hash"]
            or row.get("payload_sha256")!=original["payload_sha256"]
            or row.get("source_proof_sha256")!=original["source_proof_sha256"]
            or row.get("destination_wallet")!=original["destination_wallet"]
            or row.get("destination_tx_hash") is not None
            or row.get("destination_proof_sha256") is not None):
        raise HTTPException(409,"Original native payout already changed or settled")
    cur.execute(
        """UPDATE mininex_live_native_order_intents
           SET status='DESTINATION_FINALIZED',destination_tx_hash=%s,
               destination_proof_sha256=%s,destination_received_atomic=%s,
               destination_finalized_at=now(),updated_at=now()
           WHERE privy_did=%s AND asset=%s AND intent_key=%s
             AND status='SOURCE_FINALIZED'
             AND source_tx_hash=%s AND source_proof_sha256=%s
             AND destination_proof_sha256 IS NULL
           RETURNING status""",
        (tx,digest,atomic,did,asset,key,
         original["source_tx_hash"],original["source_proof_sha256"]))
    result=cur.fetchone()
    if type(result) is not dict or result.get("status")!="DESTINATION_FINALIZED":
        raise HTTPException(409,"Native original destination persistence conflict")
    return "DESTINATION_FINALIZED"
