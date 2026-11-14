"""Persist real, finalized Privy-owned ETH swap and WETH9 transaction legs.

Unlike Paper or preliminary receipt previews, this is durable on-chain trade
activity. Leg recording does NOT settle a full multi-transaction order, credit
portfolio, assess performance fees, or trigger trading.
"""
from __future__ import annotations

import logging
import re

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .eth_router02_receipts import check_user_router02_receipt
from .eth_weth9_receipts import check_user_weth9_receipt
from .storage import _db_url, _ensure_schema, _prepare_rows, _safe_did

LOG = logging.getLogger("mininex.real_eth_legs")
HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
KINDS = {"SWAP", "WRAP", "UNWRAP"}
SIDES = {"BUY", "SELL"}
STABLES = {"USDC", "USDT"}


def validated_finalized_leg(*, tx_hash: str, side: str, stable: str,
                            kind: str, receipt: dict) -> dict:
    if (type(tx_hash) is not str or not HASH.fullmatch(tx_hash)
            or side not in SIDES or stable not in STABLES or kind not in KINDS
            or type(receipt) is not dict
            or receipt.get("tx_hash") != tx_hash.lower()
            or receipt.get("asset") != "ETH"
            or receipt.get("network") != "ethereum-mainnet"
            or receipt.get("transaction_finalized") is not True
            or receipt.get("execution_enabled") is not False):
        raise ValueError("ETH transaction identity or finality mismatch")
    if kind == "SWAP":
        if (receipt.get("state") != "FINALIZED_ERC20_SWAP_LEG"
                or receipt.get("swap_erc20_leg_verified") is not True
                or receipt.get("side") != side or receipt.get("stable") != stable):
            raise ValueError("ETH swap fill unavailable")
        source, destination = (
            receipt.get("source_spent_amount"),
            receipt.get("destination_received_amount"),
        )
        if receipt.get("swap_output_asset") != ("WETH" if side == "BUY" else stable):
            raise ValueError("Unexpected swap output")
    else:
        expected = "DEPOSIT" if side == "SELL" else "WITHDRAW"
        if (kind != ("WRAP" if side == "SELL" else "UNWRAP")
                or receipt.get("operation") != expected
                or receipt.get("state") != "FINALIZED_WETH9_CONVERSION"
                or receipt.get("confirmed_native_conversion") is not True):
            raise ValueError("Unexpected native ETH conversion")
        source = destination = receipt.get("weth9_event_amount")

    from decimal import Decimal, InvalidOperation
    def decimal_units(value, *, zero=False):
        if type(value) is not str or len(value)>75:
            raise ValueError("Amount unavailable")
        try:
            d = Decimal(value)
        except InvalidOperation:
            raise ValueError("Amount unavailable") from None
        if not d.is_finite() or d<0 or (not zero and d<=0) or d>=Decimal("1e27"):
            raise ValueError("Invalid amount")
        if d.as_tuple().exponent < -18:
            raise ValueError("Excess amount precision")
        return str(d)

    return {
        "tx_hash": tx_hash.lower(), "side": side, "stable": stable,
        "kind": kind, "source_amount": decimal_units(source),
        "destination_amount": decimal_units(destination),
        "network_fee_eth": decimal_units(receipt.get("network_fee_eth"),zero=True),
    }


def save_verified_leg(did: str, leg: dict) -> dict:
    """Exactly-once, owner-scoped record. A conflicting hash is never reassigned."""
    did = _safe_did(did)
    if type(leg) is not dict:
        raise ValueError("Verified leg required")
    url = _db_url()
    try:
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur,did)
                cur.execute(
                    """INSERT INTO mininex_live_eth_verified_legs
                       (privy_did,tx_hash,side,stable,kind,
                        source_amount,destination_amount,network_fee_eth)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (tx_hash) DO NOTHING""",
                    (did, leg["tx_hash"],leg["side"],leg["stable"],leg["kind"],
                     leg["source_amount"],leg["destination_amount"],
                     leg["network_fee_eth"]),
                )
                cur.execute(
                    """SELECT privy_did,tx_hash,side,stable,kind,
                              source_amount,destination_amount,network_fee_eth
                       FROM mininex_live_eth_verified_legs
                       WHERE tx_hash=%s""", (leg["tx_hash"],),
                )
                found=cur.fetchone()
                if type(found) is not dict:
                    raise HTTPException(503,"ETH fill persistence failed")
                if (found["privy_did"] != did
                        or any(str(found[k]) != str(leg[k]) for k in (
                            "tx_hash","side","stable","kind",
                            "source_amount","destination_amount","network_fee_eth"))):
                    raise HTTPException(409,"Transaction already assigned to another ETH trade")
        return {"asset":"ETH",**leg,"recorded":True,
                "onchain_finalized":True,"full_order_settled":False,
                "portfolio_credit_recorded":False,"execution_enabled":False}
    except psycopg.Error:
        LOG.warning("ETH verified leg database unavailable")
        raise HTTPException(503,"ETH trade history unavailable") from None


async def record_real_eth_leg(did: str, *, tx_hash: str, side: str,
                              stable: str, kind: str) -> dict:
    did=_safe_did(did)
    if (type(tx_hash) is not str or not HASH.fullmatch(tx_hash)
            or side not in SIDES or stable not in STABLES or kind not in KINDS
            or (kind=="WRAP" and side!="SELL")
            or (kind=="UNWRAP" and side!="BUY")):
        raise HTTPException(422,"Invalid ETH trade stage")
    if kind=="SWAP":
        receipt=await check_user_router02_receipt(did,tx_hash,side=side,stable=stable)
    else:
        receipt=await check_user_weth9_receipt(did,tx_hash,
            operation="DEPOSIT" if kind=="WRAP" else "WITHDRAW")
    try:
        leg=validated_finalized_leg(
            tx_hash=tx_hash,side=side,stable=stable,kind=kind,receipt=receipt)
    except ValueError:
        raise HTTPException(409,"ETH transaction is not independently finalized") from None
    return save_verified_leg(did,leg)


def get_real_eth_legs(did: str) -> dict:
    did=_safe_did(did)
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur,did)
                cur.execute(
                    """SELECT tx_hash,side,stable,kind,source_amount,
                              destination_amount,network_fee_eth,created_at
                       FROM mininex_live_eth_verified_legs
                       WHERE privy_did=%s
                       ORDER BY created_at DESC,tx_hash DESC LIMIT 50""",(did,))
                rows=cur.fetchall()
        return {"asset":"ETH","source":"FINALIZED_ONCHAIN_LEGS",
                "legs":[{k:(v.isoformat() if k=="created_at" else str(v))
                         for k,v in row.items()} for row in rows],
                "full_order_settled":False,"simulation_included":False}
    except psycopg.Error:
        raise HTTPException(503,"ETH trade history unavailable") from None
