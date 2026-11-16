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
from .shared_evm_execution import bound_evm_wallet_rpc
from .shared_evm_auto_preflight import inspect_evm_exact_swap
from .shared_evm_privy_transport import bound_privy_evm_swap_request,post_claimed_privy_evm_rpc
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
    # The active real ETH Privy AUTO broadcaster uses the same exact swap
    # validator as every other EVM asset. No extra broadcaster is enabled.
    inspect_evm_exact_swap(
        tx,asset="ETH",wallet=wallet,side=side,stable=stable,
        input_atomic=input_atomic,
        minimum_output_atomic=minimum_output_atomic,
    )
    # Privy expects the chain in caip2, and the exact ERC20 transaction
    # in params.transaction. No caller-provided destination or arbitrary RPC.
    return bound_privy_evm_swap_request(
        asset="ETH",wallet_id=wallet_id,wallet=wallet,tx=tx,
        side=side,stable=stable,input_atomic=input_atomic,
        minimum_output_atomic=minimum_output_atomic,
    )


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
    # This ETH-only entry point preserves the existing committed order,
    # user consent, per-wallet policy and quorum checks in its caller.
    # All EVM networks now share the exact same one-shot Privy transport.
    return await post_claimed_privy_evm_rpc(
        url=url,body=body,app_secret=app_secret,
        authorization_signature=authorization_signature,
        idempotency_key=idempotency_key,client=client,
    )
