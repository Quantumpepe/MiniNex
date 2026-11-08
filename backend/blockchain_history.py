"""Read-only paginated public history for BTC, ETH, mainnet USDC and USDT.

Bounded requests, hardcoded explorer origins and token contract allowlist.
Results are *not* proof of the authenticated user's ownership, and are never
automatically inserted into a verified tax ledger.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from decimal import Decimal
import json
import re

import httpx
from fastapi import HTTPException

from .blockchain_preview import BTC_API, ETH_API, BTC_RE, ETH_RE, HEX64, _btc_tx, _eth_tx

CONTRACTS = {
    "USDC": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    "USDT": "0xdac17f958d2ee523a2206206994597c13d831ec7",
}
PAGE_LIMIT = 50
_ALLOWED_KEYS = {"block_number", "index", "items_count", "transaction_hash", "batch_block_hash",
                 "batch_transaction_hash", "batch_log_index", "log_index"}


def validate_request(asset: str, address: str, cursor: str | None = None) -> tuple[str, str, dict]:
    if asset not in ("BTC", "ETH", "USDC", "USDT"):
        raise HTTPException(404, "Unsupported asset")
    if not isinstance(address, str) or not (BTC_RE.fullmatch(address) if asset=="BTC" else ETH_RE.fullmatch(address)):
        raise HTTPException(422, "Invalid mainnet address")
    if not cursor:
        return asset, address, {}
    if len(cursor)>900 or not re.fullmatch(r"[A-Za-z0-9_=-]+",cursor):
        raise HTTPException(422, "Invalid page cursor")
    if asset=="BTC":
        if not HEX64.fullmatch(cursor) or cursor.startswith("0x"):
            raise HTTPException(422, "Invalid Bitcoin cursor")
        return asset,address,{"after_txid":cursor}
    try:
        raw=base64.urlsafe_b64decode(cursor+"="*(-len(cursor)%4))
        decoded=json.loads(raw)
        if not isinstance(decoded,dict) or not decoded or set(decoded)-_ALLOWED_KEYS:
            raise ValueError("invalid page")
        for key,val in decoded.items():
            if isinstance(val,bool) or not isinstance(val,(int,str)):
                raise ValueError("invalid value")
            if isinstance(val,int) and not 0<=val<10**14:
                raise ValueError("invalid number")
            if isinstance(val,str) and (len(val)>85 or not re.fullmatch(r"0x[a-fA-F0-9]+|[0-9]+",val)):
                raise ValueError("invalid string")
    except (ValueError,TypeError,UnicodeError,OverflowError):
        raise HTTPException(422, "Invalid page cursor") from None
    return asset,address,decoded


def encode_cursor(params: dict | None) -> str | None:
    if not isinstance(params,dict) or not params:
        return None
    raw=json.dumps(params,separators=(",",":"),sort_keys=True).encode()
    if len(raw)>600:
        return None
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def parse_token(row: dict, address: str, asset: str) -> dict | None:
    if not isinstance(row,dict):
        return None
    token=row.get("token") or {}
    if not isinstance(token,dict) or str(token.get("address_hash","")).lower()!=CONTRACTS[asset]:
        return None
    if str(token.get("type","")).upper()!="ERC-20":
        return None
    tx_hash=str(row.get("tx_hash") or row.get("transaction_hash") or "")
    if not HEX64.fullmatch(tx_hash) or not tx_hash.startswith("0x"):
        return None
    try:
        src=str((row.get("from") or {}).get("hash","")).lower()
        dest=str((row.get("to") or {}).get("hash","")).lower()
        if src!=address.lower() and dest!=address.lower():
            return None
        # Blockscout uses total.value and total.decimals in token transfers.
        total=row.get("total")
        if not isinstance(total,dict):
            return None
        units=int(total["value"])
        decimals=int(total.get("decimals") if total.get("decimals") is not None else token["decimals"])
        if units<0 or not 0<=decimals<=30:
            return None
        index=int(row["log_index"])
        if index<0 or index>100000:
            return None
        time_raw=str(row["timestamp"])
        stamp=datetime.fromisoformat(time_raw.replace("Z","+00:00"))
        if stamp.tzinfo is None:
            return None
        confirmed=bool(row.get("block_number"))
        value=Decimal(units)/Decimal(10)**decimals
    except (ValueError,TypeError,KeyError,OverflowError,AttributeError):
        return None
    if not value:
        return None
    return {
        "id":f"{tx_hash}:{index}", "tx_hash":tx_hash, "asset":asset,
        "direction":"OUT" if src==address.lower() else "IN",
        "quantity":format(value,"f"), "confirmed":confirmed,
        "timestamp":stamp.astimezone(timezone.utc).isoformat() if confirmed else None,
        "explorer_url":f"https://eth.blockscout.com/tx/{tx_hash}",
        "network":"ethereum",
    }


async def fetch_history(asset: str, address: str, cursor: str | None = None) -> dict:
    asset,address,params=validate_request(asset,address,cursor)
    if asset=="BTC":
        url=(f"{BTC_API}/address/{address}/txs/chain/{params['after_txid']}" if params
             else f"{BTC_API}/address/{address}/txs")
        query=None
    elif asset=="ETH":
        url=f"{ETH_API}/addresses/{address}/transactions"
        query=params or None
    else:
        url=f"{ETH_API}/addresses/{address}/token-transfers"
        query={"type":"ERC-20","token":CONTRACTS[asset],**params}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10),follow_redirects=False) as client:
            response=await client.get(url,params=query,headers={"Accept":"application/json"})
            response.raise_for_status()
            payload=response.json()
        if asset=="BTC":
            if not isinstance(payload,list):
                raise ValueError("Unexpected Bitcoin response")
            rows=payload
            raw_items=[_btc_tx(row,address) for row in rows[:75]]
            confirmed=[r for r in rows if isinstance(r,dict) and (r.get("status") or {}).get("confirmed") is True]
            next_cursor=(str(confirmed[-1].get("txid")) if len(confirmed)>=25 and
                         HEX64.fullmatch(str(confirmed[-1].get("txid",""))) else None)
        else:
            if not isinstance(payload,dict) or not isinstance(payload.get("items"),list):
                raise ValueError("Unexpected Ethereum response")
            rows=payload["items"][:PAGE_LIMIT]
            parser=(lambda row:_eth_tx(row,address)) if asset=="ETH" else (lambda row:parse_token(row,address,asset))
            raw_items=[parser(row) for row in rows]
            next_cursor=encode_cursor(payload.get("next_page_params"))
        items=[]
        for item in raw_items:
            if item:
                items.append(item)
        return {
            "asset":asset,"address":address,"items":items,
            "next_cursor":next_cursor,"preview_only":True,
            "wallet_ownership_verified":False,"tax_complete":False,
            "page_complete":next_cursor is None,
            "warning":"Explorer view only; no confirmed ownership or full tax coverage.",
        }
    except (httpx.HTTPError,ValueError,TypeError,OverflowError):
        raise HTTPException(503,"Blockchain explorer unavailable") from None
