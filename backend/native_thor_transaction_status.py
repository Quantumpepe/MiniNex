"""Read-only THORChain execution status for an original BTC/SOL/XRP trade.

Uses the documented THORNode /thorchain/tx/status/{txid} endpoint. The
protocol report is not proof that the destination wallet received money.
Only an original owner journal row may be checked; no client-selected txid,
vault, router, memo or wallet address can change the source trade.
"""
from __future__ import annotations

import re

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .manual_swap_live_quotes import THOR_ASSETS
from .native_live_order_journal import _valid_source_hash
from .native_trade_receipts import _valid_native_hash
from .storage import _db_url,_ensure_schema,_safe_did

_THOR_URL="https://thornode.ninerealms.com/thorchain/tx/status/"
_CHAINS={"BTC":"BTC","SOL":"SOL","XRP":"XRP"}
_VALID_INTENT=re.compile(r"^native_[0-9a-f]{64}$")
_NUMBER=re.compile(r"^[1-9][0-9]{0,34}$")
_VALID_HEX=re.compile(r"^(?:0x)?[0-9A-Fa-f]{64}$")


def _matches_hash(asset:str,actual:object,expected:str)->bool:
    if type(actual) is not str or type(expected) is not str:
        return False
    if asset=="ETH":
        return bool(_VALID_HEX.fullmatch(actual)) and \
            actual.lower().removeprefix("0x")==expected.lower().removeprefix("0x")
    return (actual.upper()==expected.upper()
            if asset in ("BTC","XRP") else actual==expected)


def _valid_memo_destination(memo:object,expected_asset:str,
                            address:str)->bool:
    if type(memo) is not str or not 6<len(memo)<=250:
        return False
    parts=memo.split(":")
    if len(parts)<3 or parts[0].upper() not in ("=","S","SWAP"):
        return False
    return (parts[1].upper()==expected_asset.upper() and
            parts[2].split("/")[0].lower()==address.lower())


def inspect_original_thorchain_status(original:dict,observed:object)->dict:
    """Validate a protocol claim against previously finalized source identity.

    Never mark a destination transfer 'received' from THORChain's own status;
    the real receiving chain must independently confirm its token transfer.
    """
    if (type(original) is not dict
            or original.get("asset") not in _CHAINS
            or original.get("side") not in ("BUY","SELL")
            or original.get("status")!="SOURCE_FINALIZED"
            or type(original.get("source_tx_hash")) is not str
            or not _valid_source_hash(original["asset"],original["side"],
                                      original["source_tx_hash"])):
        raise ValueError("Original sender finality missing or unrecognized")
    if type(observed) is not dict or type(observed.get("tx")) is not dict:
        raise ValueError("THORChain has not observed this original transaction")
    asset=original["asset"]
    tx=observed["tx"]
    side=original["side"]
    source_asset=THOR_ASSETS[asset if side=="SELL" else original["stable"]][0]
    target_asset=THOR_ASSETS[original["stable"] if side=="SELL" else asset][0]
    source_chain=asset if side=="SELL" else "ETH"
    target_chain="ETH" if side=="SELL" else asset
    if (not _matches_hash(asset if side=="SELL" else "ETH",tx.get("id"),original["source_tx_hash"])
            or tx.get("chain")!=source_chain
            or type(tx.get("from_address")) is not str
            or tx["from_address"]!=original["source_wallet"]
            or not _valid_memo_destination(
                tx.get("memo"),target_asset,original["destination_wallet"])):
        raise ValueError("THORChain original account or memo contradicts journal")
    coins=tx.get("coins")
    if (type(coins) is not list or len(coins)!=1
            or type(coins[0]) is not dict
            or coins[0].get("asset")!=source_asset
            or type(coins[0].get("amount")) is not str
            or not _NUMBER.fullmatch(coins[0]["amount"])):
        raise ValueError("THORChain original input amount unverified")
    stages=observed.get("stages")
    if type(stages) is not dict:
        raise ValueError("THORChain inbound stages missing")
    inbound_seen=stages.get("inbound_observed")
    finalized=stages.get("inbound_finalised")
    if (type(inbound_seen) is not dict
            or type(finalized) is not dict
            or type(inbound_seen.get("completed")) is not bool
            or type(finalized.get("completed")) is not bool
            or (finalized["completed"] and not inbound_seen["completed"])):
        raise ValueError("Inconsistent original protocol stages")
    swap=stages.get("swap_finalised")
    if swap is not None and (type(swap) is not dict
                             or type(swap.get("completed")) is not bool):
        raise ValueError("Unverified THORChain swap stage")
    outgoing=observed.get("out_txs") or []
    if type(outgoing) is not list:
        raise ValueError("Malformed THORChain outbound list")
    out_ids=[]
    out_amounts=[]
    for transaction in outgoing:
        if (type(transaction) is not dict or transaction.get("chain")!=target_chain
                or type(transaction.get("to_address")) is not str
                or (transaction["to_address"].lower()!=original["destination_wallet"].lower()
                    if side=="SELL" else
                    transaction["to_address"]!=original["destination_wallet"])):
            raise ValueError("THORChain outbound targets a different owner")
        outputs=transaction.get("coins")
        if (type(outputs) is not list or not outputs
                or any(type(x) is not dict or x.get("asset")!=target_asset
                       or type(x.get("amount")) is not str
                       or not _NUMBER.fullmatch(x["amount"])
                       for x in outputs)):
            raise ValueError("Unexpected THORChain outbound settlement asset")
        hash_=transaction.get("id")
        if (type(hash_) is not str or
                not (_VALID_HEX.fullmatch(hash_) if side=="SELL" or asset=="BTC"
                     else _valid_native_hash(asset,hash_))):
            raise ValueError("THORChain outbound transaction id unavailable")
        out_ids.append(hash_.lower().removeprefix("0x")
                       if side=="SELL" or asset=="BTC" else hash_)
        out_amounts.append(str(sum(int(output["amount"]) for output in outputs)))
    return {
        "mode":"THORCHAIN_ORIGINAL_TRANSACTION_READ_ONLY",
        "asset":asset,
        "source_tx_hash":original["source_tx_hash"],
        "source_wallet_original":True,
        "destination_owner_bound":True,
        "protocol_inbound_observed":inbound_seen["completed"],
        "protocol_inbound_finalized":finalized["completed"],
        "protocol_reports_swap_finalized":(
            swap is not None and swap["completed"] is True),
        "protocol_outbound_hashes":out_ids,
        "protocol_outbound_units_1e8":out_amounts,
        "side":side,
        "destination_chain_receipts_verified":False,
        "realized_pnl_verified":False,"fee_assessed":False,
        "retry_permitted":False,"new_transactions_sent":0,
    }


def _read_original_locked(cur,*,did:str,asset:str,intent_key:str)->dict:
    cur.execute(
        """SELECT privy_did,asset,intent_key,source_tx_hash,
                  source_wallet,destination_wallet,stable,side,status
           FROM mininex_live_native_order_intents
           WHERE privy_did=%s AND asset=%s AND intent_key=%s""",
        (did,asset,intent_key))
    result=cur.fetchone()
    if (type(result) is not dict or result.get("status")!="SOURCE_FINALIZED"
            or result.get("privy_did")!=did or result.get("asset")!=asset
            or result.get("intent_key")!=intent_key):
        raise HTTPException(409,"Original native source finality unavailable")
    return result


async def get_private_original_protocol_status(
    did:str,*,asset:str,intent_key:str,
)->dict:
    """Account-scoped, read-only chain/protocol observation; no transaction."""
    did=_safe_did(did)
    if (asset not in _CHAINS or type(intent_key) is not str
            or not _VALID_INTENT.fullmatch(intent_key)):
        raise HTTPException(422,"Invalid original native order reference")
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            original=_read_original_locked(
                cur,did=did,asset=asset,intent_key=intent_key)
    tx=original["source_tx_hash"]
    try:
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(9),follow_redirects=False) as client:
            response=await client.get(_THOR_URL+tx,headers={
                "Accept":"application/json"})
            response.raise_for_status()
            if len(response.content)>100_000:
                raise ValueError("Oversized THORChain tx status")
            observed=response.json()
        return inspect_original_thorchain_status(original,observed)
    except (httpx.HTTPError,ValueError,TypeError,KeyError):
        raise HTTPException(
            503,"Original THORChain transaction state unavailable") from None
