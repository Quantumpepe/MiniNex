"""Immutable per-sale 5% obligation, with no HWM and no signing.

A durable accounting record is not a payment instruction. Recalculate the
original FIFO snapshot under the owner lock before booking, and never charge
a legacy fee event a second time. Legacy/native-only fills without original
cost evidence cannot generate obligations.
"""
from __future__ import annotations
import hashlib
import json
import logging
from threading import Lock
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from .storage import _db_url,_ensure_schema,_safe_did
from .realized_net_snapshot import NET_FILLS_SQL,hydrate_net_rows
from .realized_net_accounting import calculate_realized_net
from .ethereum_settlement_plan import COIN_NETWORKS

LOG=logging.getLogger("mininex.net_fee_obligations")
_PAGE_LOCK=Lock()
_OFFSET=0

def obligation_evidence(row):
    if (row.get("net_verified") is not True or row.get("payment_authorized") is not False
            or row["family"] not in ("ETHEREUM","WRAPPED_SPOT","CROSS_EVM")
            or row["asset"] not in COIN_NETWORKS or row["stable"] not in ("USDC","USDT")):
        return None
    amount=row["fee_due_stable_atomic"]
    if type(amount) is not str or not amount.isascii() or not amount.isdecimal() or not 0<int(amount)<2**256:
        return None
    # Commit the exact rational net, the lot allocation and original fill.
    evidence={"version":1,"chain_id":1,"asset":row["asset"],"family":row["family"],
        "fill_key":row["fill_key"],"coin_wallet":row["coin_wallet"],"stable":row["stable"],
        "fee_rate":"0.05","fee_due_stable_atomic":amount,"net_usd_ratio":row["net_usd_ratio"],
        "consumed_buy_lots":row["consumed_buy_lots"]}
    digest=hashlib.sha256(json.dumps(evidence,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode("ascii")).hexdigest()
    return evidence,digest

def _legacy_fee_exists(cur,did,row):
    # Old EVM ledger records the original source hash and original intent.
    # Old BTC/ETH obligations have no reliable source hash. Fail closed for
    # such historic accounts until those old fee events are reconciled.
    cur.execute("""SELECT 1 FROM mininex_evm_fee_events e
        WHERE e.privy_did=%s AND e.asset=%s AND e.fee_assessed_usd>0
          AND (e.source_tx_hash=%s OR EXISTS(
                SELECT 1 FROM mininex_live_evm_verified_fills f
                WHERE f.privy_did=e.privy_did AND f.asset=e.asset
                  AND f.intent_key=e.execution_id AND f.tx_hash=%s))
        UNION ALL SELECT 1 FROM mininex_fee_events
        WHERE privy_did=%s AND asset=%s AND fee_assessed_usd>0
        LIMIT 1""",(did,row["asset"],row["fill_key"],row["fill_key"],did,row["asset"]))
    return cur.fetchone() is not None

def book_owner_net_fees(did,asset):
    did=_safe_did(did)
    if asset not in COIN_NETWORKS:
        raise ValueError("Unknown original fee asset")
    db=_db_url();_ensure_schema(db)
    booked=0
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT privy_did FROM mininex_accounts WHERE privy_did=%s FOR UPDATE",(did,))
            if not cur.fetchone():
                return 0
            cur.execute("WITH fills AS ("+NET_FILLS_SQL+") SELECT * FROM fills WHERE asset=%(asset)s ORDER BY settled_at,fill_key",{"did":did,"asset":asset})
            rows=hydrate_net_rows(cur.fetchall())
            for row in calculate_realized_net(rows):
                result=obligation_evidence(row)
                if result is None or _legacy_fee_exists(cur,did,row):
                    continue
                evidence,digest=result
                cur.execute("""INSERT INTO mininex_net_fee_obligations
                    (privy_did,asset,family,fill_key,stable,amount_atomic,evidence_sha256,evidence)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (privy_did,asset,family,fill_key) DO NOTHING RETURNING fill_key""",
                    (did,asset,row["family"],row["fill_key"],row["stable"],
                     int(row["fee_due_stable_atomic"]),digest,Jsonb(evidence)))
                if cur.fetchone():
                    booked+=1
                else:
                    cur.execute("""SELECT evidence_sha256 FROM mininex_net_fee_obligations
                        WHERE privy_did=%s AND asset=%s AND family=%s AND fill_key=%s""",
                        (did,asset,row["family"],row["fill_key"]))
                    saved=cur.fetchone()
                    if not saved or saved["evidence_sha256"]!=digest:
                        raise ValueError("Original booked fee evidence changed; reconcile before payment")
    return booked

def pending_net_fee_owners(limit=20):
    global _OFFSET
    db=_db_url();_ensure_schema(db)
    sql="""SELECT x.privy_did,x.asset FROM (
        SELECT v.privy_did,v.asset FROM mininex_ethereum_net_valuations v
          WHERE v.evidence->>'side'='SELL'
        UNION SELECT v.privy_did,v.asset FROM mininex_staged_net_valuations v
          WHERE v.evidence->>'side'='SELL'
        ) x ORDER BY x.privy_did,x.asset LIMIT %s OFFSET %s"""
    with _PAGE_LOCK:
        with psycopg.connect(db,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql,(limit,_OFFSET));rows=cur.fetchall()
                if not rows and _OFFSET:
                    _OFFSET=0;cur.execute(sql,(limit,0));rows=cur.fetchall()
        _OFFSET+=len(rows)
    return rows

def reconcile_net_fee_obligations_once():
    for row in pending_net_fee_owners():
        try:
            book_owner_net_fees(row["privy_did"],row["asset"])
        except Exception:
            LOG.warning("Original net fee booking deferred for reconciliation")
