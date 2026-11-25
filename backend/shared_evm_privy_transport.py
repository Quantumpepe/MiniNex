"""Single EVM Privy RPC transport for MiniNex's six supported assets.

Real ETH AUTO uses this implementation. Other assets can use the identical
transport only after a separately verified per-asset grant, persistent
SENDING journal, unique order claim and registered server quorum.
There is NO public endpoint or shortcut around those prerequisites.
"""
from __future__ import annotations

import logging
import re

import httpx
from fastapi import HTTPException

from .privy_wallet_verification import APP_ID
from .shared_evm_auto_preflight import inspect_evm_exact_swap
from .shared_evm_execution import bound_evm_wallet_rpc

_HASH=re.compile(r"^0x[0-9a-fA-F]{64}$")
_SIGNATURE=re.compile(r"^[A-Za-z0-9+/=_:-]{40,8192}$")
_KEY=re.compile(r"^[A-Za-z0-9_-]{16,96}$")
LOG=logging.getLogger("mininex.shared_evm_privy_transport")


def bound_privy_evm_swap_request(
    *,asset:str,wallet_id:str,wallet:str,tx:dict,
    side:str,stable:str,input_atomic:str,minimum_output_atomic:str,
)->tuple[str,dict]:
    """Derive exact owner-scoped calldata and CAIP2 from server registry."""
    result=inspect_evm_exact_swap(
        tx,asset=asset,wallet=wallet,side=side,stable=stable,
        input_atomic=input_atomic,
        minimum_output_atomic=minimum_output_atomic,
    )
    return bound_evm_wallet_rpc(
        asset=asset,wallet_id=wallet_id,wallet=wallet,
        **({"wrapped_spot":True} if asset in ("BTC","XRP") else {}),
        tx={
            "from":wallet,"chainId":result["chain_id"],
            "to":result["router"],"data":tx["data"],"value":"0",
        },
    )


async def post_claimed_privy_evm_rpc(
    *,url:str,body:dict,app_secret:str,authorization_signature:str,
    idempotency_key:str,client:httpx.AsyncClient,
)->str:
    """POST once after the caller's durable SENDING claim, never retry.

    Reuses identical on-wire Privy request shape for every EVM network.
    A signature and idempotency key are supplied only by server internal
    execution after user consent and a committed order claim.
    """
    if (type(url) is not str or
            not re.fullmatch(r"https://api\.privy\.io/v1/wallets/[A-Za-z0-9_-]{6,160}/rpc",url)
            or type(body) is not dict or body.get("method")!="eth_sendTransaction"
            or type(body.get("caip2")) is not str
            or body["caip2"] not in ("eip155:1","eip155:56","eip155:137","eip155:43114","eip155:42161")
            or type(body.get("params")) is not dict
            or type(body["params"].get("transaction")) is not dict
            or type(app_secret) is not str or len(app_secret)<12
            or type(authorization_signature) is not str
            or not _SIGNATURE.fullmatch(authorization_signature)
            or type(idempotency_key) is not str or not _KEY.fullmatch(idempotency_key)):
        raise ValueError("Missing verified Privy EVM signing prerequisites")
    try:
        response=await client.post(
            url,auth=(APP_ID,app_secret),
            headers={"privy-app-id":APP_ID,
                     "privy-authorization-signature":authorization_signature,
                     "privy-idempotency-key":idempotency_key,
                     "Content-Type":"application/json"},
            json=body,
        )
        response.raise_for_status()
        if len(response.content)>65536:
            raise ValueError("Unexpected Privy EVM RPC response length")
        payload=response.json()
        if type(payload) is not dict:
            raise ValueError("Unexpected Privy EVM RPC response shape")
        inner=payload.get("data")
        tx_hash=inner.get("hash") if type(inner) is dict else payload.get("hash")
        if type(tx_hash) is not str or not _HASH.fullmatch(tx_hash):
            raise ValueError("Privy EVM RPC returned no verified hash")
        return tx_hash.lower()
    except (httpx.HTTPError,ValueError,TypeError):
        # An HTTP timeout is ambiguous: the transaction may already exist.
        # The durable caller MUST reconcile and never submit a second time.
        LOG.warning("Privy EVM send outcome uncertain; reconcile original journal")
        raise HTTPException(503,"Privy EVM submission uncertain; reconcile original transaction") from None
