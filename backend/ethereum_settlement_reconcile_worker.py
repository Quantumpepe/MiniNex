"""Passive original bridge receipts; independent of active trading or app UI."""
import asyncio
from decimal import Decimal,localcontext
import logging

import psycopg
from psycopg.rows import dict_row

from .storage import _db_url,_ensure_schema
from .ethereum_settlement_status import check_ethereum_settlement

LOG=logging.getLogger("mininex.ethereum_settlement_reconcile")


def pending_bridge_receipts(page_size=20):
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT q.privy_did,q.quote_id,q.asset,q.side,q.stable,
                q.input_atomic,attempt.source_tx_hash
                FROM mininex_ethereum_settlement_quotes q
                JOIN (
                    SELECT quote_id,privy_did,source_tx_hash,updated_at
                    FROM mininex_ethereum_settlement_manual_attempts
                    WHERE state='SUBMITTED' AND final_outcome IS NULL
                    UNION ALL
                    SELECT quote_id,privy_did,source_tx_hash,updated_at
                    FROM mininex_ethereum_settlement_submissions WHERE state='SUBMITTED'
                ) attempt ON attempt.quote_id=q.quote_id AND attempt.privy_did=q.privy_did
                WHERE NOT EXISTS(SELECT 1 FROM mininex_ethereum_settlement_finality f
                    WHERE f.quote_id=q.quote_id AND f.privy_did=q.privy_did)
                ORDER BY attempt.updated_at LIMIT %s""",(page_size,))
            return cur.fetchall()


def touch_checked(row):
    db=_db_url()
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor() as cur:
            for table in ("mininex_ethereum_settlement_manual_attempts",
                          "mininex_ethereum_settlement_submissions"):
                cur.execute("UPDATE "+table+" SET updated_at=now() WHERE quote_id=%s "
                    "AND privy_did=%s AND state='SUBMITTED' AND source_tx_hash=%s",
                    (row["quote_id"],row["privy_did"],row["source_tx_hash"]))


async def reconcile_bridge_receipts_once():
    rows=await asyncio.to_thread(pending_bridge_receipts)
    for row in rows:
        try:
            with localcontext() as ctx:
                ctx.prec=100
                amount=format(Decimal(row["input_atomic"]).scaleb(-6 if row["side"]=="BUY" else -18),"f")
            await check_ethereum_settlement(row["privy_did"],asset=row["asset"],
                side=row["side"],stable=row["stable"],amount=amount,
                tx_hash=row["source_tx_hash"],quote_id=str(row["quote_id"]))
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.warning("Original bridge receipt still requires reconciliation")
        finally:
            await asyncio.to_thread(touch_checked,row)
    return {"checked":len(rows)}
