"""Original-owner SOL -> Ethereum stablecoin source-chain read-only preflight.

Obtain an owner-bound THORChain quote and independent Solana-mainnet RPC:
genesis hash, finalized owner balance, current blockhash, expiry height and
the *actual* getFeeForMessage result. Recompile the exact transfer + refund
memo after reading the network fee. Does not sign, delegate or send SOL.
"""
from __future__ import annotations

import base64
import re
import time

import httpx
from fastapi import HTTPException

from .native_coin_balances import _rpc_url, _valid_address
from .native_sol_compiled_message import compile_original_unsigned_sol_message
from .native_thor_swap_quote import get_private_native_thornode_terms
from .storage import _safe_did

_SOL_MAINNET_GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
_MAX_LAMPORTS = 2**64 - 1


async def _sol_rpc(client: httpx.AsyncClient, *, method: str,
                   params: list, request_id: int) -> object:
    response = await client.post(_rpc_url("SOL"), json={
        "jsonrpc": "2.0", "id": request_id,
        "method": method, "params": params,
    }, headers={"Accept": "application/json",
                "Content-Type": "application/json"})
    response.raise_for_status()
    if len(response.content) > 40_000:
        raise ValueError("Oversized Solana mainnet RPC response")
    body = response.json()
    if (type(body) is not dict or body.get("jsonrpc") != "2.0"
            or body.get("id") != request_id
            or "error" in body or "result" not in body):
        raise ValueError("Untrusted Solana RPC response")
    return body["result"]


def _read_finalized_source(owner: str, balance: object,
                           latest: object, height: object) -> tuple[dict,dict]:
    if (not _valid_address("SOL", owner)
            or type(balance) is not dict or type(latest) is not dict
            or type(height) is not int or height < 1):
        raise ValueError("Original Solana source proof unavailable")
    balance_context = balance.get("context")
    source_balance = balance.get("value")
    blockhash_context = latest.get("context")
    blockhash_value = latest.get("value")
    if (type(balance_context) is not dict
            or type(balance_context.get("slot")) is not int
            or balance_context["slot"] < 1
            or type(source_balance) is not int
            or not 0 <= source_balance <= _MAX_LAMPORTS
            or type(blockhash_context) is not dict
            or type(blockhash_context.get("slot")) is not int
            or blockhash_context["slot"] < balance_context["slot"]
            or type(blockhash_value) is not dict
            or type(blockhash_value.get("blockhash")) is not str
            or type(blockhash_value.get("lastValidBlockHeight")) is not int
            or blockhash_value["lastValidBlockHeight"] <= height):
        raise ValueError("Original Solana blockhash or source balance stale")
    source = {
        "owner": owner, "network": "solana-mainnet",
        "commitment": "finalized", "slot": balance_context["slot"],
        "balance_lamports": str(source_balance),
    }
    network = {
        "recent_blockhash": blockhash_value["blockhash"],
        "context_slot": blockhash_context["slot"],
        "current_block_height": height,
        "last_valid_block_height": blockhash_value["lastValidBlockHeight"],
        "fee_lamports": "0",
    }
    return source, network


async def prepare_original_sol_unsigned_owner_transfer(
    did: str, *, stable: str, amount: str,
) -> dict:
    """Current genuine original RPC state with an UNSIGNED Solana message."""
    did = _safe_did(did)
    if stable not in ("USDC", "USDT"):
        raise HTTPException(422, "Unsupported SOL owner settlement asset")
    quote = await get_private_native_thornode_terms(
        did, asset="SOL", side="SELL", stable=stable, amount=amount,
    )
    owner = quote.get("source_wallet")
    if not _valid_address("SOL", owner):
        raise HTTPException(409, "Original Privy SOL source owner unavailable")
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(9), follow_redirects=False,
        ) as client:
            genesis = await _sol_rpc(
                client, method="getGenesisHash", params=[], request_id=1,
            )
            if genesis != _SOL_MAINNET_GENESIS:
                raise ValueError("Native SOL provider does not serve mainnet")
            balance = await _sol_rpc(
                client, method="getBalance",
                params=[owner, {"commitment": "finalized"}], request_id=2,
            )
            latest = await _sol_rpc(
                client, method="getLatestBlockhash",
                params=[{"commitment": "finalized"}], request_id=3,
            )
            height = await _sol_rpc(
                client, method="getBlockHeight",
                params=[{"commitment": "finalized"}], request_id=4,
            )
            source, network = _read_finalized_source(
                owner, balance, latest, height,
            )
            before_fee = compile_original_unsigned_sol_message(
                quote, amount=amount, now=int(time.time()),
                source_account=source, network_terms=network,
            )
            quoted_fee = await _sol_rpc(
                client, method="getFeeForMessage",
                params=[before_fee["message_base64"],
                        {"commitment": "finalized"}], request_id=5,
            )
            if (type(quoted_fee) is not dict
                    or type(quoted_fee.get("value")) is not int
                    or not 0 < quoted_fee["value"] <= _MAX_LAMPORTS):
                raise ValueError("Original signed SOL message fee unavailable")
            network["fee_lamports"] = str(quoted_fee["value"])
            compiled = compile_original_unsigned_sol_message(
                quote, amount=amount, now=int(time.time()),
                source_account=source, network_terms=network,
            )
    except (httpx.HTTPError, ValueError, KeyError, TypeError,
            OverflowError):
        raise HTTPException(
            409, "Original SOL owner quote, balance, blockhash or fee unavailable",
        ) from None
    if (compiled["source_wallet"] != owner
            or compiled["message_sha256"] != before_fee["message_sha256"]):
        raise HTTPException(409, "Original SOL payment changed during fee lookup")
    return {
        "mode": "ORIGINAL_SOL_SOURCE_UNSIGNED_MAINNET_PREFLIGHT",
        "asset": "SOL", "side": "SELL",
        "stable": stable,
        "source_wallet": owner,
        "inbound_vault": compiled["inbound_vault"],
        "destination_wallet": compiled["destination_wallet"],
        "source_amount_lamports": compiled["source_amount_lamports"],
        "source_balance_lamports": source["balance_lamports"],
        "network_fee_lamports": network["fee_lamports"],
        "finalized_source_slot": source["slot"],
        "current_block_height": network["current_block_height"],
        "last_valid_block_height": compiled["last_valid_block_height"],
        "solana_mainnet_genesis_verified": True,
        "current_owner_blockhash_and_balance_verified": True,
        "real_message_fee_verified": True,
        "original_compiled_message": compiled,
        "original_privy_signer_policy_verified": False,
        "original_order_send_claim_verified": False,
        "wallet_signed": False,
        "source_transaction_sent": False,
        "execution_enabled": False, "can_submit": False,
    }
