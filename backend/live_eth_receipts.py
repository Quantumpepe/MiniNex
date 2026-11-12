"""Independent Ethereum-mainnet transaction/receipt verification for user-owned swaps.

This is a REAL read-only chain check, not a simulation. It runs only after
an authenticated user explicitly requests a transaction hash. It never
signs, broadcasts, approves, moves money, retries or marks an order filled.
Finalized transaction success DOES NOT prove the correct swap output amount;
that requires separately decoding and reconciling actual transfer logs.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
import re

import httpx
from fastapi import HTTPException

from .live_eth_swap_plan import _ROUTER, _ROUTER_SELECTOR
from .privy_wallet_verification import fetch_privy_user
from .wallet_balances import ETH_RPC, verified_addresses

_HASH = re.compile(r"^0x[a-fA-F0-9]{64}$")
_ADDRESS = re.compile(r"^0x[a-fA-F0-9]{40}$")
_QUANTITY = re.compile(r"^0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)$")
_INPUT = re.compile(r"^0x[a-fA-F0-9]{8,131072}$")
_RPC_CALLS = (
    ("eth_chainId", []),
    ("eth_getTransactionByHash", None),
    ("eth_getTransactionReceipt", None),
    ("eth_blockNumber", []),
)


def _num(value: object) -> int:
    if (type(value) is not str or len(value) > 66 or
            not _QUANTITY.fullmatch(value)):
        raise ValueError("Invalid Ethereum RPC quantity")
    number = int(value, 16)
    if number > 2**256 - 1:
        raise ValueError("Ethereum RPC quantity overflow")
    return number


def inspect_eth_swap_receipt(tx_hash: str, *, wallet: str,
                             chain_id: object, transaction: object,
                             receipt: object, latest: object,
                             finalized: object) -> dict:
    """Validate chain, user ownership, router and canonical receipt fields.

    Included is NOT final; only an Ethereum finalized checkpoint at or after
    the included block produces FINALIZED_*.
    """
    if not isinstance(tx_hash, str) or not _HASH.fullmatch(tx_hash):
        raise ValueError("Invalid Ethereum hash")
    if not isinstance(wallet, str) or not _ADDRESS.fullmatch(wallet):
        raise ValueError("Invalid verified Ethereum wallet")
    if _num(chain_id) != 1:
        raise ValueError("RPC is not Ethereum mainnet")
    head = _num(latest)
    summary = {
        "mode": "ETH_MAINNET_RECEIPT_CHECK",
        "network": "ethereum-mainnet",
        "tx_hash": tx_hash.lower(),
        "transaction_seen": False,
        "transaction_finalized": False,
        "transaction_success": None,
        "swap_outputs_verified": False,
        "live_order_recorded": False,
        "execution_enabled": False,
        "can_submit": False,
        "state": "NOT_FOUND",
        "confirmations": 0,
        "network_fee_eth": None,
        "warning": "Chain receipt only; token outputs and actual swap fill not yet verified.",
    }
    if transaction is None:
        if receipt is not None:
            raise ValueError("Receipt exists without a verified transaction")
        return summary
    if not isinstance(transaction, dict):
        raise ValueError("Unexpected Ethereum transaction")
    if (str(transaction.get("hash", "")).lower() != tx_hash.lower()
            or not isinstance(transaction.get("from"), str)
            or transaction["from"].lower() != wallet.lower()
            or not isinstance(transaction.get("to"), str)
            or transaction["to"].lower() != _ROUTER.lower()
            or _num(transaction.get("chainId")) != 1):
        raise ValueError("Transaction not bound to the owned mainnet router wallet")
    calldata = transaction.get("input")
    if (not isinstance(calldata, str) or len(calldata) % 2 != 0
            or len(calldata) < 10 + 64*3
            or not _INPUT.fullmatch(calldata)
            or not calldata.lower().startswith(_ROUTER_SELECTOR)):
        raise ValueError("Unrecognized Uniswap swap transaction input")
    summary["transaction_seen"] = True
    summary["state"] = "PENDING"
    if receipt is None:
        if transaction.get("blockHash") not in (None, "0x"):
            raise ValueError("Included transaction has no receipt yet")
        if transaction.get("blockNumber") is not None:
            raise ValueError("Included transaction missing receipt")
        return summary
    if not isinstance(receipt, dict):
        raise ValueError("Malformed Ethereum receipt")
    if (str(receipt.get("transactionHash", "")).lower() != tx_hash.lower()
            or not isinstance(receipt.get("from"), str)
            or receipt["from"].lower() != wallet.lower()
            or not isinstance(receipt.get("to"), str)
            or receipt["to"].lower() != _ROUTER.lower()):
        raise ValueError("Receipt transaction, owner or router mismatch")
    block_hash = receipt.get("blockHash")
    if not isinstance(block_hash, str) or not _HASH.fullmatch(block_hash):
        raise ValueError("Invalid Ethereum receipt block hash")
    block = _num(receipt.get("blockNumber"))
    if block > head or block <= 0:
        raise ValueError("Receipt block is outside canonical head")
    if (str(transaction.get("blockHash", "")).lower() != block_hash.lower()
            or _num(transaction.get("blockNumber")) != block):
        raise ValueError("Transaction and receipt are in different blocks")
    status = _num(receipt.get("status"))
    if status not in (0, 1):
        raise ValueError("Invalid transaction status")
    gas = _num(receipt.get("gasUsed"))
    gas_price = _num(receipt.get("effectiveGasPrice"))
    if gas <= 0 or gas > 30_000_000 or gas_price <= 0 or gas_price > 1_000_000_000_000:
        raise ValueError("Implausible verified mainnet gas")
    wei = gas * gas_price
    summary.update(
        state="INCLUDED_SUCCESS" if status == 1 else "INCLUDED_REVERTED",
        confirmations=min(head - block + 1, 1_000_000),
        transaction_success=status == 1,
        network_fee_eth=format(Decimal(wei) / Decimal(10**18), "f"),
    )
    if finalized is None:
        return summary
    if not isinstance(finalized, dict):
        raise ValueError("Invalid finalized Ethereum checkpoint")
    final_number = _num(finalized.get("number"))
    if final_number > head:
        raise ValueError("Finalized checkpoint exceeds canonical head")
    if final_number >= block:
        summary["state"] = "FINALIZED_SUCCESS" if status == 1 else "FINALIZED_REVERTED"
        summary["transaction_finalized"] = True
    return summary


async def _rpc(client: httpx.AsyncClient, method: str,
               params: list, req_id: int) -> object:
    try:
        resp = await client.post(
            ETH_RPC,
            json={"jsonrpc": "2.0", "id": req_id, "method": method, "params": params},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        resp.raise_for_status()
        if len(resp.content) > 96_000:
            raise ValueError("Oversized Ethereum RPC response")
        result = resp.json()
        if (not isinstance(result, dict) or result.get("id") != req_id
                or result.get("jsonrpc") != "2.0" or "error" in result
                or "result" not in result):
            raise ValueError("Invalid Ethereum RPC response")
        return result["result"]
    except (httpx.HTTPError, ValueError, TypeError):
        raise ValueError("Ethereum mainnet RPC temporarily unavailable") from None


async def check_eth_swap_receipt(did: str, tx_hash: str) -> dict:
    """At most five on-demand public RPC reads; no background service."""
    if not isinstance(tx_hash, str) or not _HASH.fullmatch(tx_hash):
        raise HTTPException(422, "Invalid Ethereum transaction hash")
    user = await fetch_privy_user(did)
    try:
        wallet = verified_addresses(user, did)["ETH"]
        if not isinstance(wallet, str) or not _ADDRESS.fullmatch(wallet):
            raise ValueError("Missing uniquely verified wallet")
    except (KeyError, ValueError):
        raise HTTPException(503, "Unique verified ETH wallet required") from None

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8.0), follow_redirects=False,
        ) as client:
            calls = [
                _rpc(client, "eth_chainId", [], 1),
                _rpc(client, "eth_getTransactionByHash", [tx_hash], 2),
                _rpc(client, "eth_getTransactionReceipt", [tx_hash], 3),
                _rpc(client, "eth_blockNumber", [], 4),
            ]
            chain, tx, receipt, head = await asyncio.gather(*calls)
            final = (
                await _rpc(client, "eth_getBlockByNumber", ["finalized", False], 5)
                if receipt is not None else None
            )
        return inspect_eth_swap_receipt(
            tx_hash, wallet=wallet, chain_id=chain, transaction=tx,
            receipt=receipt, latest=head, finalized=final,
        )
    except (httpx.HTTPError, ValueError, TypeError, OverflowError):
        raise HTTPException(502, "Independently verified ETH receipt unavailable") from None
