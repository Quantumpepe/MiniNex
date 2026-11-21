"""Bind the original Privy Ethereum transaction nonce to its durable journal.

Original native THORChain BUY and finite ERC20 approval sends MUST use the
same nonce on-chain as in the nonce-locked sender journal. This pure,
Ethereum-mainnet-only serializer never signs, approves, or broadcasts.
"""
from __future__ import annotations
import re

_URL=re.compile(r"^https://api\.privy\.io/v1/wallets/[A-Za-z0-9_-]{6,160}/rpc$")
_ADDRESS=re.compile(r"^0x[0-9a-fA-F]{40}$")
_BYTES=re.compile(r"^0x(?:[0-9a-fA-F]{2})*$")


def pin_original_privy_eth_nonce(url:str,body:dict,nonce:int)->dict:
    """Return a new exact eth_sendTransaction body with the observed nonce."""
    if (type(url) is not str or not _URL.fullmatch(url)
            or type(nonce) is not int or not 0<=nonce<2**64
            or type(body) is not dict
            or set(body)!={"method","caip2","params"}
            or body.get("method")!="eth_sendTransaction"
            or body.get("caip2")!="eip155:1"
            or type(body.get("params")) is not dict
            or set(body["params"])!={"transaction"}
            or type(body["params"]["transaction"]) is not dict):
        raise ValueError("Original Privy Ethereum nonce or envelope invalid")
    tx=body["params"]["transaction"]
    if (set(tx)!={"to","data","value"}
            or type(tx.get("to")) is not str
            or not _ADDRESS.fullmatch(tx["to"])
            or type(tx.get("data")) is not str
            or not _BYTES.fullmatch(tx["data"])
            or tx.get("value")!="0"):
        raise ValueError("Original ETH owner transaction calldata unavailable")
    return {**body,"params":{"transaction":{**tx,"nonce":hex(nonce)}}}
