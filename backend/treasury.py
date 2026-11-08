"""Fail-closed MiniNex treasury destination and collection planning.

This module NEVER signs, sends, approves or collects tokens. Live settlement
requires a future independently reconciled execution and verified receipt.
Performance fee debts are denominated in USD; an ETH-chain USDC/USDT transfer
amount must be quoted at execution, never inferred from dollar parity.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import os
import re

from .fees import decimal_amount

EVM_ADDRESS=re.compile(r"^0x[a-fA-F0-9]{40}$")
ZERO_ADDRESS="0x"+"0"*40
SUPPORTED_TOKENS=("USDC","USDT")
SUPPORTED_TREASURY_NETWORK="ethereum"


@dataclass(frozen=True)
class TreasuryConfig:
    address: str
    network: str = SUPPORTED_TREASURY_NETWORK

    def output(self) -> dict:
        return {"configured":True, "network":self.network,
                "address":self.address,"collection_enabled":False,
                "payment_mode":"VERIFIED_LIVE_SETTLEMENT_REQUIRED"}


def validate_treasury_address(raw: str) -> str:
    """Validate a 20-byte EVM destination; do not accept BTC as an EVM address."""
    if not isinstance(raw,str) or not EVM_ADDRESS.fullmatch(raw) or raw.lower()==ZERO_ADDRESS:
        raise ValueError("MININEX_TREASURY_WALLET must be a nonzero Ethereum address")
    return raw


def configured_treasury() -> TreasuryConfig | None:
    raw=os.getenv("MININEX_TREASURY_WALLET","").strip()
    if not raw:
        return None
    return TreasuryConfig(address=validate_treasury_address(raw))


def treasury_status() -> dict:
    """Safe, read-only operational state; never implies live payments are enabled."""
    try:
        config=configured_treasury()
    except ValueError:
        return {"configured":False,"valid":False,
                "network":SUPPORTED_TREASURY_NETWORK,
                "collection_enabled":False,
                "payment_mode":"DISABLED_INVALID_TREASURY"}
    return {
        "configured":config is not None,
        "valid":True,
        "network":SUPPORTED_TREASURY_NETWORK,
        "collection_enabled":False,
        "payment_mode":"VERIFIED_LIVE_SETTLEMENT_REQUIRED",
    }


def create_unfunded_instruction(
    *, execution_id: str, fee_due_usd: object, token: str,
) -> dict:
    """Produce an AUDIT-ONLY payment intent; never a transferable order.

    The fiat-denominated fee and actual ERC20 token units differ whenever
    USDC/USDT trade off peg, so the token amount remains unknown until a
    verified settlement quote and confirmed live receipt exist.
    """
    if not isinstance(execution_id,str) or not 1<=len(execution_id)<=180:
        raise ValueError("invalid execution id")
    if token not in SUPPORTED_TOKENS:
        raise ValueError("Treasury settlement supports Ethereum USDC or USDT")
    fee=decimal_amount(fee_due_usd)
    if fee<=Decimal("0"):
        raise ValueError("Only positive fee obligations create intents")
    config=configured_treasury()
    if config is None:
        raise ValueError("Treasury wallet not configured")
    return {
        "execution_id":execution_id,
        "treasury_address":config.address,
        "network":config.network,
        "token":token,
        "fee_due_usd":str(fee),
        "token_amount":None,
        "status":"AWAITING_VERIFIED_LIVE_SETTLEMENT",
        "can_execute":False,
        "paper":False,
    }
