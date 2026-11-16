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
                from decimal import Decimal
                amounts=("source_amount","destination_amount","network_fee_eth")
                if (found["privy_did"] != did
                        or any(found[k] != leg[k] for k in
                               ("tx_hash","side","stable","kind"))
                        or any(Decimal(str(found[k])) != Decimal(leg[k])
                               for k in amounts)):
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


def get_confirmed_eth_swaps(did: str) -> dict:
    """Authenticated, order-bound BUY/SELL fills only; no PAPER or fee claims.

    The ledger includes a swap only when the durable AUTO order and the
    independently finalized ERC-20 transfer leg agree on owner, hash, side
    and settlement stablecoin. Native WRAP/UNWRAP stages remain separate.
    """
    did = _safe_did(did)
    try:
        url = _db_url()
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                cur.execute(
                    """SELECT o.intent_key,o.side,o.stable,o.tx_hash,
                              l.source_amount,l.destination_amount,
                              l.network_fee_eth,o.finalized_at,
                              n.status AS native_unwrap_status,
                              n.tx_hash AS native_unwrap_hash,
                              n.amount_wei AS native_unwrap_wei,
                              u.source_amount AS verified_unwrap_eth,
                              u.network_fee_eth AS unwrap_network_fee_eth
                       FROM mininex_live_order_intents o
                       JOIN mininex_live_eth_verified_legs l
                         ON l.privy_did=o.privy_did
                        AND l.tx_hash=o.tx_hash
                        AND l.side=o.side
                        AND l.stable=o.stable
                        AND l.kind='SWAP'
                       LEFT JOIN mininex_live_eth_auto_native_steps n
                         ON n.privy_did=o.privy_did
                        AND n.source_key=o.intent_key
                        AND n.operation='WITHDRAW'
                        AND o.side='BUY'
                       LEFT JOIN mininex_live_eth_verified_legs u
                         ON u.privy_did=n.privy_did
                        AND u.tx_hash=n.tx_hash
                        AND u.kind='UNWRAP'
                        AND u.side='BUY'
                        AND u.stable=o.stable
                       WHERE o.privy_did=%s AND o.asset='ETH'
                         AND o.chain_network='ethereum-mainnet'
                         AND o.state='CONFIRMED'
                         AND o.finalized_at IS NOT NULL
                       ORDER BY o.finalized_at DESC,o.intent_key DESC
                       LIMIT 101""", (did,),
                )
                rows = cur.fetchall()
        # Retain a sentinel so a long history is never represented as complete.
        history_truncated = len(rows) > 100
        swaps = []
        for row in rows[:100]:
            side = row["side"]
            stable = row["stable"]
            if side not in SIDES or stable not in STABLES:
                raise ValueError("Invalid confirmed ETH swap identity")
            conversion = "NOT_APPLICABLE"
            conversion_fee = None
            if side == "BUY":
                status = row.get("native_unwrap_status")
                if status is None:
                    conversion = "NOT_STARTED"
                elif status == "CONFIRMED":
                    # A successful DB state alone never proves that purchased
                    # WETH was converted to native ETH in this precise amount.
                    from decimal import Decimal
                    original = Decimal(str(row["destination_amount"]))
                    verified = row.get("verified_unwrap_eth")
                    atomic = row.get("native_unwrap_wei")
                    if (verified is not None and atomic is not None
                            and Decimal(str(verified)) == original
                            and Decimal(str(atomic)) == original * (10 ** 18)
                            and row.get("native_unwrap_hash")):
                        conversion = "CONFIRMED_VERIFIED"
                        conversion_fee = str(row["unwrap_network_fee_eth"])
                    else:
                        conversion = "RECONCILIATION_REQUIRED"
                elif status in ("SENDING", "SUBMITTED", "RECONCILIATION_REQUIRED",
                                "REVERTED"):
                    conversion = status
                else:
                    raise ValueError("Invalid WETH to native conversion state")
            swaps.append({
                "native_conversion_status": conversion,
                "native_conversion_network_fee_eth": conversion_fee,
                "intent_key": row["intent_key"],
                "side": side,
                "stable": stable,
                "tx_hash": row["tx_hash"],
                "spent_asset": stable if side == "BUY" else "WETH",
                "spent_amount": str(row["source_amount"]),
                "received_asset": "WETH" if side == "BUY" else stable,
                "received_amount": str(row["destination_amount"]),
                "network_fee_eth": str(row["network_fee_eth"]),
                "finalized_at": row["finalized_at"].isoformat(),
                "onchain_finalized": True,
            })
        return {
            "asset": "ETH", "network": "ethereum-mainnet",
            "source": "CONFIRMED_AUTO_ROUTER02_SWAPS",
            "swaps": swaps, "history_truncated": history_truncated,
            "includes_paper": False,
            "native_conversion_stages_included": False,
            "realized_pnl_reconciled": False,
            "performance_fee_assessed": False,
        }
    except (psycopg.Error, ValueError, TypeError, KeyError, AttributeError):
        LOG.warning("ETH confirmed BUY/SELL ledger unavailable")
        raise HTTPException(503, "ETH confirmed swap history unavailable") from None
