"""Immutable historical USD observations for real finalized EVM fills.

A BUY's acquisition/gas USD reference must survive beyond CoinGecko's
recent high-resolution history. We persist only near-original-block market
observations after the Privy-owner receipt is independently verified.
Conflicting transaction/block data cannot rewrite an earlier observation.
This module never creates a fee obligation or sends wallet transactions.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import logging
import re

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .shared_evm_historical_usd import fetch_recent_historical_usd_observation
from .shared_evm_swap_receipts import _pair
from .storage import _db_url, _ensure_schema, _safe_did

LOG=logging.getLogger("mininex.immutable_evm_usd")
_HASH=re.compile(r"^0x[0-9a-f]{64}$")
_ASSETS=frozenset(("BNB","POL","AVAX","LINK","UNI"))
_QUOTED=frozenset(("ETH","BNB","POL","AVAX","USDC","USDT"))


def validate_bound_historical_price(
    *, proof: object, row: object, quoted_asset: str,
) -> dict:
    """Bind a stored provider price to canonical previously confirmed fill."""
    if (type(proof) is not dict or type(row) is not dict
            or proof.get("mode")!="INDEPENDENT_FINALIZED_EVM_EXECUTION"
            or proof.get("onchain_finalized") is not True
            or proof.get("asset") not in _ASSETS
            or quoted_asset not in _QUOTED
            or type(proof.get("tx_hash")) is not str
            or not _HASH.fullmatch(proof["tx_hash"])
            or type(proof.get("block_hash")) is not str
            or not _HASH.fullmatch(proof["block_hash"])
            or type(proof.get("block_number")) is not int
            or type(proof.get("block_unix")) is not int
            or proof["block_number"]<=0 or proof["block_unix"]<=0
            or row.get("asset")!=proof["asset"]
            or row.get("chain_id")!=proof.get("chain_id")
            or row.get("tx_hash")!=proof["tx_hash"]
            or row.get("quoted_asset")!=quoted_asset
            or row.get("block_number")!=proof["block_number"]
            or row.get("block_hash")!=proof["block_hash"]
            or row.get("block_unix")!=proof["block_unix"]
            or row.get("provider")!="COINGECKO_MARKET_CHART_RANGE"):
        raise ValueError("Historical rate cannot be attached to this onchain fill")
    try:
        observed = row["observed_unix"]
        price=Decimal(str(row["usd_price"]))
        if (type(observed) is not int
                or abs(observed-proof["block_unix"])>180
                or not price.is_finite() or price<=0 or price>=Decimal("1e12")
                or (quoted_asset in ("USDC","USDT")
                    and not Decimal(".95")<=price<=Decimal("1.05"))):
            raise ValueError("Unverifiable historic execution currency price")
    except (KeyError, InvalidOperation, TypeError, ValueError):
        raise ValueError("Historical price ledger entry corrupt") from None
    return {
        "mode":"HISTORICAL_EXECUTION_USD_REFERENCE_ONLY",
        "asset":quoted_asset,
        "provider":"COINGECKO_MARKET_CHART_RANGE",
        "execution_block_unix":proof["block_unix"],
        "rate_observed_unix":observed,
        "rate_age_seconds":abs(observed-proof["block_unix"]),
        "usd_price":format(price,"f"),
        "onchain_execution_finality_verified_by_this_rate":False,
        "fifo_cost_basis_proved_by_this_rate":False,
        "durably_recorded_on_finalized_owner_trade":True,
    }


def _read_immutable_price(did: str,proof: dict, quoted_asset: str)->dict|None:
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT asset,chain_id,tx_hash,quoted_asset,
                          block_number,block_hash,block_unix,observed_unix,
                          usd_price,provider
                   FROM mininex_evm_immutable_fill_usd_prices
                   WHERE privy_did=%s AND asset=%s AND tx_hash=%s
                     AND quoted_asset=%s""",
                (did,proof["asset"],proof["tx_hash"],quoted_asset))
            row=cur.fetchone()
    return validate_bound_historical_price(
        proof=proof,row=row,quoted_asset=quoted_asset) if row is not None else None


def _commit_original_observation(
    did: str,proof: dict, quoted_asset: str, observation: dict,
) -> dict:
    exact=observation.get("usd_price")
    if type(exact) is not str or "e" in exact.lower() or len(exact)>60:
        raise ValueError("Precise historical provider amount required")
    value=Decimal(exact)
    if not value.is_finite() or not Decimal(0)<value<Decimal("1e12"):
        raise ValueError("Unreliable immutable historic USD rate")
    price=value.quantize(Decimal("0.000000000000000001"),
                         rounding=ROUND_HALF_EVEN)
    if not price>0:
        raise ValueError("Historical execution USD precision unavailable")
    row={
        "asset":proof["asset"],"chain_id":proof["chain_id"],
        "tx_hash":proof["tx_hash"],"quoted_asset":quoted_asset,
        "block_number":proof["block_number"],"block_hash":proof["block_hash"],
        "block_unix":proof["block_unix"],
        "observed_unix":observation["rate_observed_unix"],
        "usd_price":price,"provider":"COINGECKO_MARKET_CHART_RANGE",
    }
    validated=validate_bound_historical_price(
        proof=proof,row=row,quoted_asset=quoted_asset)
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """INSERT INTO mininex_evm_immutable_fill_usd_prices
                   (privy_did,asset,chain_id,tx_hash,quoted_asset,
                    block_number,block_hash,block_unix,observed_unix,
                    usd_price,provider)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(privy_did,asset,tx_hash,quoted_asset)
                   DO NOTHING""",
                (did,proof["asset"],proof["chain_id"],proof["tx_hash"],
                 quoted_asset,proof["block_number"],proof["block_hash"],
                 proof["block_unix"],row["observed_unix"],price,
                 "COINGECKO_MARKET_CHART_RANGE"))
            cur.execute(
                """SELECT asset,chain_id,tx_hash,quoted_asset,
                          block_number,block_hash,block_unix,observed_unix,
                          usd_price,provider
                   FROM mininex_evm_immutable_fill_usd_prices
                   WHERE privy_did=%s AND asset=%s AND tx_hash=%s
                     AND quoted_asset=%s""",
                (did,proof["asset"],proof["tx_hash"],quoted_asset))
            saved=cur.fetchone()
            if saved is None:
                raise ValueError("Historical owner quote not persisted")
            result=validate_bound_historical_price(
                proof=proof,row=saved,quoted_asset=quoted_asset)
    return result


async def historical_usd_for_verified_fill(
    *,did: str,proof: dict,quoted_asset: str,
    client: httpx.AsyncClient,now: int | None = None,
)->dict:
    """Read immutable past rate or capture one fresh near-execution sample."""
    did=_safe_did(did)
    if (type(proof) is not dict or proof.get("asset") not in _ASSETS
            or quoted_asset not in _QUOTED
            or type(client) is not httpx.AsyncClient):
        raise ValueError("Untrusted original source trade and rate")
    route=_pair(proof["asset"],proof["stable"],proof["side"])
    if proof.get("chain_id")!=route["chain_id"]:
        raise ValueError("Wrong chain in original historical cost proof")
    try:
        existing=_read_immutable_price(did,proof,quoted_asset)
        if existing is not None:
            return existing
        recent=await fetch_recent_historical_usd_observation(
            asset=quoted_asset,block_unix=proof["block_unix"],
            client=client,now=now)
        if (recent.get("asset")!=quoted_asset
                or recent.get("execution_block_unix")!=proof["block_unix"]):
            raise ValueError("Historical rate source does not match fill")
        return _commit_original_observation(
            did,proof,quoted_asset,recent)
    except psycopg.Error:
        LOG.warning("Original finalized EVM historical rate ledger unavailable")
        raise HTTPException(
            503,"Cannot durably prove historical EVM fee reference") from None
