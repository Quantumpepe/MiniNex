"""Persist bounded confirmed blockchain transfers ONLY after server-side Privy ownership check.

Each call imports at most one explorer page. No simulated trades, unconfirmed
events, token spoofing, inferred USD/CHF valuations or signing.
"""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import logging

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .blockchain_history import fetch_history, validate_request
from .privy_wallet_verification import verify_wallet_address
from .storage import _db_url, _ensure_schema, _prepare_rows, _safe_did

LOG=logging.getLogger("mininex.verified_sync")


def checked_entry(item: dict, asset: str) -> tuple | None:
    if not isinstance(item,dict) or item.get("asset")!=asset or item.get("confirmed") is not True:
        return None
    try:
        when=datetime.fromisoformat(item["timestamp"])
        if when.tzinfo is None:
            return None
        when=when.astimezone(timezone.utc)
        amount=Decimal(str(item["quantity"]))
        if not amount.is_finite() or not 0<amount<Decimal("1e18"):
            return None
        direction=item["direction"]
        if direction not in ("IN","OUT"):
            return None
        tx_hash=item.get("tx_hash") or item["id"]
        if asset=="BTC":
            tx_hash=item["id"]
        if not isinstance(tx_hash,str) or not 32<len(tx_hash)<=80:
            return None
        record_id=f"{asset}:{item['id']}"
        if len(record_id)>140:
            return None
        return (record_id,asset,"DEPOSIT" if direction=="IN" else "WITHDRAWAL",
                when,amount,tx_hash,"bitcoin" if asset=="BTC" else "ethereum")
    except (ValueError,TypeError,InvalidOperation,OverflowError,KeyError):
        return None


def store_confirmed_page(did: str, asset: str, items: list[dict]) -> int:
    did=_safe_did(did)
    if asset not in ("BTC","ETH","USDC","USDT"):
        raise ValueError("Unsupported asset")
    if not isinstance(items,list) or len(items)>75:
        raise ValueError("Invalid page")
    entries=[entry for item in items if (entry:=checked_entry(item,asset)) is not None]
    if not entries:
        return 0
    try:
        url=_db_url()
        _ensure_schema(url)
        count=0
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur,did)
                for row in entries:
                    cur.execute(
                        """INSERT INTO mininex_verified_activity
                           (privy_did,source,record_id,asset,activity_type,occurred_at,
                            quantity,tx_hash,network,verification)
                           VALUES (%s,'BLOCKCHAIN',%s,%s,%s,%s,%s,%s,%s,'CONFIRMED')
                           ON CONFLICT (privy_did,source,record_id) DO NOTHING
                           RETURNING record_id""",
                        (did,*row),
                    )
                    if cur.fetchone():
                        count+=1
        return count
    except psycopg.Error:
        LOG.warning("MiniNex blockchain storage unavailable")
        raise HTTPException(503,"Blockchain history storage unavailable") from None


async def synchronize_one_page(did: str, asset: str, address: str, cursor: str | None) -> dict:
    _safe_did(did)
    validate_request(asset,address,cursor)
    # Critical: verified JWT DID is not sufficient; Privy must also confirm wallet ownership.
    await verify_wallet_address(did,asset,address)
    page=await fetch_history(asset,address,cursor)
    if page["address"]!=address or page["asset"]!=asset:
        raise HTTPException(503,"Explorer identity mismatch")
    count=store_confirmed_page(did,asset,page["items"])
    return {
        "asset":asset,"saved":count,"next_cursor":page["next_cursor"],
        "wallet_ownership_verified":True,"tax_complete":False,
        "page_complete":page["page_complete"],
        "warning":"One page archived. Historical pagination, token coverage and CHF valuations still require reconciliation.",
    }
