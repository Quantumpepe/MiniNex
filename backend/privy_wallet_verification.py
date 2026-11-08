"""Server-side address ownership check against Privy's authenticated user record.

Never use client-supplied wallet lists as authority to store tax evidence.
Requires MININEX_PRIVY_APP_SECRET on Render; absent credentials fail closed.
"""
from __future__ import annotations
import os
import httpx
from fastapi import HTTPException
from .blockchain_history import validate_request
from .storage import _safe_did

APP_ID = "cmw7sb7ty013o0gl55aqyzi3y"


def address_in_privy_user(data: dict, *, did: str, asset: str, address: str) -> bool:
    if not isinstance(data, dict) or data.get("id") != did:
        return False
    accounts=data.get("linked_accounts")
    if not isinstance(accounts,list):
        return False
    for account in accounts:
        if not isinstance(account,dict) or account.get("type")!="wallet":
            continue
        chain=str(account.get("chain_type",""))
        actual=str(account.get("address",""))
        if asset=="BTC":
            if chain=="bitcoin-segwit" and actual==address:
                return True
        else:
            if chain=="ethereum" and actual.lower()==address.lower() and (
                account.get("wallet_client_type")=="privy" or
                account.get("wallet_client")=="privy"
            ):
                return True
    return False


async def verify_wallet_address(did: str, asset: str, address: str) -> None:
    _safe_did(did)
    validate_request(asset,address)
    secret=os.environ.get("MININEX_PRIVY_APP_SECRET","").strip()
    if not secret:
        raise HTTPException(503,"Private Privy wallet verification not configured")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(9),follow_redirects=False) as client:
            response=await client.get(
                "https://api.privy.io/v1/users/"+did,
                auth=(APP_ID,secret),
                headers={"privy-app-id":APP_ID,"Accept":"application/json"},
            )
            response.raise_for_status()
            data=response.json()
    except (httpx.HTTPError,ValueError):
        raise HTTPException(503,"Could not independently verify Privy wallet") from None
    if not address_in_privy_user(data,did=did,asset=asset,address=address):
        raise HTTPException(403,"Wallet is not verified for this MiniNex account")
