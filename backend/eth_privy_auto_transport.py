"""Real Privy Wallet RPC submission transport for the internal ETH AUTO executor.

This module performs a real HTTP POST capable of signing/broadcasting when
called by a separately authorized, durable worker. It is NOT exposed as an
API endpoint and does not manufacture a user's additional-signer grant.
"""
from __future__ import annotations

import re
import logging
import httpx

from fastapi import HTTPException
from .privy_wallet_verification import APP_ID
from .eth_swaprouter02_preflight import (
    inspect_swaprouter02_exact_input, SWAPROUTER02_MAINNET,
)

LOG=logging.getLogger("mininex.privy_eth_auto_rpc")
_WALLET_ID=re.compile(r"^[A-Za-z0-9_-]{6,160}$")
_ADDR=re.compile(r"^0x[0-9a-fA-F]{40}$")
_HASH=re.compile(r"^0x[0-9a-fA-F]{64}$")
_SIGNATURE=re.compile(r"^[A-Za-z0-9+/=_:-]{40,8192}$")
_PATH="https://api.privy.io/v1/wallets/"


def bound_privy_rpc_request(*, wallet_id: str, wallet: str, tx: dict,
                            side: str, stable: str, input_atomic: str,
                            minimum_output_atomic: str) -> tuple[str,dict]:
    """Build actual Privy eth_sendTransaction request for one Router02 swap.

    The wallet metadata, quote, order and consent MUST originate from
    authenticated server records, never caller JSON. This private transport
    cannot substitute for that prerequisite.
    """
    if (type(wallet_id) is not str or not _WALLET_ID.fullmatch(wallet_id)
            or type(wallet) is not str or not _ADDR.fullmatch(wallet)):
        raise ValueError("Privy owner wallet identity required")
    if type(tx) is not dict:
        raise ValueError("Immutable Ethereum transaction required")
    inspect_swaprouter02_exact_input(
        tx,wallet=wallet,side=side,stable=stable,
        input_atomic=input_atomic,
        minimum_output_atomic=minimum_output_atomic,
    )
    # Privy expects the chain in caip2, and the exact ERC20 transaction
    # in params.transaction. No caller-provided destination or arbitrary RPC.
    url=_PATH+wallet_id+"/rpc"
    payload={
        "method":"eth_sendTransaction",
        "caip2":"eip155:1",
        "params":{"transaction":{
            "to":SWAPROUTER02_MAINNET,
            "data":tx["data"],
            "value":"0",
        }},
    }
    return url,payload


async def broadcast_privy_eth_swap(
    *, wallet_id: str, wallet: str, tx: dict, side: str, stable: str,
    input_atomic: str, minimum_output_atomic: str,
    app_secret: str, authorization_signature: str,
    idempotency_key: str, client: httpx.AsyncClient,
) -> str:
    """Perform one real authorized Privy send. Never auto-retry uncertainty.

    A worker must FIRST durably record SENDING, its transaction digest,
    Privy quorum authorization, wallet-owner grant and unique order nonce.
    Authorization signature is supplied by the approved P-256 signer service.
    """
    url,body=bound_privy_rpc_request(
        wallet_id=wallet_id,wallet=wallet,tx=tx,side=side,stable=stable,
        input_atomic=input_atomic,
        minimum_output_atomic=minimum_output_atomic)
    if (type(app_secret) is not str or len(app_secret)<12
            or type(authorization_signature) is not str
            or not _SIGNATURE.fullmatch(authorization_signature)
            or type(idempotency_key) is not str
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,96}",idempotency_key)):
        raise ValueError("Privy signing authority or intent key unavailable")
    try:
        response=await client.post(
            url,auth=(APP_ID,app_secret),
            headers={"privy-app-id":APP_ID,
                     "privy-authorization-signature":authorization_signature,
                     "privy-idempotency-key":idempotency_key,
                     "Content-Type":"application/json"},
            json=body)
        response.raise_for_status()
        if len(response.content)>65536:
            raise ValueError("Unexpected large Privy RPC result")
        data=response.json()
        if type(data) is not dict:
            raise ValueError("Unexpected Privy RPC result")
        nested=data.get("data")
        tx_hash=(nested.get("hash") if type(nested) is dict
                 else data.get("hash"))
        if type(tx_hash) is not str or not _HASH.fullmatch(tx_hash):
            raise ValueError("Privy RPC did not return a transaction hash")
        return tx_hash.lower()
    except (httpx.HTTPError,ValueError,TypeError):
        # Even on timeout the wallet may have signed/broadcast. A caller
        # MUST keep its durable SENDING/UNCERTAIN marker and reconcile.
        LOG.warning("Privy ETH AUTO broadcast outcome unknown; do not resend")
        raise HTTPException(
            503,"Privy ETH submission uncertain; reconcile original transaction"
        ) from None
