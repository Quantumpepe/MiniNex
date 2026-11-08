"""Read-only, untrusted-address blockchain explorer preview.

Never treats an address provided by a client as wallet ownership. No funds,
signatures, wallet authorization, trade execution, or tax ledger insertion.
Only fixed public provider URLs are requested, with strict timeouts and bounds.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re

import httpx
from fastapi import HTTPException

BTC_RE = re.compile(r"^(?:bc1q[ac-hj-np-z02-9]{38,59}|bc1p[ac-hj-np-z02-9]{58}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})$")
ETH_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
HEX64 = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")
BTC_API = "https://mempool.space/api"
ETH_API = "https://eth.blockscout.com/api/v2"
MAX_RETURNED = 20


def check_address(asset: str, address: str) -> str:
    if asset not in ("BTC", "ETH"):
        raise HTTPException(404, "Only BTC and ETH supported")
    if not isinstance(address, str) or not (
        BTC_RE.fullmatch(address) if asset == "BTC" else ETH_RE.fullmatch(address)
    ):
        raise HTTPException(422, "Invalid mainnet address")
    return address


def _btc_tx(row: dict, address: str) -> dict | None:
    if not isinstance(row, dict) or not HEX64.fullmatch(str(row.get("txid", ""))):
        return None
    status = row.get("status") or {}
    confirmed = status.get("confirmed") is True and isinstance(status.get("block_time"), int)
    incoming = sum(
        int(out.get("value", 0)) for out in row.get("vout", [])
        if isinstance(out, dict) and out.get("scriptpubkey_address") == address
        and isinstance(out.get("value"), int) and out["value"] >= 0
    )
    outgoing = sum(
        int(item["prevout"]["value"]) for item in row.get("vin", [])
        if isinstance(item, dict) and isinstance(item.get("prevout"), dict)
        and item["prevout"].get("scriptpubkey_address") == address
        and isinstance(item["prevout"].get("value"), int) and item["prevout"]["value"] >= 0
    )
    net = incoming - outgoing
    if net == 0:
        return None
    value = Decimal(abs(net)) / Decimal(100_000_000)
    return {
        "id": row["txid"], "asset": "BTC",
        "direction": "IN" if net > 0 else "OUT",
        "quantity": format(value, "f"),
        "confirmed": confirmed,
        "timestamp": datetime.fromtimestamp(status["block_time"], tz=timezone.utc).isoformat() if confirmed else None,
        "explorer_url": f"https://mempool.space/tx/{row['txid']}",
        "network": "bitcoin",
    }


def _eth_tx(row: dict, address: str) -> dict | None:
    if not isinstance(row, dict):
        return None
    tx_hash = str(row.get("hash", ""))
    if not tx_hash.startswith("0x") or not HEX64.fullmatch(tx_hash):
        return None
    try:
        from_addr = str((row.get("from") or {}).get("hash", "")).lower()
        to_addr = str((row.get("to") or {}).get("hash", "")).lower()
        value_wei = int(row.get("value", "0"))
        if value_wei < 0 or (from_addr != address.lower() and to_addr != address.lower()):
            return None
        if value_wei == 0:
            return None  # Contract calls/token transfers are NOT native ETH transfers.
        timestamp = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            return None
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        return None
    confirmed = str(row.get("status", "")).lower() in ("ok", "success", "1") and bool(row.get("block_number"))
    return {
        "id": tx_hash, "asset": "ETH",
        "direction": "OUT" if from_addr == address.lower() else "IN",
        "quantity": format(Decimal(value_wei) / Decimal(10**18), "f"),
        "confirmed": confirmed,
        "timestamp": timestamp.astimezone(timezone.utc).isoformat() if confirmed else None,
        "explorer_url": f"https://eth.blockscout.com/tx/{tx_hash}",
        "network": "ethereum",
    }


async def fetch_preview(asset: str, address: str) -> dict:
    address = check_address(asset, address)
    url = (
        f"{BTC_API}/address/{address}/txs"
        if asset == "BTC" else
        f"{ETH_API}/addresses/{address}/transactions"
    )
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(9.0), follow_redirects=False) as client:
            response = await client.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
            data = response.json()
        if asset == "BTC":
            if not isinstance(data, list):
                raise ValueError("Unexpected BTC explorer response")
            rows = data
            parse = _btc_tx
        else:
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise ValueError("Unexpected ETH explorer response")
            rows = data["items"]
            parse = _eth_tx
        output = []
        for row in rows[:60]:
            parsed = parse(row, address)
            if parsed:
                output.append(parsed)
            if len(output) == MAX_RETURNED:
                break
        return {
            "asset": asset,
            "address": address,
            "items": output,
            "preview_only": True,
            "wallet_ownership_verified": False,
            "tax_complete": False,
            "page_complete": False,
            "warning": "Public explorer preview. Not proof of ownership or a complete tax history.",
        }
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise HTTPException(503, "Blockchain explorer temporarily unavailable") from None
