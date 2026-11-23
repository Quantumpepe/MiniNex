"""Read-only recent transaction views per mainnet, scoped to the Privy owner.

No send/sign/approve/sync/tax storage. Providers are fixed by chain, never
client URLs. Public explorer rows remain preview data, not complete accounting.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import os
import re

import httpx
from fastapi import HTTPException

from .blockchain_history import fetch_history
from .native_coin_balances import verified_native_addresses
from .privy_wallet_verification import fetch_privy_user
from .storage import _safe_did
from .wallet_balances import verified_addresses

SUPPORTED = ("BTC", "ETH", "BNB", "POL", "AVAX", "ARB", "SOL", "XRP", "TRX")
_EVM_EXPLORERS = {
    "ETH": ("https://eth.blockscout.com/api/v2", "https://etherscan.io", "ETH"),
    "BNB": (None, "https://bscscan.com", "BNB"),
    "POL": ("https://polygon.blockscout.com/api/v2", "https://polygonscan.com", "POL"),
    "AVAX": ("https://c.snowtrace.io/api/v2", "https://snowtrace.io", "AVAX"),
    "ARB": ("https://arbitrum.blockscout.com/api/v2", "https://arbiscan.io", "ETH"),
}
_NATIVE_EXPLORERS = {
    "BTC": "https://mempool.space", "SOL": "https://solscan.io",
    "XRP": "https://xrpscan.com", "TRX": "https://tronscan.org",
}
_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
_HEX = re.compile(r"^[0-9a-fA-F]{64}$")
_SOL_SIG = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{75,90}$")
_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")
_TOKEN_REGISTRY = {
    "ETH": {
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": ("USDC", 6),
        "0xdac17f958d2ee523a2206206994597c13d831ec7": ("USDT", 6),
        "0x514910771af9ca656af840dff83e8264ecf986ca": ("LINK", 18),
        "0x4a220e6096b25eadb88358cb44068a3248254675": ("QNT", 18),
        "0x1f9840a85d5af5bf1d1762f925bddadc4201f984": ("UNI", 18),
    },
    "POL": {
        "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270": ("WPOL", 18),
        "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": ("USDC", 6),
    },
    "BNB": {
        "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c": ("WBNB", 18),
        "0x55d398326f99059ff775485246999027b3197955": ("USDT", 18),
    },
    "AVAX": {
        "0xb31f66aa3c1e785363f0875a1b74e27b85fd66c7": ("WAVAX", 18),
        "0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e": ("USDC", 6),
    },
    "ARB": {
        "0x912ce59144191c1204e64559fe8253a0e49e6548": ("ARB", 18),
        "0xaf88d065e77c8cc2239327c5edb3a432268e5831": ("USDC", 6),
        "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9": ("USDT", 6),
    },
}

def _iso(raw: object) -> str | None:
    if not isinstance(raw, str) or len(raw) > 45 or not _DATE_RE.match(raw):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
    except ValueError:
        return None


def _amount(units: object, decimals: int) -> str | None:
    if type(units) not in (int, str) or not str(units).isdigit() or len(str(units)) > 70:
        return None
    try:
        value = Decimal(str(units)) / (Decimal(10) ** decimals)
        return format(value, "f") if value > 0 else None
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _address_from_obj(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("hash") or "").lower()
    return str(value or "").lower() if isinstance(value, str) else ""


def evm_transaction(row: object, address: str, chain: str) -> dict | None:
    if not isinstance(row, dict) or chain not in _EVM_EXPLORERS:
        return None
    tx = row.get("hash")
    if type(tx) is not str or not _HASH.fullmatch(tx):
        return None
    src, dst = _address_from_obj(row.get("from")), _address_from_obj(row.get("to"))
    if address.lower() not in (src, dst):
        return None
    method = row.get("method")
    if not isinstance(method, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,55}", method):
        method = "Contract call"
    amount = _amount(row.get("value", "0"), 18)
    wrapped = _TOKEN_REGISTRY.get(chain, {})
    is_wrap = (chain in ("POL", "BNB", "AVAX") and src == address.lower()
               and amount and method.lower() == "deposit"
               and dst in wrapped and wrapped[dst][0] == "W" + chain)
    raw_status = str(row.get("status") or "").lower()
    result = str(row.get("result") or "").lower()
    failed = raw_status in ("error", "failed") or result in ("error", "failed")
    confirmed = bool(row.get("block_number")) and not failed
    explorer = _EVM_EXPLORERS[chain][1]
    return {
        "hash": tx.lower(), "chain": chain,
        "type": "WRAP" if is_wrap else ("TRANSFER" if amount else "CONTRACT"),
        "label": ("Wrap " + chain + " → W" + chain) if is_wrap else
                 ("Transfer" if amount else method),
        "direction": "OUT" if src == address.lower() else "IN",
        "quantity": amount,
        "asset": _EVM_EXPLORERS[chain][2] if amount else None,
        "timestamp": _iso(row.get("timestamp")), "confirmed": confirmed,
        "failed": failed, "tokens": [],
        "explorer_url": explorer + "/tx/" + tx.lower(),
    }


def evm_token_transfer(row: object, address: str, chain: str) -> dict | None:
    if not isinstance(row, dict):
        return None
    tx = row.get("tx_hash") or row.get("transaction_hash")
    if type(tx) is not str or not _HASH.fullmatch(tx):
        return None
    token = row.get("token")
    if not isinstance(token, dict) or str(token.get("type") or "").upper() != "ERC-20":
        return None
    known = _TOKEN_REGISTRY.get(chain, {}).get(
        str(token.get("address_hash") or "").lower())
    if not known:
        return None
    src, dest = _address_from_obj(row.get("from")), _address_from_obj(row.get("to"))
    if address.lower() not in (src, dest):
        return None
    total = row.get("total")
    if not isinstance(total, dict) or type(total.get("value")) not in (int, str):
        return None
    quantity = _amount(total.get("value"), known[1])
    if not quantity:
        return None
    if total.get("decimals") is not None:
        try:
            if int(total["decimals"]) != known[1]:
                return None
        except (TypeError, ValueError):
            return None
    return {
        "hash": tx.lower(), "asset": known[0], "quantity": quantity,
        "direction": "OUT" if src == address.lower() else "IN",
        "timestamp": _iso(row.get("timestamp")),
        "confirmed": bool(row.get("block_number")),
    }


def combine_evm_events(transactions: list, transfers: list, chain: str) -> list:
    grouped = {}
    for item in transactions:
        if item:
            grouped[item["hash"]] = dict(item)
    for token in transfers:
        if not token:
            continue
        tx = token["hash"]
        row = grouped.get(tx)
        if row is None:
            row = {
                "hash": tx, "chain": chain, "type": "TOKEN",
                "label": "Token transfer", "direction": token["direction"],
                "quantity": token["quantity"], "asset": token["asset"],
                "timestamp": token["timestamp"], "confirmed": token["confirmed"],
                "failed": False, "tokens": [],
                "explorer_url": _EVM_EXPLORERS[chain][1] + "/tx/" + tx,
            }
            grouped[tx] = row
        row["tokens"].append({
            "asset": token["asset"], "quantity": token["quantity"],
            "direction": token["direction"],
        })
        if not row["timestamp"]:
            row["timestamp"] = token["timestamp"]
        row["confirmed"] = row["confirmed"] or token["confirmed"]
    return sorted(grouped.values(),
                  key=lambda item: (item.get("timestamp") or "", item["hash"]),
                  reverse=True)[:40]


def _explorer_address(chain: str, address: str) -> str:
    if chain in _EVM_EXPLORERS:
        return _EVM_EXPLORERS[chain][1] + "/address/" + address
    return {
        "BTC": "https://mempool.space/address/",
        "SOL": "https://solscan.io/account/",
        "XRP": "https://xrpscan.com/account/",
        "TRX": "https://tronscan.org/#/address/",
    }[chain] + address


async def _blockscout(client: httpx.AsyncClient, chain: str, address: str) -> tuple[list, str]:
    base = _EVM_EXPLORERS[chain][0]
    if not base:
        return [], "unavailable"
    endpoint = base + "/addresses/" + address
    async def fetch(suffix: str, params: dict | None = None):
        response = await client.get(endpoint + suffix, params=params,
                                    headers={"Accept": "application/json"})
        response.raise_for_status()
        if len(response.content) > 750_000:
            raise ValueError("Oversized public explorer page")
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("Unexpected public explorer page")
        return data["items"][:50]
    pages = await asyncio.gather(fetch("/transactions"),
                                 fetch("/token-transfers", {"type": "ERC-20"}),
                                 return_exceptions=True)
    good = []
    for data, parser in zip(pages, (evm_transaction, evm_token_transfer)):
        if isinstance(data, BaseException):
            good.append(None)
        else:
            good.append([parser(row, address, chain) for row in data])
    if all(item is None for item in good):
        return [], "unavailable"
    return combine_evm_events(good[0] or [], good[1] or [], chain), (
        "partial" if any(item is None for item in good) else "ready")


def _read_sol(data: object) -> list:
    if not isinstance(data, dict) or data.get("jsonrpc") != "2.0" or data.get("id") != 1 \
       or "error" in data or not isinstance(data.get("result"), list):
        raise ValueError("Invalid Solana signature response")
    items = []
    for row in data["result"][:40]:
        if not isinstance(row, dict):
            continue
        sig = row.get("signature")
        if type(sig) is not str or not _SOL_SIG.fullmatch(sig):
            continue
        time = row.get("blockTime")
        stamp = (datetime.fromtimestamp(time, timezone.utc).isoformat()
                 if type(time) is int and time > 0 else None)
        items.append({
            "hash": sig, "chain": "SOL", "type": "TRANSACTION",
            "label": "Solana transaction", "direction": None,
            "quantity": None, "asset": "SOL", "timestamp": stamp,
            "confirmed": row.get("confirmationStatus") == "finalized",
            "failed": row.get("err") is not None, "tokens": [],
            "explorer_url": "https://solscan.io/tx/" + sig,
        })
    return items


def _read_xrp(data: object, address: str) -> list:
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict) or result.get("status") != "success" or \
       not isinstance(result.get("transactions"), list):
        raise ValueError("Invalid XRP account transactions")
    items = []
    for row in result["transactions"][:40]:
        if not isinstance(row, dict):
            continue
        tx = row.get("tx_json") or row.get("tx")
        if not isinstance(tx, dict):
            continue
        h = tx.get("hash") or row.get("hash")
        if type(h) is not str or not _HEX.fullmatch(h):
            continue
        kind = tx.get("TransactionType")
        if type(kind) is not str or not re.fullmatch(r"[A-Za-z]{2,35}", kind):
            kind = "Transaction"
        out = tx.get("Account") == address
        incoming = tx.get("Destination") == address
        if not (out or incoming):
            continue
        quantity = (_amount(tx.get("Amount"), 6)
                    if kind == "Payment" and type(tx.get("Amount")) is str else None)
        date = tx.get("date")
        stamp = (datetime.fromtimestamp(date + 946684800, timezone.utc).isoformat()
                 if type(date) is int and date >= 0 else None)
        items.append({
            "hash": h.upper(), "chain": "XRP", "type": kind.upper(), "label": kind,
            "direction": "OUT" if out else "IN", "quantity": quantity,
            "asset": "XRP" if quantity else None, "timestamp": stamp,
            "confirmed": row.get("validated") is True,
            "failed": (row.get("meta", {}).get("TransactionResult") not in
                       (None, "tesSUCCESS")) if isinstance(row.get("meta"), dict) else False,
            "tokens": [], "explorer_url": "https://xrpscan.com/tx/" + h.upper(),
        })
    return items


def _read_trx(data: object) -> list:
    if not isinstance(data, dict) or data.get("success") is not True or \
       not isinstance(data.get("data"), list):
        raise ValueError("Invalid TRON history")
    items = []
    for row in data["data"][:40]:
        if not isinstance(row, dict):
            continue
        h = row.get("txID")
        if type(h) is not str or not _HEX.fullmatch(h):
            continue
        raw = row.get("raw_data") or {}
        contracts = raw.get("contract") if isinstance(raw, dict) else None
        action = (contracts[0].get("type") if isinstance(contracts, list) and
                  contracts and isinstance(contracts[0], dict) else "Transaction")
        if not isinstance(action, str) or not re.fullmatch(r"[A-Za-z0-9_]{2,55}", action):
            action = "Transaction"
        timestamp = raw.get("timestamp") if isinstance(raw, dict) else None
        stamp = (datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat()
                 if type(timestamp) is int and timestamp > 0 else None)
        status = row.get("ret") if isinstance(row.get("ret"), list) else []
        failed = bool(status and isinstance(status[0], dict)
                      and status[0].get("contractRet") not in (None, "SUCCESS"))
        items.append({
            "hash": h.lower(), "chain": "TRX", "type": "TRANSACTION",
            "label": action, "direction": None, "quantity": None,
            "asset": "TRX", "timestamp": stamp, "confirmed": True,
            "failed": failed, "tokens": [],
            "explorer_url": "https://tronscan.org/#/transaction/" + h.lower(),
        })
    return items


async def _native(client: httpx.AsyncClient, chain: str, address: str) -> tuple[list, str]:
    if chain == "SOL":
        resp = await client.post(
            os.environ.get("MININEX_SOLANA_RPC_URL", "").strip() or
            "https://api.mainnet.solana.com",
            json={"jsonrpc": "2.0", "id": 1,
                  "method": "getSignaturesForAddress",
                  "params": [address, {"limit": 35, "commitment": "finalized"}]},
            headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        if len(resp.content) > 200_000:
            raise ValueError("Oversized Solana RPC response")
        return _read_sol(resp.json()), "ready"
    if chain == "XRP":
        resp = await client.post(
            os.environ.get("MININEX_XRPL_RPC_URL", "").strip() or
            "https://s1.ripple.com:51234/",
            json={"method": "account_tx", "params": [
                {"account": address, "ledger_index_min": -1,
                 "ledger_index_max": -1, "limit": 35}]},
            headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        if len(resp.content) > 220_000:
            raise ValueError("Oversized XRPL account response")
        return _read_xrp(resp.json(), address), "ready"
    if chain == "TRX":
        resp = await client.get(
            "https://api.trongrid.io/v1/accounts/" + address + "/transactions",
            params={"only_confirmed": "true", "limit": 35},
            headers={"Accept": "application/json"})
        resp.raise_for_status()
        if len(resp.content) > 250_000:
            raise ValueError("Oversized TRON account response")
        return _read_trx(resp.json()), "ready"
    raise ValueError("Unsupported native chain")


async def read_chain_transactions(did: str, chain: str) -> dict:
    """One operator-fixed mainnet/owner view; no arbitrary input address."""
    _safe_did(did)
    if chain not in SUPPORTED:
        raise HTTPException(404, "Unsupported blockchain network")
    user = await fetch_privy_user(did)
    try:
        if chain in ("BTC", "ETH", "BNB", "POL", "AVAX", "ARB"):
            account = verified_addresses(user, did)
            address = account["BTC" if chain == "BTC" else "ETH"]
        else:
            address = verified_native_addresses(user, did)[chain]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(503, "Cannot independently identify account wallet") from None
    if not address:
        return {"chain": chain, "items": [], "source_status": "wallet_missing",
                "wallet_ownership_verified": True, "preview_only": True,
                "tax_complete": False, "history_complete": False,
                "explorer_address_url": None}
    address_url = _explorer_address(chain, address)
    items, state = [], "unavailable"
    try:
        if chain == "BTC":
            page = await fetch_history("BTC", address)
            items = [{
                "hash": row["id"], "chain": "BTC", "type": "TRANSFER",
                "label": "Bitcoin transfer", "direction": row["direction"],
                "quantity": row["quantity"], "asset": "BTC",
                "timestamp": row["timestamp"], "confirmed": row["confirmed"],
                "failed": False, "tokens": [],
                "explorer_url": row["explorer_url"],
            } for row in page["items"][:40]]
            state = "ready"
        else:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10),
                                         follow_redirects=False) as client:
                if chain in _EVM_EXPLORERS:
                    items, state = await _blockscout(client, chain, address)
                else:
                    items, state = await _native(client, chain, address)
    except (httpx.HTTPError, ValueError, TypeError, OverflowError, HTTPException):
        state = "unavailable"
    return {
        "chain": chain, "address": address, "items": items[:40],
        "source_status": state, "wallet_ownership_verified": True,
        "preview_only": True, "tax_complete": False,
        "history_complete": False, "explorer_address_url": address_url,
    }
