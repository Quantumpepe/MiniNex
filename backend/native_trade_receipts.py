"""Original SOL and XRPL transaction receipt checks for owner-linked Privy wallets.

A finalized sender-chain receipt is not a THORChain swap fill or stablecoin
settlement. This module NEVER broadcasts, signs, alters trades or charges fees.
"""
from __future__ import annotations

import re
import httpx
from fastapi import HTTPException

from .native_coin_balances import (
    CHAINS,_valid_address,verified_native_addresses,_rpc_url,
)
from .privy_wallet_verification import fetch_privy_user
from .storage import _safe_did

_XRP_TX=re.compile(r"^[A-Fa-f0-9]{64}$")
_SOL_BASE58="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_SOL_SIG=re.compile(r"^[1-9A-HJ-NP-Za-km-z]{85,90}$")


def _valid_native_hash(asset:str,value:object)->bool:
    if type(value) is not str:
        return False
    if asset=="XRP":
        return bool(_XRP_TX.fullmatch(value))
    if asset!="SOL" or not _SOL_SIG.fullmatch(value):
        return False
    n=0
    for c in value:
        n=n*58+_SOL_BASE58.index(c)
    length=(n.bit_length()+7)//8
    zeros=len(value)-len(value.lstrip("1"))
    return length+zeros==64


def inspect_finalized_solana_sender(
    owner:str,signature:str,response:object,
)->dict:
    if not _valid_address("SOL",owner) or not _valid_native_hash("SOL",signature):
        raise ValueError("Invalid Solana owner or signature")
    if type(response) is not dict or response.get("jsonrpc")!="2.0" or response.get("id")!=1:
        raise ValueError("Unverified Solana receipt")
    result=response.get("result")
    if type(result) is not dict or type(result.get("slot")) is not int or result["slot"]<=0:
        raise ValueError("Solana finalized transaction unavailable")
    tx=result.get("transaction")
    meta=result.get("meta")
    if (type(tx) is not dict or type(meta) is not dict
            or "err" not in meta or meta.get("err") is not None
            or type(tx.get("signatures")) is not list
            or signature not in tx["signatures"]):
        raise ValueError("Solana transaction identity or success not proven")
    message=tx.get("message")
    keys=message.get("accountKeys") if type(message) is dict else None
    pre=meta.get("preBalances")
    post=meta.get("postBalances")
    fee=meta.get("fee")
    if (type(keys) is not list or type(pre) is not list
            or type(post) is not list or len(keys)!=len(pre) or len(keys)!=len(post)
            or type(fee) is not int or fee<0):
        raise ValueError("Solana sender balance evidence unavailable")
    found=[]
    for i,key in enumerate(keys):
        if (type(key) is dict and key.get("pubkey")==owner
                and key.get("signer") is True):
            found.append(i)
    if len(found)!=1:
        raise ValueError("Privy Solana wallet did not sign the transaction")
    i=found[0]
    if (type(pre[i]) is not int or type(post[i]) is not int
            or pre[i]<0 or post[i]<0):
        raise ValueError("Malformed Solana account balance")
    return {
        "asset":"SOL","network":"solana-mainnet",
        "source_wallet_signature_verified":True,
        "source_chain_finalized":True,
        "source_wallet_balance_before_lamports":str(pre[i]),
        "source_wallet_balance_after_lamports":str(post[i]),
        "source_wallet_fee_payer":i==0,
        "network_fee_lamports":str(fee if i==0 else 0),
        "source_chain_slot":result["slot"],
        "cross_chain_swap_completed":False,
        "destination_stablecoin_received":False,
        "wallet_transaction_sent_by_this_check":False,
    }


def inspect_validated_xrp_sender(owner:str,tx_hash:str,response:object)->dict:
    if not _valid_address("XRP",owner) or not _valid_native_hash("XRP",tx_hash):
        raise ValueError("Invalid XRPL owner or hash")
    if type(response) is not dict:
        raise ValueError("Invalid XRPL RPC response")
    result=response.get("result")
    if type(result) is not dict or result.get("validated") is not True:
        raise ValueError("XRPL transaction is not in a validated ledger")
    tx=result.get("tx_json",result)
    meta=result.get("meta")
    if (type(tx) is not dict or type(meta) is not dict
            or tx.get("Account")!=owner
            or tx.get("TransactionType")!="Payment"
            or str(tx.get("hash",result.get("hash",""))).upper()!=tx_hash.upper()
            or meta.get("TransactionResult")!="tesSUCCESS"):
        raise ValueError("Privy XRPL outgoing payment was not verified")
    ledger=result.get("ledger_index")
    fee=tx.get("Fee")
    if (type(ledger) is not int or ledger<=0
            or type(fee) is not str or not re.fullmatch(r"[0-9]{1,20}",fee)):
        raise ValueError("XRPL validated ledger and fee unavailable")
    return {
        "asset":"XRP","network":"xrpl-mainnet",
        "source_wallet_transaction_verified":True,
        "source_chain_finalized":True,
        "source_chain_ledger":ledger,
        "network_fee_drops":fee,
        "cross_chain_swap_completed":False,
        "destination_stablecoin_received":False,
        "wallet_transaction_sent_by_this_check":False,
    }


async def get_owner_native_sender_receipt(
    did:str,*,asset:str,transaction_hash:str,
)->dict:
    """Look up a sender-chain tx, only after proving current Privy custody."""
    did=_safe_did(did)
    if asset not in ("SOL","XRP") or not _valid_native_hash(asset,transaction_hash):
        raise HTTPException(422,"Invalid native coin or transaction hash")
    user=await fetch_privy_user(did)
    owned=verified_native_addresses(user,did).get(asset)
    if not owned:
        raise HTTPException(409,"Native Privy sender wallet unavailable")
    method=("getTransaction" if asset=="SOL" else "tx")
    params=([transaction_hash,{"encoding":"jsonParsed",
                 "commitment":"finalized","maxSupportedTransactionVersion":0}]
            if asset=="SOL" else [{"transaction":transaction_hash,"binary":False}])
    payload=({"jsonrpc":"2.0","id":1,"method":method,"params":params}
             if asset=="SOL" else {"method":method,"params":params})
    try:
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(12),follow_redirects=False) as client:
            resp=await client.post(_rpc_url(asset),json=payload,
                       headers={"Content-Type":"application/json",
                                "Accept":"application/json"})
            resp.raise_for_status()
            if len(resp.content)>250_000:
                raise ValueError("Oversized native chain receipt")
            raw=resp.json()
        proof=(inspect_finalized_solana_sender(owned,transaction_hash,raw)
               if asset=="SOL" else
               inspect_validated_xrp_sender(owned,transaction_hash,raw))
    except (httpx.HTTPError,ValueError,TypeError,KeyError,OverflowError):
        raise HTTPException(502,"Verified native transaction receipt unavailable") from None
    return {
        "mode":"OWNER_NATIVE_SOURCE_RECEIPT_READ_ONLY",
        **proof,
        "source_tx_hash":transaction_hash,
        "destination_finality_still_required":True,
        "trading_profit_recorded":False,
        "fee_assessed":False,
        "can_submit":False,
    }
