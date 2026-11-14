"""Real, read-only SOL / XRP / TRX holdings for native Privy wallets.

One account-bound API, three native network adapters. No fake Ethereum address,
wallet signing, token movement, synthetic balance or paper-ledger modification.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
from decimal import Decimal

import httpx

from .privy_wallet_verification import fetch_privy_user
from .storage import _safe_did
from .wallet_balances import _units

ORDER = ("SOL", "XRP", "TRX")
CHAINS = {
    "SOL": ("solana", "solana-mainnet", "https://api.mainnet.solana.com",
            "MININEX_SOLANA_RPC_URL", Decimal(10**9)),
    "XRP": ("xrpl", "xrpl-mainnet", "https://s1.ripple.com:51234/",
            "MININEX_XRPL_RPC_URL", Decimal(10**6)),
    "TRX": ("tron", "tron-mainnet",
            "https://api.trongrid.io/walletsolidity/getaccount",
            "MININEX_TRON_RPC_URL", Decimal(10**6)),
}
_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_RIPPLE = "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"
_MAX_64 = 2**64 - 1


def _decode_b58(address: str, alphabet: str) -> bytes | None:
    if not isinstance(address, str) or not 20 <= len(address) <= 50:
        return None
    n = 0
    for letter in address:
        i = alphabet.find(letter)
        if i < 0:
            return None
        n = n*58+i
    count = len(address)-len(address.lstrip(alphabet[0]))
    hexvalue = f"{n:x}" if n else ""
    if len(hexvalue) % 2:
        hexvalue = "0"+hexvalue
    return bytes(count)+bytes.fromhex(hexvalue)


def _checksum(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()[:4]


def _valid_address(asset: str, address: object) -> bool:
    if not isinstance(address, str):
        return False
    if asset == "SOL":
        if not 32 <= len(address) <= 44:
            return False
        return len(_decode_b58(address, _BASE58) or b"") == 32
    if asset == "XRP":
        if not re.fullmatch(r"r[A-Za-z0-9]{24,34}", address):
            return False
        decoded = _decode_b58(address, _RIPPLE)
        return bool(decoded and len(decoded)==25 and decoded[0]==0
                    and decoded[-4:]==_checksum(decoded[:21]))
    if asset == "TRX":
        if not re.fullmatch(r"T[A-Za-z0-9]{33}", address):
            return False
        decoded = _decode_b58(address, _BASE58)
        return bool(decoded and len(decoded)==25 and decoded[0]==0x41
                    and decoded[-4:]==_checksum(decoded[:21]))
    return False


def verified_native_addresses(user: dict, did: str) -> dict[str, str | None]:
    if not isinstance(user, dict) or user.get("id")!=did:
        raise ValueError("Privy account mismatch")
    accounts = user.get("linked_accounts")
    if not isinstance(accounts, list):
        raise ValueError("Privy linked wallets unavailable")
    seen = {coin:set() for coin in ORDER}
    for wallet in accounts:
        if not isinstance(wallet, dict) or wallet.get("type")!="wallet":
            continue
        # Only linked wallets explicitly marked as Privy; no external keys.
        if wallet.get("wallet_client_type", wallet.get("wallet_client"))!="privy":
            continue
        for coin in ORDER:
            if wallet.get("chain_type") == CHAINS[coin][0]:
                addr = wallet.get("address")
                if _valid_address(coin, addr):
                    seen[coin].add(addr)
    # Ambiguous native wallet holdings are unavailable, not combined.
    return {coin:next(iter(found)) if len(found)==1 else None
            for coin,found in seen.items()}


def _parse_quantity(value: object) -> int:
    if type(value) not in (str,int):
        raise ValueError("Expected an integer native balance")
    raw = str(value)
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)", raw):
        raise ValueError("Malformed native balance")
    amount = int(raw)
    if amount > _MAX_64:
        raise ValueError("Native balance too large")
    return amount


def _rpc_url(coin: str) -> str:
    url = os.getenv(CHAINS[coin][3], "").strip() or CHAINS[coin][2]
    if (not url.startswith("https://") or
        any(char in url for char in " \t\r\n#@")):
        raise ValueError("Invalid operator RPC URL")
    return url


async def _read_one(client: httpx.AsyncClient, coin: str, address: str) -> str:
    url = _rpc_url(coin)
    if coin == "SOL":
        payload = {
            "jsonrpc":"2.0","id":1,"method":"getBalance",
            "params":[address,{"commitment":"finalized"}],
        }
    elif coin == "XRP":
        payload = {
            "method":"account_info",
            "params":[{"account":address,"ledger_index":"validated"}],
        }
    else:
        payload = {"address":address,"visible":True}

    resp = await client.post(url,json=payload,headers={
        "Accept":"application/json","Content-Type":"application/json",
    })
    resp.raise_for_status()
    if len(resp.content) > 8192:
        raise ValueError("Oversized native balance response")
    body = resp.json()
    if not isinstance(body, dict):
        raise ValueError("Invalid native account response")
    if coin=="SOL":
        if (body.get("jsonrpc")!="2.0" or body.get("id")!=1 or
            "error" in body or not isinstance(body.get("result"),dict)):
            raise ValueError("Solana RPC error")
        value = body["result"].get("value")
    elif coin=="XRP":
        result = body.get("result")
        if (not isinstance(result,dict) or result.get("status")!="success"
                or result.get("validated") is not True
                or not isinstance(result.get("account_data"),dict)
                or result["account_data"].get("Account")!=address):
            raise ValueError("XRP validated ledger unavailable")
        value = result["account_data"].get("Balance")
    else:
        if ("Error" in body or body.get("address")!=address):
            raise ValueError("TRON confirmed account unavailable")
        # Activated TRON accounts may omit balance when exactly zero.
        value = body.get("balance",0)
    return _units(_parse_quantity(value),CHAINS[coin][4])


async def get_native_balances(privy_did: str) -> dict:
    did = _safe_did(privy_did)
    user = await fetch_privy_user(did)
    addresses = verified_native_addresses(user,did)
    entries = {
        coin:{
            "asset":coin,
            "network":CHAINS[coin][1],
            "status":"unavailable" if addresses[coin] else "wallet_missing",
            "balance":None,
            "native":True,
            "wallet_funds_used_for_paper":False,
        }
        for coin in ORDER
    }
    active = [(coin,address) for coin,address in addresses.items() if address]
    if active:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(9.0),follow_redirects=False,
        ) as client:
            results = await asyncio.gather(*(
                _read_one(client,coin,address) for coin,address in active
            ),return_exceptions=True)
        for (coin,_),result in zip(active,results):
            if isinstance(result,str):
                entries[coin]["status"]="verified"
                entries[coin]["balance"]=result
    return {
        "mode":"NATIVE_MAINNET_BALANCES_READ_ONLY",
        "wallet_ownership_verified":True,
        "asset_order":list(ORDER),
        "assets":entries,
        "wallet_funds_used_for_paper":False,
        "real_trading_enabled":False,
        "execution_available":False,
    }
