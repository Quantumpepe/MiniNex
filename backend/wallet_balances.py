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
ETH_RPC = "https://ethereum-rpc.publicnode.com"  # Ethereum mainnet only, read-only JSON-RPC

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


def _rpc_quantity(payload: object, *, fixed_word: bool = False) -> int:
    """Validate Ethereum RPC result, including ABI-padded ERC-20 uint256."""
    if not isinstance(payload,dict) or payload.get("jsonrpc")!="2.0" or "error" in payload:
        raise ValueError("Ethereum RPC error")
    value=payload.get("result")
    if not isinstance(value,str):
        raise ValueError("Invalid Ethereum RPC response")
    # eth_call returns a 32-byte ABI hex word, unlike quantity responses
    # from eth_getBalance/eth_chainId, which prohibit leading zeros.
    valid=(re.fullmatch(r"0x[0-9a-fA-F]{64}",value) if fixed_word else
           re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)",value))
    if not valid:
        raise ValueError("Invalid Ethereum RPC response")
    number=int(value,16)
    if number>MAX_UINT256:
        raise ValueError("Ethereum RPC uint256 overflow")
    return number


async def _eth_rpc_call(client: httpx.AsyncClient, method: str, params: list, req_id: int) -> int:
    try:
        response=await client.post(
            ETH_RPC, json={"jsonrpc":"2.0","method":method,"params":params,"id":req_id},
            headers={"Accept":"application/json","Content-Type":"application/json"},
        )
        response.raise_for_status()
        payload=response.json()
        if not isinstance(payload,dict) or payload.get("id")!=req_id:
            raise ValueError("Ethereum RPC response mismatch")
        return _rpc_quantity(payload,fixed_word=method=="eth_call")
    except (httpx.HTTPError,ValueError,TypeError,AttributeError):
        raise ValueError("Ethereum mainnet RPC unavailable") from None


async def _ethereum_fallback(client: httpx.AsyncClient, address: str,
                             need_eth: bool, need_tokens: bool) -> dict[str,str]:
    """Read chain-verified Ethereum balances even for never-used addresses.

    Blockscout may return coin_balance:null when an address has no activity.
    Only a successful mainnet JSON-RPC response may establish zero as fact.
    """
    if not ETH_RE.fullmatch(address):
        raise ValueError("Invalid EVM address")
    if await _eth_rpc_call(client,"eth_chainId",[],1)!=1:
        raise ValueError("RPC provider is not Ethereum mainnet")
    result={}
    if need_eth:
        wei=await _eth_rpc_call(client,"eth_getBalance",[address,"latest"],2)
        result["ETH"]=_units(wei,WEI)
    if need_tokens:
        selector="0x70a08231"  # ERC-20 balanceOf(address); a read-only eth_call
        calldata=selector+("0"*24)+address[2:].lower()
        for req_id,(token,contract) in enumerate(CONTRACTS.items(),start=3):
            amount=await _eth_rpc_call(client,"eth_call",[
                {"to":contract,"data":calldata},"latest",
            ],req_id)
            result[token]=_units(amount,STABLE_UNITS)
    return result


async def get_wallet_balances(did: str) -> dict:
    user=await fetch_privy_user(did)
    try:
        wallets=verified_addresses(user,did)
    except ValueError:
        raise HTTPException(503,"Verified wallet selection unavailable") from None
    btc,eth=wallets["BTC"],wallets["ETH"]
    balances={
        asset:{"balance":None,
               "status":"unavailable" if wallets["BTC" if asset=="BTC" else "ETH"] else "wallet_missing",
               "network":"bitcoin-mainnet" if asset=="BTC" else "ethereum-mainnet"}
        for asset in ("BTC","ETH","USDC","USDT")
    }
    # Each public explorer resource must fail independently. ETH holdings
    # must not disappear because Bitcoin or ERC-20 data is offline (or vice
    # versa). Failed requests never masquerade as verified zero balances.
    requests=[]
    if btc:
        requests.append(("BTC",f"{BTC_API}/address/{btc}"))
    if eth:
        requests.extend([
            ("ETH",f"{ETH_API}/addresses/{eth}"),
            ("TOKENS",f"{ETH_API}/addresses/{eth}/token-balances"),
        ])
    if requests:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(9.0),follow_redirects=False) as client:
                responses=await asyncio.gather(*[
                    _get_json(client,url) for _,url in requests
                ],return_exceptions=True)
                for (source,_),payload in zip(requests,responses):
                    if isinstance(payload,BaseException):
                        continue
                    try:
                        if source=="BTC":
                            balances["BTC"]["balance"]=parse_btc_balance(payload)
                            balances["BTC"]["status"]="verified"
                        elif source=="ETH":
                            balances["ETH"]["balance"]=parse_eth_balance(payload)
                            balances["ETH"]["status"]="verified"
                        elif source=="TOKENS":
                            tokens=parse_stable_balances(payload)
                            for token in ("USDC","USDT"):
                                balances[token]["balance"]=tokens[token]
                                balances[token]["status"]="verified"
                    except (ValueError,TypeError,OverflowError):
                        # A malformed explorer response only invalidates that
                        # source; zero must never be invented on exceptions.
                        continue
                if eth and (balances["ETH"]["status"]=="unavailable"
                            or balances["USDC"]["status"]=="unavailable"
                            or balances["USDT"]["status"]=="unavailable"):
                    # Unused Ethereum addresses often have coin_balance:null.
                    # Ask a known mainnet JSON-RPC for actual zero / token
                    # balances instead of marking the whole wallet missing.
                    try:
                        fallback=await _ethereum_fallback(
                            client,eth,
                            balances["ETH"]["status"]=="unavailable",
                            balances["USDC"]["status"]=="unavailable"
                            or balances["USDT"]["status"]=="unavailable",
                        )
                        for asset,quantity in fallback.items():
                            balances[asset]["balance"]=quantity
                            balances[asset]["status"]="verified"
                    except (ValueError,httpx.HTTPError):
                        pass  # Keep "unavailable" if the fallback also fails.
        except (httpx.HTTPError,ValueError,TypeError,OverflowError):
            pass  # Never label unknown explorer balances as verified zero.

    return {
        "mode":"VERIFIED_READ_ONLY",
        "wallet_ownership_verified":True,
        "real_trading_enabled":False,
        "automatic_signing_enabled":False,
        "wallet_funds_used_for_paper":False,
        "pricing":"NATIVE_UNITS_ONLY",
        "provider_status":"partial" if any(v["status"]=="unavailable" for v in balances.values()) else "complete",
        "as_of":datetime.now(timezone.utc).isoformat(),
        "wallets":wallets,
        "balances":balances,
        "warning":"Mainnet explorer estimates, not available to spend in MiniNex. No transaction or wallet approval is enabled.",
    }
