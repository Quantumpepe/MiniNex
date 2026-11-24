"""Persist an independently verified original bridge outcome exactly once.

Internal receipt verifier only. No client-authorized outcome, no wallet
signer, no profit calculation. Release outstanding funding only after this
original transaction is verified. An unknown/partial/refund case remains open.
"""
import re
import uuid
from decimal import Decimal, localcontext

import psycopg
from psycopg.rows import dict_row
from fastapi import HTTPException

from .storage import _db_url,_ensure_schema,_safe_did

_HASH=re.compile(r"^0x[0-9a-f]{64}$")


def _atomic(value,positive=False):
    if type(value) is not int or value<0 or value>=2**256 or (positive and value==0):
        raise ValueError("Invalid original receipt atomic amount")
    return value


def original_outcome_terms(*,source_hash,destination_hash,received,deposit_id,gas,outcome):
    if not isinstance(source_hash,str) or not _HASH.fullmatch(source_hash.lower()):
        raise ValueError("Invalid original source hash")
    _atomic(gas)
    if outcome=="FINALIZED":
        if not isinstance(destination_hash,str) or not _HASH.fullmatch(destination_hash.lower()):
            raise ValueError("Invalid verified destination hash")
        _atomic(received,True)
        if not isinstance(deposit_id,str) or not deposit_id.isascii() or not deposit_id.isdecimal():
            raise ValueError("Invalid verified bridge deposit")
        _atomic(int(deposit_id))
        destination_hash=destination_hash.lower()
        deposit_id=int(deposit_id)
    elif outcome=="SOURCE_REVERTED":
        if any(x is not None for x in (destination_hash,received,deposit_id)):
            raise ValueError("Reverted transaction cannot have destination proceeds")
    else:
        raise ValueError("Unverified bridge outcome")
    return (source_hash.lower(),destination_hash,deposit_id,received,gas,outcome)


def persist_verified_outcome(did,quote_id,*,source_hash,destination_hash=None,
                             received=None,deposit_id=None,gas,outcome):
    did=_safe_did(did); key=uuid.UUID(quote_id)
    evidence=original_outcome_terms(source_hash=source_hash,destination_hash=destination_hash,
        received=received,deposit_id=deposit_id,gas=gas,outcome=outcome)
    db=_db_url();_ensure_schema(db)
    try:
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""SELECT privy_did FROM mininex_accounts
                    WHERE privy_did=%s FOR UPDATE""",(did,))
                if not cur.fetchone():
                    raise HTTPException(409,"Original settlement owner unavailable")
                cur.execute("""SELECT q.source_chain_id,q.destination_chain_id,q.minimum_output_atomic
                    FROM mininex_ethereum_settlement_quotes q
                    WHERE q.quote_id=%s AND q.privy_did=%s AND
                      (EXISTS(SELECT 1 FROM mininex_ethereum_settlement_manual_attempts m
                         WHERE m.quote_id=q.quote_id AND m.privy_did=q.privy_did
                           AND m.state='SUBMITTED' AND m.source_tx_hash=%s)
                       OR EXISTS(SELECT 1 FROM mininex_ethereum_settlement_submissions s
                         WHERE s.quote_id=q.quote_id AND s.privy_did=q.privy_did
                           AND s.state='SUBMITTED' AND s.source_tx_hash=%s))
                    FOR UPDATE""",(key,did,evidence[0],evidence[0]))
                q=cur.fetchone()
                if not q or (outcome=="FINALIZED" and received<q["minimum_output_atomic"]):
                    raise HTTPException(409,"Finality differs from original submitted order")
                cur.execute("""INSERT INTO mininex_ethereum_settlement_finality
                    (quote_id,privy_did,source_chain_id,destination_chain_id,source_tx_hash,
                    destination_tx_hash,bridge_deposit_id,received_atomic,source_gas_atomic,outcome)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(quote_id) DO NOTHING""",
                    (key,did,q["source_chain_id"],q["destination_chain_id"],*evidence))
                cur.execute("""SELECT source_tx_hash,destination_tx_hash,bridge_deposit_id,
                    received_atomic,source_gas_atomic,outcome
                    FROM mininex_ethereum_settlement_finality WHERE quote_id=%s AND privy_did=%s""",
                    (key,did))
                original=cur.fetchone()
                if not original or tuple(original.values())!=evidence:
                    raise HTTPException(409,"Finality evidence is immutable")
                cur.execute("""UPDATE mininex_ethereum_settlement_manual_attempts
                    SET final_outcome=%s,updated_at=now()
                    WHERE quote_id=%s AND privy_did=%s AND state='SUBMITTED'
                    AND source_tx_hash=%s AND (final_outcome IS NULL OR final_outcome=%s)""",
                    (outcome,key,did,evidence[0],outcome))
        return True
    except psycopg.Error:
        raise HTTPException(503,"Original settlement finality persistence unavailable") from None


def load_verified_outcome(did,quote_id):
    """Read only immutable, independently verified chain settlement records."""
    did=_safe_did(did)
    db=_db_url();_ensure_schema(db)
    try:
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""SELECT source_chain_id,destination_chain_id,
                    source_tx_hash,destination_tx_hash,bridge_deposit_id,
                    received_atomic,source_gas_atomic,outcome
                    FROM mininex_ethereum_settlement_finality
                    WHERE privy_did=%s AND quote_id=%s""",(did,uuid.UUID(quote_id)))
                return cur.fetchone()
    except psycopg.Error:
        raise HTTPException(503,"Original settlement outcome unavailable") from None


def verified_outcome_status(row,quote,source_hash):
    """Restore an original terminal status without querying the bridge again."""
    if row is None:
        return None
    if (row["source_chain_id"]!=quote["source_chain_id"]
            or row["destination_chain_id"]!=quote["destination_chain_id"]
            or row["source_tx_hash"]!=source_hash.lower()):
        raise HTTPException(409,"Stored outcome differs from original transaction")
    outcome=row["outcome"]
    received=None if row["received_atomic"] is None else int(row["received_atomic"])
    deposit=None if row["bridge_deposit_id"] is None else str(row["bridge_deposit_id"])
    gas=int(row["source_gas_atomic"])
    original_outcome_terms(source_hash=source_hash,
        destination_hash=row["destination_tx_hash"],received=received,
        deposit_id=deposit,gas=gas,outcome=outcome)
    if outcome=="FINALIZED" and received<int(quote["minimum_output_atomic"]):
        raise ValueError("Stored settlement falls below original quote")
    with localcontext() as ctx:
        ctx.prec=100
        actual=None if received is None else format(Decimal(received).scaleb(
            -18 if quote["side"]=="BUY" else -6),"f")
        cost=format(Decimal(gas).scaleb(-18),"f")
    return {"state":outcome,"destination_tx_hash":row["destination_tx_hash"],
        "received_amount":actual,"source_fee_native":cost,
        "bridge_deposit_id":deposit,"bridge_message_verified":outcome=="FINALIZED",
        "settlement_verified":outcome=="FINALIZED"}
