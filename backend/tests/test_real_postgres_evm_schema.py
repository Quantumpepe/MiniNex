"""Real ephemeral PostgreSQL schema and once-only claim integration tests.

A GitHub Actions PostgreSQL service is explicitly required. Normal tests skip
this file's database tests; it never reaches Render or user wallets.
"""
import os
import uuid

import psycopg
import pytest

from backend.storage import SCHEMA


REQUIRED=(
    "mininex_accounts",
    "mininex_live_evm_auto_sessions",
    "mininex_live_evm_owner_permissions",
    "mininex_live_evm_budget_reservations",
    "mininex_live_evm_auto_broadcasts",
    "mininex_live_evm_verified_fills",
    "mininex_live_evm_native_auto_steps",
    "mininex_live_evm_native_sell_intents",
    "mininex_live_evm_completed_native_cycles",
    "mininex_evm_asset_hwm",
    "mininex_evm_fee_events",
    "mininex_evm_fee_obligations",
)


def _ci_dsn():
    return os.getenv("MININEX_TEST_POSTGRES_DSN","").strip()


@pytest.mark.skipif(not _ci_dsn(),reason="Only ephemeral CI PostgreSQL may run")
def test_real_postgres_accepts_entire_schema_and_second_bootstrap():
    with psycopg.connect(_ci_dsn(),connect_timeout=6) as conn:
        with conn.cursor() as cur:
            for sql in SCHEMA:
                cur.execute(sql)
            for sql in SCHEMA:
                cur.execute(sql)
            cur.execute(
                """SELECT tablename FROM pg_catalog.pg_tables
                   WHERE schemaname='public'""")
            found={r[0] for r in cur.fetchall()}
    assert set(REQUIRED).issubset(found)


@pytest.mark.skipif(not _ci_dsn(),reason="Only ephemeral CI PostgreSQL may run")
def test_real_postgres_original_native_sell_cannot_be_claimed_twice():
    did="did:privy:ci_"+uuid.uuid4().hex
    key="sig_"+uuid.uuid4().hex
    wallet="0x"+"ab"*20
    sig="a"*64
    row=(did,"BNB",key,"owner_session_12345","market_event_12345",
         56,"USDT","wallet_123456789",wallet,1,10**18,sig)
    insert="""INSERT INTO mininex_live_evm_native_sell_intents
              (privy_did,asset,intent_key,strategy_run_id,signal_event_id,
               chain_id,stable,wallet_id,wallet_address,
               budget_revision,amount_wei,request_digest,status)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'SENDING')"""
    with psycopg.connect(_ci_dsn(),connect_timeout=6) as conn:
        with conn.cursor() as cur:
            for sql in SCHEMA:
                cur.execute(sql)
            cur.execute(
                "INSERT INTO mininex_accounts(privy_did) VALUES (%s)",(did,))
            cur.execute(insert,row)
            # The savepoint rolls back only the attempted duplicate; the
            # original immutable record stays within the outer transaction.
            with pytest.raises(psycopg.errors.UniqueViolation):
                with conn.transaction():
                    cur.execute(insert,row)
            cur.execute(
                """SELECT COUNT(*) FROM mininex_live_evm_native_sell_intents
                   WHERE privy_did=%s AND asset='BNB' AND intent_key=%s""",
                (did,key))
            assert cur.fetchone()[0]==1
