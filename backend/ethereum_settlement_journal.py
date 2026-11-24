"""Durable read-only quote identity. No signing, sending or profit writes."""
from __future__ import annotations

import re
import uuid

import psycopg
from psycopg.rows import dict_row
from fastapi import HTTPException

from .storage import _db_url, _ensure_schema, _safe_did

_ADDR = re.compile(r"^0x[0-9a-f]{40}$")
_UINT = re.compile(r"^[1-9][0-9]{0,77}$")
_CHAINS = {"BNB": 56, "POL": 137, "AVAX": 43114, "ARB": 42161}


def validate_quote_terms(terms):
    asset, side, stable = terms["asset"], terms["side"], terms["stable"]
    if asset not in _CHAINS or side not in ("BUY", "SELL") or stable not in ("USDC", "USDT"):
        raise ValueError("Invalid settlement pair")
    expected = (1, _CHAINS[asset]) if side == "BUY" else (_CHAINS[asset], 1)
    if (terms["source_chain_id"], terms["destination_chain_id"]) != expected:
        raise ValueError("Settlement must use Ethereum")
    for field in ("wallet_address", "source_token", "destination_token"):
        if not isinstance(terms[field], str) or not _ADDR.fullmatch(terms[field]):
            raise ValueError("Invalid quote address")
    for field in ("input_atomic", "minimum_output_atomic"):
        if not isinstance(terms[field], str) or not _UINT.fullmatch(terms[field]):
            raise ValueError("Invalid atomic amount")
        if int(terms[field]) >= 2**256:
            raise ValueError("Atomic amount exceeds uint256")
    if (type(terms["issued_at"]) is not int or type(terms["expires_at"]) is not int
            or not 0 < terms["expires_at"] - terms["issued_at"] <= 30):
        raise ValueError("Invalid quote lifetime")
    return terms


def store_quote(did, terms):
    did = _safe_did(did)
    validate_quote_terms(terms)
    key = uuid.uuid4()
    url = _db_url()
    try:
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO mininex_accounts(privy_did) VALUES (%s)
                               ON CONFLICT (privy_did) DO NOTHING""", (did,))
                cur.execute("""INSERT INTO mininex_ethereum_settlement_quotes
                    (quote_id,privy_did,wallet_address,asset,side,stable,
                     source_chain_id,destination_chain_id,source_token,destination_token,
                     input_atomic,minimum_output_atomic,issued_at,expires_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (key,did,terms["wallet_address"],terms["asset"],terms["side"],
                     terms["stable"],terms["source_chain_id"],terms["destination_chain_id"],
                     terms["source_token"],terms["destination_token"],
                     terms["input_atomic"],terms["minimum_output_atomic"],
                     terms["issued_at"],terms["expires_at"]))
    except psycopg.Error:
        raise HTTPException(503, "Settlement quote storage unavailable") from None
    return str(key)


def load_quote(did, quote_id):
    did = _safe_did(did)
    try:
        key = uuid.UUID(quote_id)
        if str(key) != quote_id:
            raise ValueError("Noncanonical quote ID")
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, "Invalid settlement quote ID") from None
    url = _db_url()
    try:
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""SELECT * FROM mininex_ethereum_settlement_quotes
                    WHERE quote_id=%s AND privy_did=%s""", (key,did))
                row = cur.fetchone()
    except psycopg.Error:
        raise HTTPException(503, "Settlement quote storage unavailable") from None
    if row is None:
        raise HTTPException(404, "Settlement quote not found")
    row["input_atomic"] = str(row["input_atomic"])
    row["minimum_output_atomic"] = str(row["minimum_output_atomic"])
    return validate_quote_terms(row)
