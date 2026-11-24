"""Durable read-only quote identity. No signing, sending or profit writes."""
from __future__ import annotations

import re
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from fastapi import HTTPException

from .storage import _db_url, _ensure_schema, _safe_did
from .ethereum_settlement_plan import check_evm_quote_route

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
    check_evm_quote_route(terms)
    for field in ("input_atomic", "minimum_output_atomic"):
        if not isinstance(terms[field], str) or not _UINT.fullmatch(terms[field]):
            raise ValueError("Invalid atomic amount")
        if int(terms[field]) >= 2**256:
            raise ValueError("Atomic amount exceeds uint256")
    if (type(terms["issued_at"]) is not int or type(terms["expires_at"]) is not int
            or not 0 < terms["expires_at"] - terms["issued_at"] <= 30):
        raise ValueError("Invalid quote lifetime")
    digest=terms.get("route_sha256")
    if digest is not None and (not isinstance(digest,str) or not re.fullmatch(r"[0-9a-f]{64}",digest)):
        raise ValueError("Invalid inspected route digest")
    if terms.get("unsigned_transaction") is not None:
        from .ethereum_settlement_calldata import route_digest
        if route_digest(terms["unsigned_transaction"],terms["source_chain_id"])!=digest:
            raise ValueError("Stored route transaction differs from digest")
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
                     input_atomic,minimum_output_atomic,issued_at,expires_at,route_sha256,unsigned_transaction)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (key,did,terms["wallet_address"],terms["asset"],terms["side"],
                     terms["stable"],terms["source_chain_id"],terms["destination_chain_id"],
                     terms["source_token"],terms["destination_token"],
                     terms["input_atomic"],terms["minimum_output_atomic"],
                     terms["issued_at"],terms["expires_at"],terms.get("route_sha256"),
                     Jsonb(terms["unsigned_transaction"]) if terms.get("unsigned_transaction") else None))
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



def pending_bridge_order(cur,did,asset):
    """Shared owner/coin pending gate for manual and automated bridge paths.
    Submission callers must hold the owner account lock."""
    cur.execute("""SELECT 1 FROM mininex_ethereum_settlement_quotes q
        WHERE q.privy_did=%s AND q.asset=%s
        AND NOT EXISTS(SELECT 1 FROM mininex_ethereum_settlement_finality f
                       WHERE f.quote_id=q.quote_id)
        AND (EXISTS(SELECT 1 FROM mininex_ethereum_settlement_manual_attempts m
              WHERE m.quote_id=q.quote_id AND m.privy_did=q.privy_did
                AND m.final_outcome IS NULL
                AND m.state IN ('AWAITING_SIGNATURE','SUBMITTED','UNCERTAIN'))
          OR EXISTS(SELECT 1 FROM mininex_ethereum_settlement_submissions s
              WHERE s.quote_id=q.quote_id AND s.privy_did=q.privy_did
                AND s.state IN ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')))
        LIMIT 1""",(did,asset))
    return cur.fetchone() is not None


def claim_authorized_submission_locked(cur, *, did, quote_id, intent_key,
                                       request_sha256, now):
    """Internal final claim; caller must already verify grant, calldata and budget.

    Caller must hold the existing account/budget/policy locks and check all
    legacy network journals in the same transaction. No public write route.
    This primitive alone is NOT an execution authorization.
    """
    did = _safe_did(did)
    key = uuid.UUID(quote_id)
    if (not isinstance(intent_key, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", intent_key)
            or not isinstance(request_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", request_sha256)
            or type(now) is not int):
        raise ValueError("Invalid authorized submission identity")
    cur.execute("""SELECT * FROM mininex_ethereum_settlement_quotes
        WHERE quote_id=%s AND privy_did=%s FOR UPDATE""", (key,did))
    quote = cur.fetchone()
    if (not quote or not quote.get("route_sha256")
            or not quote["issued_at"] <= now < quote["expires_at"]):
        raise HTTPException(409, "Settlement quote expired or unavailable")
    if pending_bridge_order(cur,did,quote["asset"]):
        raise HTTPException(409,"Previous swap requires reconciliation")
    cur.execute("""INSERT INTO mininex_ethereum_settlement_submissions
        (quote_id,privy_did,intent_key,source_chain_id,request_sha256,state)
        VALUES (%s,%s,%s,%s,%s,'SENDING')
        ON CONFLICT DO NOTHING RETURNING quote_id""",
        (key,did,intent_key,quote["source_chain_id"],request_sha256))
    if cur.fetchone() is None:
        raise HTTPException(409, "Settlement submission already claimed")
    return str(key)


def record_claimed_submission_locked(cur, *, did, quote_id, request_sha256,
                                     source_tx_hash):
    """Record a single trusted transport result; ambiguity is never a retry."""
    did = _safe_did(did)
    key = uuid.UUID(quote_id)
    if not isinstance(request_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}",request_sha256):
        raise ValueError("Invalid submission digest")
    if source_tx_hash is not None:
        if not isinstance(source_tx_hash,str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}",source_tx_hash):
            raise ValueError("Invalid source transaction hash")
        source_tx_hash = source_tx_hash.lower()
    state = "SUBMITTED" if source_tx_hash else "RECONCILIATION_REQUIRED"
    cur.execute("""UPDATE mininex_ethereum_settlement_submissions
        SET state=%s,source_tx_hash=%s,updated_at=now()
        WHERE quote_id=%s AND privy_did=%s AND request_sha256=%s AND state='SENDING'
        RETURNING quote_id""", (state,source_tx_hash,key,did,request_sha256))
    if cur.fetchone() is None:
        raise HTTPException(409, "Original settlement submission not found")
    return state


def load_submission(did, quote_id):
    """Read a trusted transport result; a client hash never creates a claim."""
    did = _safe_did(did)
    url = _db_url()
    try:
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""SELECT state,source_tx_hash,source_chain_id FROM
                    mininex_ethereum_settlement_submissions
                    WHERE privy_did=%s AND quote_id=%s""", (did,uuid.UUID(quote_id)))
                return cur.fetchone()
    except psycopg.Error:
        raise HTTPException(503, "Settlement submission unavailable") from None
