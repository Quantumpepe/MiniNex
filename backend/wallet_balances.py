"""Owner-verified, read-only native BTC / Ethereum mainnet balances.

This is NEVER a trading, wallet signing, delegation or authorization path.
Only independently fetched Privy linked wallets are used; a client cannot
supply the address or choose token contracts/chain IDs. All values are units,
not USD estimates, and public explorer availability is not guaranteed.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import re

import httpx
from fastapi import HTTPException

from .blockchain_history import CONTRACTS
from .blockchain_preview import BTC_API, ETH_API, BTC_RE, ETH_RE
from .privy_wallet_verification import fetch_privy_user

INTEGER = re.compile(r"^(?:0|[1-9][0-9]*)$")
MAX_UINT256 = 2**256 - 1
SATOSHIS = Decimal(100_000_000)
WEI = Decimal(10**18)
STABLE_UNITS = Decimal(10**6)

def _units(value: object, divisor: Decimal) -> str:
    if isinstance(value,bool) or not isinstance(value,(str,int)):
        raise ValueError("Balance is not an integer")
    raw=str(value)
    if not INTEGER.fullmatch(raw) or len(raw)>78:
        raise ValueError("Invalid numeric balance")
    number=int(raw)
    if number>MAX_UINT256:
        raise ValueError("Balance outside uint256 range")
    return format(Decimal(number)/divisor,"f")


def verified_addresses(user: dict, did: str) -> dict[str,str|None]:
    """Reject ambiguous wallets rather than attaching somebody else's address."""
    if not isinstance(user,dict) or user.get("id")!=did:
        raise ValueError("Privy identity mismatch")
    linked=user.get("linked_accounts")
    if not isinstance(linked,list):
        raise ValueError("Invalid Privy account")
    found={"BTC":set(),"ETH":set()}
    for item in linked:
        if not isinstance(item,dict) or item.get("type")!="wallet":
            continue
        address=item.get("address")
        if not isinstance(address,str):
            continue
        chain=item.get("chain_type")
        if chain=="bitcoin-segwit" and BTC_RE.fullmatch(address):
            found["BTC"].add(address)
        elif (chain=="ethereum" and ETH_RE.fullmatch(address)
              and (item.get("wallet_client_type")=="privy"
                   or item.get("wallet_client")=="privy")):
            found["ETH"].add(address.lower())
    if any(len(items)>1 for items in found.values()):
        raise ValueError("Ambiguous Privy wallet: choose a wallet before reading funds")
    return {asset:next(iter(items),None) for asset,items in found.items()}


def parse_btc_balance(payload: dict) -> str:
    if not isinstance(payload,dict) or not isinstance(payload.get("chain_stats"),dict):
        raise ValueError("Malformed Bitcoin mainnet balance")
    stats=payload["chain_stats"]
    received=stats.get("funded_txo_sum")
    spent=stats.get("spent_txo_sum")
    # Esplora amounts are satoshis. Pending amounts intentionally excluded.
    a=_units(received,Decimal(1))
    b=_units(spent,Decimal(1))
    unspent=int(a)-int(b)
    if unspent<0 or unspent>2_100_000_000_000_000:
        raise ValueError("Invalid confirmed Bitcoin balance")
    return format(Decimal(unspent)/SATOSHIS,"f")


def parse_eth_balance(payload: dict) -> str:
    if not isinstance(payload,dict):
        raise ValueError("Malformed Ethereum mainnet balance")
    # Blockscout deployments use either coin_balance or balance.
    raw=payload.get("coin_balance",payload.get("balance"))
    return _units(raw,WEI)


def parse_stable_balances(payload: object) -> dict[str,str]:
    if not isinstance(payload,list) or len(payload)>400:
        raise ValueError("Malformed Ethereum token balances")
    values={asset:"0" for asset in CONTRACTS}
    seen=set()
    for entry in payload:
        if not isinstance(entry,dict):
            raise ValueError("Malformed token row")
        token=entry.get("token")
        if not isinstance(token,dict):
            raise ValueError("Malformed token metadata")
        contract=token.get("address_hash") or token.get("address")
        if not isinstance(contract,str):
            continue
        matched=next((asset for asset,allowed in CONTRACTS.items()
                      if contract.lower()==allowed),None)
        if matched is None:
            continue
        if matched in seen or str(token.get("type","")).upper()!="ERC-20":
            raise ValueError("Invalid or duplicated allowed ERC-20")
        if str(token.get("decimals"))!="6":
            raise ValueError("Unexpected USDC/USDT decimals")
        values[matched]=_units(entry.get("value"),STABLE_UNITS)
        seen.add(matched)
    return values


async def _get_json(client: httpx.AsyncClient, url: str) -> object:
    try:
        response=await client.get(url,headers={"Accept":"application/json"})
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError,ValueError):
        raise ValueError("Mainnet explorer unavailable") from None


async def get_wallet_balances(did: str) -> dict:
    user=await fetch_privy_user(did)
    try:
        wallets=verified_addresses(user,did)
    except ValueError:
        raise HTTPException(503,"Verified wallet selection unavailable") from None
    btc,eth=wallets["BTC"],wallets["ETH"]
    balances={
        asset:{"balance":None,"status":"wallet_missing",
               "network":"bitcoin-mainnet" if asset=="BTC" else "ethereum-mainnet"}
        for asset in ("BTC","ETH","USDC","USDT")
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(9.0),follow_redirects=False) as client:
            requests=[]
            if btc:
                requests.append(_get_json(client,f"{BTC_API}/address/{btc}"))
            if eth:
                requests.extend([
                    _get_json(client,f"{ETH_API}/addresses/{eth}"),
                    _get_json(client,f"{ETH_API}/addresses/{eth}/token-balances"),
                ])
            responses=await asyncio.gather(*requests)
        index=0
        if btc:
            balances["BTC"]={"balance":parse_btc_balance(responses[index]),
                             "status":"verified","network":"bitcoin-mainnet"}
            index+=1
        if eth:
            balances["ETH"]={"balance":parse_eth_balance(responses[index]),
                             "status":"verified","network":"ethereum-mainnet"}
            index+=1
            tokens=parse_stable_balances(responses[index])
            for token in ("USDC","USDT"):
                balances[token]={"balance":tokens[token],"status":"verified",
                                 "network":"ethereum-mainnet"}
    except (ValueError,TypeError,OverflowError):
        raise HTTPException(503,"Read-only blockchain balances temporarily unavailable") from None

    return {
        "mode":"VERIFIED_READ_ONLY",
        "wallet_ownership_verified":True,
        "real_trading_enabled":False,
        "automatic_signing_enabled":False,
        "wallet_funds_used_for_paper":False,
        "pricing":"NATIVE_UNITS_ONLY",
        "as_of":datetime.now(timezone.utc).isoformat(),
        "wallets":wallets,
        "balances":balances,
        "warning":"Mainnet explorer estimates, not available to spend in MiniNex. No transaction or wallet approval is enabled.",
    }
