"""Actual wallet-owner authorized Privy WETH9 wrap and unwrap submissions.

Used by ETH LIVE AUTO conversion stages, never callable by frontend JSON.
Only Ethereum mainnet WETH9 deposit/withdraw, with exact amount.
"""
from __future__ import annotations

import re
import logging
from fastapi import HTTPException
import httpx

from .privy_wallet_verification import APP_ID
from .eth_privy_p256_signer import sign_privy_rpc_payload
from .eth_swaprouter02_preflight import WETH_MAINNET

_ID=re.compile(r"^[A-Za-z0-9_-]{6,160}$")
_ADDR=re.compile(r"^0x[0-9a-fA-F]{40}$")
_HASH=re.compile(r"^0x[0-9a-fA-F]{64}$")
_KEY=re.compile(r"^[A-Za-z0-9_-]{16,96}$")
LOG=logging.getLogger("mininex.privy_auto_weth9")


def bound_weth9_rpc(*,wallet_id:str,wallet:str,operation:str,
                    amount_wei:int)->tuple[str,dict]:
    if (type(wallet_id) is not str or not _ID.fullmatch(wallet_id)
            or type(wallet) is not str or not _ADDR.fullmatch(wallet)
            or operation not in ("DEPOSIT","WITHDRAW")
            or type(amount_wei) is not int
            or not 0<amount_wei<10**45):
        raise ValueError("Invalid ETH owner WETH9 conversion")
    if operation=="DEPOSIT":
        calldata="0xd0e30db0"
        value=str(amount_wei)
    else:
        calldata="0x2e1a7d4d"+f"{amount_wei:064x}"
        value="0"
    return "https://api.privy.io/v1/wallets/"+wallet_id+"/rpc",{
        "method":"eth_sendTransaction",
        "caip2":"eip155:1",
        "params":{"transaction":{
            "to":WETH_MAINNET,"data":calldata,"value":value,
        }},
    }


async def send_privy_native_weth9(*,wallet_id:str,wallet:str,
                                  operation:str,amount_wei:int,
                                  intent_key:str,app_secret:str,
                                  client:httpx.AsyncClient)->str:
    """One real broadcast; caller must commit SENDING before calling."""
    url,body=bound_weth9_rpc(
        wallet_id=wallet_id,wallet=wallet,
        operation=operation,amount_wei=amount_wei)
    if (type(intent_key) is not str or not _KEY.fullmatch(intent_key)
            or type(app_secret) is not str or len(app_secret)<12):
        raise ValueError("Missing Privy signer credentials")
    signature=sign_privy_rpc_payload(
        url=url,body=body,idempotency_key=intent_key)
    try:
        response=await client.post(
            url,auth=(APP_ID,app_secret),json=body,
            headers={"privy-app-id":APP_ID,
                     "privy-authorization-signature":signature,
                     "privy-idempotency-key":intent_key,
                     "Content-Type":"application/json"})
        response.raise_for_status()
        if len(response.content)>65536:
            raise ValueError("Unexpected Privy WETH response")
        content=response.json()
        if type(content) is not dict:
            raise ValueError("Unexpected Privy WETH response")
        nested=content.get("data")
        result=nested.get("hash") if type(nested) is dict else content.get("hash")
        if type(result) is not str or not _HASH.fullmatch(result):
            raise ValueError("Privy returned no WETH transaction hash")
        return result.lower()
    except (httpx.HTTPError,ValueError,TypeError):
        LOG.warning("Privy WETH9 transaction send ambiguous; never retry original")
        raise HTTPException(503,"ETH conversion send outcome unknown") from None
