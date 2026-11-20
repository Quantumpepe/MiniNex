"""Read-only XRPL-mainnet original-owner source preflight for XRP SELL.

The real XRPL server must report network_id=0, a fresh validated ledger,
current reserve requirements, exact network fee and the original owner's
validated account sequence/balance from THAT ledger. Nothing is signed,
submitted, delegated or marked as a swap fill.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re

import httpx
from fastapi import HTTPException

from .native_coin_balances import _rpc_url, _valid_address
from .native_thor_swap_quote import get_private_native_thornode_terms
from .native_xrp_thor_payment_tx import build_original_unsigned_xrp_thor_payment
from .storage import _safe_did
import time

_INTEGER = re.compile(r"^(?:0|[1-9][0-9]*)$")
_XRP_MULTIPLIER = Decimal(1_000_000)


def _xrp_drops(amount: object) -> str:
    if type(amount) not in (int, str, float):
        raise ValueError("XRPL reserve amount malformed")
    if len(str(amount)) > 48:
        raise ValueError("XRPL reserve quantity too large")
    try:
        drops = Decimal(str(amount)) * _XRP_MULTIPLIER
    except (InvalidOperation, ValueError):
        raise ValueError("XRPL reserve quantity unavailable") from None
    if (not drops.is_finite() or drops != drops.to_integral_value()
            or not 0 <= drops < 10**25):
        raise ValueError("XRPL fractional reserve drops unavailable")
    return str(int(drops))


def inspect_xrpl_mainnet_server(
    server_info: object, fee_result: object,
) -> dict:
    if (type(server_info) is not dict
            or server_info.get("status") != "success"
            or type(server_info.get("info")) is not dict):
        raise ValueError("Unverified XRP Ledger server state")
    info = server_info["info"]
    validated = info.get("validated_ledger")
    if (info.get("network_id") != 0
            or type(validated) is not dict
            or type(validated.get("seq")) is not int
            or not 1 <= validated["seq"] < 2**32 - 40
            or type(validated.get("age")) not in (int, float)
            or not 0 <= validated["age"] <= 30
            or info.get("amendment_blocked") is True):
        raise ValueError("XRPL source not on fresh validated mainnet")
    if (type(fee_result) is not dict
            or fee_result.get("status") != "success"
            or type(fee_result.get("drops")) is not dict):
        raise ValueError("Unverified XRP Ledger fee")
    fee = fee_result["drops"].get("open_ledger_fee")
    if type(fee) is not str or not _INTEGER.fullmatch(fee):
        raise ValueError("Missing live XRPL fee in drops")
    if not 0 < int(fee) < 10**25:
        raise ValueError("Unusable XRPL source chain fee")
    return {
        "validated_ledger_index": validated["seq"],
        "network_fee_drops": fee,
        "reserve_base_drops": _xrp_drops(validated.get("reserve_base_xrp")),
        "reserve_increment_drops": _xrp_drops(validated.get("reserve_inc_xrp")),
        "source_network_mainnet_verified": True,
    }


async def _xrp_rpc(client: httpx.AsyncClient, method: str, params: list) -> dict:
    response = await client.post(
        _rpc_url("XRP"), json={"method": method, "params": params},
        headers={"Accept": "application/json",
                 "Content-Type": "application/json"},
    )
    response.raise_for_status()
    if len(response.content) > 80_000:
        raise ValueError("Oversized XRP Ledger mainnet response")
    body = response.json()
    if type(body) is not dict or type(body.get("result")) is not dict:
        raise ValueError("Malformed XRP Ledger method response")
    return body["result"]


async def prepare_original_xrp_unsigned_owner_payment(
    did: str, *, amount: str,
) -> dict:
    """Source XRP original payment; not real Privy signing permission."""
    did = _safe_did(did)
    quote = await get_private_native_thornode_terms(
        did, asset="XRP", side="SELL", stable="USDC", amount=amount,
    )
    owner = quote.get("source_wallet")
    if not _valid_address("XRP", owner):
        raise HTTPException(409, "Original XRP wallet unavailable")
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(9), follow_redirects=False,
        ) as client:
            server = await _xrp_rpc(client, "server_info", [{}])
            fee = await _xrp_rpc(client, "fee", [{}])
            network = inspect_xrpl_mainnet_server(server, fee)
            account = await _xrp_rpc(client, "account_info", [{
                "account": owner,
                "ledger_index": network["validated_ledger_index"],
                "strict": True,
            }])
        # Do not trust an account_info response from a different finalized
        # ledger or a wallet that has already advanced its sequence.
        payment = build_original_unsigned_xrp_thor_payment(
            quote, amount=amount, now=int(time.time()),
            account_info={"result": account}, network_terms=network,
        )
    except (httpx.HTTPError, ValueError, KeyError, TypeError,
            OverflowError):
        raise HTTPException(
            409, "Current original XRP ledger, fee, reserve or source unavailable",
        ) from None
    return {
        "mode": "ORIGINAL_XRP_SOURCE_UNSIGNED_MAINNET_PREFLIGHT",
        "asset": "XRP", "side": "SELL",
        "stable": "USDC",
        "source_wallet": owner,
        "inbound_vault": payment["inbound_vault"],
        "destination_wallet": payment["destination_wallet"],
        "source_amount_drops": payment["source_amount_drops"],
        "network_fee_drops": payment["network_fee_drops"],
        "source_account_reserve_drops": payment["account_reserve_drops"],
        "validated_ledger_index": payment["account_validated_ledger"],
        "account_sequence": payment["account_sequence"],
        "xrpl_mainnet_id_verified": True,
        "source_account_and_fee_verified": True,
        "unsigned_payment": payment,
        "original_privy_policy_verified": False,
        "original_order_claim_verified": False,
        "wallet_signed": False,
        "source_transaction_sent": False,
        "execution_enabled": False, "can_submit": False,
    }
