"""Ethereum BUY source proof for native BTC, SOL and XRP swaps.

Read-only, owner-scoped verification of a finalized canonical THORChain
depositWithExpiry call and exact USDC/USDT movement. No wallet signing.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
import hashlib
import json
import re

import httpx
import psycopg
from eth_utils import keccak
from fastapi import HTTPException
from psycopg.rows import dict_row

from .eth_router02_receipts import _TRANSFER_TOPIC, _topic_address
from .live_eth_quotes import STABLES
from .live_eth_receipts import _num, _rpc
from .manual_swap_live_quotes import THOR_ASSETS
from .native_thor_swap_quote import _API, _exact_owner_wallets
from .native_thor_transaction_status import _VALID_INTENT
from .privy_wallet_verification import fetch_privy_user
from .storage import _db_url, _ensure_schema, _safe_did

_ASSETS=frozenset(("BTC","SOL","XRP"))
_SHA=re.compile(r"^[0-9a-f]{64}$")
_HASH=re.compile(r"^0x[a-fA-F0-9]{64}$")
_ADDRESS=re.compile(r"^0x[a-fA-F0-9]{40}$")
_HEX=re.compile(r"^0x[a-fA-F0-9]+$")
_DEPOSIT_SELECTOR=keccak(text="depositWithExpiry(address,address,uint256,string,uint256)")[:4]
_DEPOSIT_TOPIC="0x"+keccak(text="Deposit(address,address,uint256,string)").hex()


def _abi_memo(data:bytes,offset:int,word_count:int)->str:
    if (type(data) is not bytes or offset!=32*word_count
            or len(data)<offset+32 or len(data)%32):
        raise ValueError("Noncanonical THORChain dynamic memo")
    n=int.from_bytes(data[offset:offset+32],"big")
    end=offset+32+n
    padded=offset+32+((n+31)//32)*32
    if not 7<=n<=250 or len(data)!=padded or any(data[end:padded]):
        raise ValueError("Invalid THORChain memo length or padding")
    try:
        memo=data[offset+32:end].decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("Invalid THORChain memo UTF-8") from None
    if any(c in memo for c in "\r\n\x00"):
        raise ValueError("Invalid original native swap memo")
    return memo


def _read_deposit_call(encoded:object)->dict:
    if type(encoded) is not str or len(encoded)>2048 or not _HEX.fullmatch(encoded):
        raise ValueError("Invalid THORChain original calldata")
    call=bytes.fromhex(encoded[2:])
    if len(call)<4+5*32 or call[:4]!=_DEPOSIT_SELECTOR:
        raise ValueError("Original transaction is not depositWithExpiry")
    raw=call[4:]
    words=[int.from_bytes(raw[32*i:32*(i+1)],"big") for i in range(5)]
    if words[0]>=2**160 or words[1]>=2**160 or words[2]<=0:
        raise ValueError("Invalid ETH vault, token, or deposit quantity")
    return {
        "vault":"0x"+words[0].to_bytes(20,"big").hex(),
        "token":"0x"+words[1].to_bytes(20,"big").hex(),
        "atomic":words[2],
        "memo":_abi_memo(raw,words[3],5),
        "expiry":words[4],
    }


def _read_deposit_events(
    logs:object,*,owner:str,router:str,deposit:dict,
    tx_hash:str,block_hash:str,
)->None:
    if type(logs) is not list or len(logs)>500:
        raise ValueError("Unverified Ethereum source events")
    transferred=emitted=0
    for log in logs:
        if type(log) is not dict:
            raise ValueError("Malformed Ethereum source log")
        contract=log.get("address")
        if type(contract) is not str or not _ADDRESS.fullmatch(contract):
            raise ValueError("Missing official event contract")
        topics=log.get("topics")
        if type(topics) is not list or not topics:
            raise ValueError("Malformed Ethereum event topics")
        if (str(log.get("transactionHash","")).lower()!=tx_hash
                or str(log.get("blockHash","")).lower()!=block_hash
                or log.get("removed") is True):
            raise ValueError("Wrong original transaction/block event")
        topic=topics[0]
        if contract.lower()==router and type(topic) is str and topic.lower()==_DEPOSIT_TOPIC:
            emitted+=1
            if (len(topics)!=3
                    or _topic_address(topics[1])!=deposit["vault"]
                    or _topic_address(topics[2])!=deposit["token"]):
                raise ValueError("Router deposit uses wrong original vault or token")
            raw=log.get("data")
            if type(raw) is not str or not _HEX.fullmatch(raw):
                raise ValueError("Malformed THORChain Deposit event")
            event=bytes.fromhex(raw[2:])
            if (len(event)<64 or
                    int.from_bytes(event[:32],"big")!=deposit["atomic"] or
                    _abi_memo(event,int.from_bytes(event[32:64],"big"),2)!=deposit["memo"]):
                raise ValueError("Router Deposit event contradicts original input")
        if contract.lower()==deposit["token"] and type(topic) is str and topic.lower()==_TRANSFER_TOPIC:
            if len(topics)!=3:
                raise ValueError("Noncanonical source Transfer")
            sender=_topic_address(topics[1])
            receiver=_topic_address(topics[2])
            if sender==owner or receiver==router:
                transferred+=1
                raw=log.get("data")
                if (sender!=owner or receiver!=router or type(raw) is not str
                        or len(raw)!=66 or not _HEX.fullmatch(raw)
                        or int(raw,16)!=deposit["atomic"]):
                    raise ValueError("Source token was not sent by owner to router")
    if transferred!=1 or emitted!=1:
        raise ValueError("Ambiguous THORChain deposit and ERC20 funding events")


def inspect_original_eth_buy_source(
    original:object,*,native_owner:str,chain_id:object,
    transaction:object,receipt:object,finalized:object,
    block:object,inbounds:object,
)->dict:
    if (type(original) is not dict or original.get("asset") not in _ASSETS
            or original.get("side")!="BUY" or original.get("status")!="SUBMITTED"
            or original.get("chain")!="ethereum-mainnet"
            or original.get("stable") not in STABLES
            or (original["asset"]=="XRP" and original["stable"]!="USDC")
            or type(original.get("privy_did")) is not str
            or type(original.get("payload_sha256")) is not str
            or not _SHA.fullmatch(original["payload_sha256"])
            or type(original.get("intent_key")) is not str
            or not _VALID_INTENT.fullmatch(original["intent_key"])
            or type(original.get("source_wallet")) is not str
            or not _ADDRESS.fullmatch(original["source_wallet"])
            or original.get("destination_wallet")!=native_owner
            or type(original.get("source_tx_hash")) is not str
            or not _HASH.fullmatch(original["source_tx_hash"])
            or type(original.get("input_amount")) not in (str,Decimal)
            or type(inbounds) is not list):
        raise ValueError("Original BTC/SOL/XRP Ethereum BUY not journal-bound")
    asset=original["asset"]
    owner=original["source_wallet"].lower()
    tx_id=original["source_tx_hash"].lower()
    if (_num(chain_id)!=1 or type(transaction) is not dict
            or type(receipt) is not dict or type(finalized) is not dict
            or type(block) is not dict):
        raise ValueError("Original Ethereum BUY is not finalized on mainnet")
    router=transaction.get("to")
    if (type(router) is not str or not _ADDRESS.fullmatch(router)
            or str(transaction.get("from","")).lower()!=owner
            or str(transaction.get("hash","")).lower()!=tx_id
            or str(receipt.get("transactionHash","")).lower()!=tx_id
            or str(receipt.get("from","")).lower()!=owner
            or str(receipt.get("to","")).lower()!=router.lower()
            or _num(transaction.get("value"))!=0
            or _num(receipt.get("status"))!=1):
        raise ValueError("ETH ERC20 original source sender or router mismatch")
    receipt_hash=receipt.get("blockHash")
    height=_num(receipt.get("blockNumber"))
    if (type(receipt_hash) is not str or not _HASH.fullmatch(receipt_hash)
            or str(transaction.get("blockHash","")).lower()!=receipt_hash.lower()
            or _num(transaction.get("blockNumber"))!=height
            or _num(block.get("number"))!=height
            or str(block.get("hash","")).lower()!=receipt_hash.lower()
            or not 0<height<=_num(finalized.get("number"))):
        raise ValueError("ETH original source not in canonical finalized block")
    deposit=_read_deposit_call(transaction.get("input"))
    parts=deposit["memo"].split(":")
    if (deposit["token"]!=STABLES[original["stable"]].lower()
            or len(parts)<3 or parts[0].upper() not in ("=","S","SWAP")
            or parts[1].upper()!=THOR_ASSETS[asset][0].upper()
            or parts[2].split("/")[0]!=native_owner
            or deposit["expiry"]<_num(block.get("timestamp"))):
        raise ValueError("Original ETH stable, target, or deadline mismatch")
    try:
        expected=Decimal(original["input_amount"])*Decimal(10**6)
    except (ValueError,TypeError,ArithmeticError):
        raise ValueError("Original stablecoin quantity missing") from None
    if (not expected.is_finite() or expected<=0
            or expected!=expected.to_integral_value()
            or int(expected)!=deposit["atomic"]):
        raise ValueError("Original source stablecoin debit not journal amount")
    matches=[entry for entry in inbounds if
             type(entry) is dict and entry.get("chain")=="ETH"
             and type(entry.get("router")) is str
             and entry["router"].lower()==router.lower()
             and type(entry.get("address")) is str
             and entry["address"].lower()==deposit["vault"]]
    if len(matches)!=1:
        raise ValueError("ETH THORChain Asgard vault or router not verified")
    _read_deposit_events(
        receipt.get("logs"),owner=owner,router=router.lower(),
        deposit=deposit,tx_hash=tx_id,block_hash=receipt_hash.lower(),
    )
    fee_wei=_num(receipt.get("gasUsed"))*_num(receipt.get("effectiveGasPrice"))
    proof={
        "mode":"OWNER_ORIGINAL_ETH_BUY_SOURCE_FINALITY",
        "asset":asset,"side":"BUY",
        "owner_privy_did":original["privy_did"],
        "original_order_sha256":original["payload_sha256"],
        "source_wallet":original["source_wallet"],
        "destination_wallet":native_owner,
        "source_tx_hash":original["source_tx_hash"],
        "source_stable":original["stable"],
        "source_stable_atomic":str(deposit["atomic"]),
        "source_block_number":height,
        "source_block_hash":receipt_hash.lower(),
        "source_network_fee_wei":str(fee_wei),
        "trusted_thorchain_deposit_verified":True,
        "source_chain_finalized":True,
        "destination_settlement_finalized":False,
        "net_profit_verified":False,
        "fee_assessed":False,
        "broadcast_or_retry_performed":False,
    }
    proof["source_proof_sha256"]=hashlib.sha256(json.dumps(
        proof,sort_keys=True,separators=(",",":"),ensure_ascii=True,
    ).encode("utf-8")).hexdigest()
    return proof


def _read_submitted_eth_buy(cur,*,did:str,asset:str,key:str)->dict:
    cur.execute(
        """SELECT privy_did,asset,intent_key,payload_sha256,
                  side,stable,chain,input_amount,source_wallet,
                  destination_wallet,source_tx_hash,status
           FROM mininex_live_native_order_intents
           WHERE privy_did=%s AND asset=%s AND intent_key=%s""",
        (did,asset,key))
    row=cur.fetchone()
    if (type(row) is not dict or row.get("privy_did")!=did
            or row.get("asset")!=asset or row.get("intent_key")!=key
            or row.get("side")!="BUY" or row.get("status")!="SUBMITTED"
            or row.get("chain")!="ethereum-mainnet"):
        raise HTTPException(409,"Original Ethereum BUY source unavailable")
    return row


async def get_original_eth_buy_source(did:str,*,asset:str,intent_key:str)->dict:
    """Read-only original source inspection; no user-supplied wallet addresses."""
    did=_safe_did(did)
    if (asset not in _ASSETS or type(intent_key) is not str
            or not _VALID_INTENT.fullmatch(intent_key)):
        raise HTTPException(422,"Invalid original Ethereum BUY order reference")
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            original=_read_submitted_eth_buy(cur,did=did,asset=asset,key=intent_key)
    user=await fetch_privy_user(did)
    try:
        native,ethereum=_exact_owner_wallets(user,did,asset)
    except (ValueError,TypeError,KeyError):
        raise HTTPException(409,"Original Privy BUY source/destination missing") from None
    if (original["source_wallet"].lower()!=ethereum.lower()
            or original["destination_wallet"]!=native):
        raise HTTPException(409,"Original user native/ETH wallet changed")
    tx_id=original["source_tx_hash"].lower()
    try:
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(10),follow_redirects=False) as client:
            chain,transaction,receipt,finalized=await asyncio.gather(
                _rpc(client,"eth_chainId",[],1),
                _rpc(client,"eth_getTransactionByHash",[tx_id],2),
                _rpc(client,"eth_getTransactionReceipt",[tx_id],3),
                _rpc(client,"eth_getBlockByNumber",["finalized",False],4),
            )
            if type(receipt) is not dict:
                raise ValueError("Unmined ETH original source")
            receipt_number=receipt.get("blockNumber")
            _num(receipt_number)
            block,inh=await asyncio.gather(
                _rpc(client,"eth_getBlockByNumber",[receipt_number,False],5),
                client.get(_API+"/thorchain/inbound_addresses",
                           headers={"Accept":"application/json"}),
            )
            inh.raise_for_status()
            if len(inh.content)>250_000:
                raise ValueError("Oversized original THORChain vault data")
            inbounds=inh.json()
        return inspect_original_eth_buy_source(
            original,native_owner=native,chain_id=chain,
            transaction=transaction,receipt=receipt,
            finalized=finalized,block=block,inbounds=inbounds)
    except (ValueError,TypeError,KeyError,OverflowError,httpx.HTTPError):
        raise HTTPException(
            503,"Verified original Ethereum BUY source unavailable") from None
