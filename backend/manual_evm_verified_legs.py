"""Authenticated, immutable MANUAL EVM swap-leg history (not AUTO or PAPER).

Only an independently finalized, owner-bound Router02 ERC20 transfer can be
recorded. A wrapped-native swap is a LEG, not evidence of completed native
withdrawal/deposit. This module does not send, sign, book PnL or charge fees.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import logging
import re

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .shared_evm_swap_receipts import _pair, check_user_evm_swap
from .storage import _db_url, _ensure_schema, _prepare_rows, _safe_did

LOG = logging.getLogger("mininex.manual_evm_verified_legs")
ASSETS = frozenset(("BNB", "POL", "AVAX", "LINK", "QNT", "UNI", "ARB"))
HASH = re.compile(r"^0x[a-fA-F0-9]{64}$")
ADDR = re.compile(r"^0x[a-fA-F0-9]{40}$")


def _quantity(raw: object, *, allow_zero: bool = False) -> str:
    if type(raw) is not str or len(raw) > 72:
        raise ValueError("Unverified manual EVM amount")
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise ValueError("Unverified manual EVM amount") from None
    if (not number.is_finite() or number < 0
            or (not allow_zero and number == 0)
            or number >= Decimal("1e27")
            or number.as_tuple().exponent < -18):
        raise ValueError("Invalid manual EVM amount")
    return format(number, "f")


def validated_manual_evm_leg(
    *, asset: str, tx_hash: str, side: str, stable: str, receipt: dict
) -> dict:
    """Use amounts solely from independently verified canonical on-chain logs."""
    if asset not in ASSETS or type(tx_hash) is not str or not HASH.fullmatch(tx_hash):
        raise ValueError("Unsupported manual EVM asset or transaction")
    pair = _pair(asset, stable, side)
    if (type(receipt) is not dict
            or receipt.get("mode") != "SHARED_EVM_FINALIZED_SWAP_VERIFICATION"
            or receipt.get("state") != "FINALIZED_ERC20_SWAP_LEG"
            or receipt.get("swap_erc20_leg_verified") is not True
            or receipt.get("transaction_finalized") is not True
            or receipt.get("native_coin_cycle_complete") is not False
            or receipt.get("execution_enabled") is not False
            or receipt.get("can_submit") is not False
            or receipt.get("real_order_recorded") is not False
            or receipt.get("performance_fee_assessed") is not False
            or receipt.get("asset") != asset
            or receipt.get("network") != pair["network"]
            or receipt.get("chain_id") != pair["chain_id"]
            or receipt.get("side") != side
            or receipt.get("stable") != stable
            or receipt.get("tx_hash") != tx_hash.lower()):
        raise ValueError("Manual EVM swap finality or identity mismatch")
    wallet = receipt.get("wallet_address")
    if type(wallet) is not str or not ADDR.fullmatch(wallet):
        raise ValueError("Unverified owner wallet for manual EVM leg")
    return {
        "asset": asset, "chain_id": pair["chain_id"],
        "network": pair["network"], "tx_hash": tx_hash.lower(),
        "wallet_address": wallet.lower(), "side": side, "stable": stable,
        "source_amount": _quantity(receipt.get("source_spent_amount")),
        "destination_amount": _quantity(receipt.get("destination_received_amount")),
        "network_fee_native": _quantity(receipt.get("network_fee_native"), allow_zero=True),
    }


def save_manual_evm_leg(did: str, leg: dict) -> dict:
    """Atomic insert or verified no-op; transaction cannot move between owners."""
    did = _safe_did(did)
    if type(leg) is not dict or leg.get("asset") not in ASSETS:
        raise ValueError("Verified manual EVM swap leg required")
    url = _db_url()
    try:
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                cur.execute(
                    """INSERT INTO mininex_live_evm_manual_verified_legs
                       (privy_did,asset,chain_id,tx_hash,wallet_address,
                        side,stable,source_amount,destination_amount,network_fee_native)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (chain_id,tx_hash) DO NOTHING""",
                    (did, leg["asset"],leg["chain_id"],leg["tx_hash"],
                     leg["wallet_address"],leg["side"],leg["stable"],
                     leg["source_amount"],leg["destination_amount"],
                     leg["network_fee_native"]),
                )
                cur.execute(
                    """SELECT privy_did,asset,chain_id,tx_hash,wallet_address,
                              side,stable,source_amount,destination_amount,
                              network_fee_native
                       FROM mininex_live_evm_manual_verified_legs
                       WHERE chain_id=%s AND tx_hash=%s""",
                    (leg["chain_id"], leg["tx_hash"]),
                )
                found = cur.fetchone()
                amounts = ("source_amount", "destination_amount", "network_fee_native")
                keys = ("asset", "chain_id", "tx_hash", "wallet_address", "side", "stable")
                if (type(found) is not dict or found.get("privy_did") != did
                        or any(found.get(k) != leg[k] for k in keys)
                        or any(Decimal(str(found.get(k))) != Decimal(leg[k])
                               for k in amounts)):
                    raise HTTPException(
                        409, "EVM transaction is already assigned to another manual trade"
                    )
        return {
            **leg, "recorded": True, "onchain_finalized": True,
            "native_coin_cycle_complete": False, "full_order_settled": False,
            "realized_profit_verified": False,
            "performance_fee_assessed": False, "execution_enabled": False,
        }
    except psycopg.Error:
        LOG.warning("Manual EVM swap activity could not be stored")
        raise HTTPException(503, "Manual EVM history temporarily unavailable") from None


async def record_manual_evm_leg(
    did: str, *, asset: str, tx_hash: str, side: str, stable: str
) -> dict:
    did = _safe_did(did)
    if asset not in ASSETS or type(tx_hash) is not str or not HASH.fullmatch(tx_hash):
        raise HTTPException(422, "Unsupported EVM manual swap")
    try:
        _pair(asset, stable, side)
    except ValueError:
        raise HTTPException(422, "Unsupported manual EVM settlement") from None
    # Re-fetch real Privy ownership and canonical on-chain finality on POST.
    # Never trust a frontend-supplied receipt, quantity or 'success' flag.
    result = await check_user_evm_swap(
        did, tx_hash, asset=asset, side=side, stable=stable
    )
    try:
        leg = validated_manual_evm_leg(
            asset=asset, tx_hash=tx_hash, side=side, stable=stable, receipt=result
        )
    except (ValueError, KeyError, TypeError):
        raise HTTPException(409, "Manual EVM transaction not finalized") from None
    return save_manual_evm_leg(did, leg)


def get_manual_evm_legs(did: str, *, asset: str, limit: int = 30) -> dict:
    did = _safe_did(did)
    if asset not in ASSETS or type(limit) is not int or not 1 <= limit <= 50:
        raise HTTPException(422, "Unsupported manual EVM history request")
    url = _db_url()
    try:
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                cur.execute(
                    """SELECT asset,chain_id,tx_hash,side,stable,source_amount,
                              destination_amount,network_fee_native,created_at
                       FROM mininex_live_evm_manual_verified_legs
                       WHERE privy_did=%s AND asset=%s
                       ORDER BY created_at DESC,tx_hash DESC LIMIT %s""",
                    (did, asset, limit),
                )
                rows = cur.fetchall()
        network = _pair(asset, "USDT" if asset == "BNB" else "USDC", "BUY")["network"]
        return {
            "mode": "FINALIZED_EVM_MANUAL_LEGS_ONLY", "asset": asset,
            "network": network, "legs": [
                {
                    **{k: (v.isoformat() if k == "created_at" else str(v))
                       for k, v in row.items()},
                    "kind": "SWAP", "onchain_finalized": True,
                    "native_coin_cycle_complete": False,
                    "realized_profit_verified": False,
                }
                for row in rows
            ],
            "simulation_included": False, "performance_fee_assessed": False,
            "full_order_settled": False,
        }
    except psycopg.Error:
        raise HTTPException(503, "Manual EVM activity temporarily unavailable") from None
