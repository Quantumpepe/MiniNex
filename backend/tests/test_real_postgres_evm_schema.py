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
    "mininex_evm_immutable_fill_usd_prices",
    "mininex_evm_asset_hwm",
    "mininex_evm_fee_events",
    "mininex_evm_fee_obligations",
    "mininex_evm_verified_fee_payments",
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


@pytest.mark.skipif(not _ci_dsn(),reason="Only ephemeral CI PostgreSQL may run")
def test_real_postgres_fee_payment_requires_one_original_fee_and_one_real_hash():
    """One owner+asset+HWM close can be credited once; tx hash cannot be reused."""
    did="did:privy:fee_ci_"+uuid.uuid4().hex
    first="sig_"+uuid.uuid4().hex
    second="sig_"+uuid.uuid4().hex
    wallet="0x"+"ab"*20
    treasury="0x"+"cd"*20
    token="0x"+"ef"*20
    tx="0x"+"12"*32
    with psycopg.connect(_ci_dsn(),connect_timeout=6) as conn:
        with conn.cursor() as cur:
            for stmt in SCHEMA:
                cur.execute(stmt)
            cur.execute("INSERT INTO mininex_accounts(privy_did) VALUES (%s)",(did,))
            cur.execute(
                "INSERT INTO mininex_evm_asset_hwm(privy_did,asset) VALUES (%s,'LINK')",
                (did,))
            for key in (first,second):
                cur.execute(
                    """INSERT INTO mininex_evm_fee_events
                       (privy_did,asset,execution_id,realized_net_delta_usd,
                        cumulative_realized_net_usd,high_water_mark_usd,
                        fee_rate,fee_assessed_usd,source_tx_hash)
                       VALUES (%s,'LINK',%s,50,50,50,0.05,2.5,%s)""",
                    (did,key,"0x"+"34"*32))
                cur.execute(
                    """INSERT INTO mininex_evm_fee_obligations
                       (privy_did,asset,execution_id,fee_due_usd)
                       VALUES (%s,'LINK',%s,2.5)""",(did,key))
            stmt="""INSERT INTO mininex_evm_verified_fee_payments
                     (privy_did,asset,execution_id,chain_id,stable,
                      stable_token_address,owner_wallet,treasury_wallet,
                      fee_due_usd,stable_usd,token_amount_atomic)
                     VALUES (%s,'LINK',%s,1,'USDC',%s,%s,%s,2.5,1,2500000)"""
            cur.execute(stmt,(did,first,token,wallet,treasury))
            cur.execute(
                """UPDATE mininex_evm_verified_fee_payments
                   SET state='PAID_VERIFIED',tx_hash=%s,paid_at=now()
                   WHERE privy_did=%s AND asset='LINK' AND execution_id=%s""",
                (tx,did,first))
            with pytest.raises(psycopg.errors.UniqueViolation):
                with conn.transaction():
                    cur.execute(stmt,(did,first,token,wallet,treasury))
            cur.execute(stmt,(did,second,token,wallet,treasury))
            with pytest.raises(psycopg.errors.UniqueViolation):
                with conn.transaction():
                    cur.execute(
                        """UPDATE mininex_evm_verified_fee_payments
                           SET state='PAID_VERIFIED',tx_hash=%s,paid_at=now()
                           WHERE privy_did=%s AND asset='LINK' AND execution_id=%s""",
                        (tx,did,second))
            cur.execute(
                """SELECT COUNT(*) FROM mininex_evm_verified_fee_payments
                   WHERE privy_did=%s AND state='PAID_VERIFIED'""",(did,))
            assert cur.fetchone()[0]==1
            cur.execute(
                """SELECT state,tx_hash FROM mininex_evm_verified_fee_payments
                   WHERE privy_did=%s AND asset='LINK' AND execution_id=%s""",
                (did,second))
            assert cur.fetchone()==("PREPARED",None)


@pytest.mark.skipif(not _ci_dsn(),reason="Only ephemeral CI PostgreSQL may run")
def test_existing_legacy_fee_constraint_migrates_without_duplicate_charges():
    """Old Render installs forbid non-null settlement hashes; migration must work."""
    from backend.storage import SCHEMA
    migration=next(sql for sql in SCHEMA
                   if "DROP CONSTRAINT IF EXISTS" in sql
                   and "mininex_evm_fee_obligations" in sql)
    did="did:privy:migration_"+uuid.uuid4().hex
    key="sig_"+uuid.uuid4().hex
    tx="0x"+"ab"*32
    with psycopg.connect(_ci_dsn(),connect_timeout=6) as conn:
        with conn.cursor() as cur:
            for sql in SCHEMA:
                cur.execute(sql)
            cur.execute(
                """ALTER TABLE mininex_evm_fee_obligations
                   DROP CONSTRAINT mininex_evm_fee_obligations_payment_state_check""")
            cur.execute(
                """ALTER TABLE mininex_evm_fee_obligations
                   ADD CONSTRAINT mininex_evm_fee_obligations_settlement_status_check
                   CHECK(settlement_status='AWAITING_VERIFIED_TRANSFER')""")
            cur.execute(
                """ALTER TABLE mininex_evm_fee_obligations
                   ADD CONSTRAINT mininex_evm_fee_obligations_settlement_tx_hash_check
                   CHECK(settlement_tx_hash IS NULL)""")
            cur.execute(migration)
            cur.execute(migration)
            cur.execute(
                """INSERT INTO mininex_accounts(privy_did) VALUES (%s)""",
                (did,))
            cur.execute(
                """INSERT INTO mininex_evm_asset_hwm(privy_did,asset)
                   VALUES (%s,'LINK')""",(did,))
            cur.execute(
                """INSERT INTO mininex_evm_fee_events
                   (privy_did,asset,execution_id,realized_net_delta_usd,
                    cumulative_realized_net_usd,high_water_mark_usd,
                    fee_rate,fee_assessed_usd,source_tx_hash)
                   VALUES (%s,'LINK',%s,20,20,20,0.05,1,%s)""",
                (did,key,"0x"+"cd"*32))
            cur.execute(
                """INSERT INTO mininex_evm_fee_obligations
                   (privy_did,asset,execution_id,fee_due_usd)
                   VALUES (%s,'LINK',%s,1)""",(did,key))
            cur.execute(
                """UPDATE mininex_evm_fee_obligations
                   SET settlement_status='PAID_VERIFIED',
                       settlement_tx_hash=%s
                   WHERE privy_did=%s AND asset='LINK' AND execution_id=%s""",
                (tx,did,key))
            cur.execute(
                """SELECT settlement_status,settlement_tx_hash
                   FROM mininex_evm_fee_obligations
                   WHERE privy_did=%s AND asset='LINK' AND execution_id=%s""",
                (did,key))
            assert cur.fetchone()==("PAID_VERIFIED",tx)


@pytest.mark.skipif(not _ci_dsn(),reason="Only ephemeral CI PostgreSQL may run")
def test_historical_usd_original_trade_proof_is_immutable_on_real_postgres():
    """An original BUY's block-time quote cannot be rewritten or removed."""
    did="did:privy:historical_ci_"+uuid.uuid4().hex
    key="sig_"+uuid.uuid4().hex
    tx="0x"+"dc"*32
    block_hash="0x"+"af"*32
    wallet="0x"+"ab"*20
    with psycopg.connect(_ci_dsn(),connect_timeout=6) as conn:
        with conn.cursor() as cur:
            for sql in SCHEMA:
                cur.execute(sql)
            for sql in SCHEMA:
                cur.execute(sql)
            cur.execute("INSERT INTO mininex_accounts(privy_did) VALUES (%s)",
                        (did,))
            cur.execute(
                """INSERT INTO mininex_live_evm_auto_broadcasts
                   (privy_did,asset,chain_id,intent_key,wallet_address,
                    wallet_id,request_digest,side,stable,status,tx_hash)
                   VALUES (%s,'LINK',1,%s,%s,'wallet_123456789',%s,
                           'BUY','USDC','FINALIZED_SUCCESS',%s)""",
                (did,key,wallet,"a"*64,tx))
            cur.execute(
                """INSERT INTO mininex_live_evm_verified_fills
                   (privy_did,asset,chain_id,intent_key,tx_hash,
                    wallet_address,side,stable,source_amount,
                    destination_amount,network_fee_native)
                   VALUES (%s,'LINK',1,%s,%s,%s,'BUY','USDC',100,2,0.001)""",
                (did,key,tx,wallet))
            original=(did,"LINK",1,tx,"USDC",800000,block_hash,1800000,
                      1800010,"1.000000000000000001",
                      "COINGECKO_MARKET_CHART_RANGE")
            insert="""INSERT INTO mininex_evm_immutable_fill_usd_prices
                       (privy_did,asset,chain_id,tx_hash,quoted_asset,
                        block_number,block_hash,block_unix,observed_unix,
                        usd_price,provider)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""
            cur.execute(insert,original)
            for query,args in (
                ("""UPDATE mininex_evm_immutable_fill_usd_prices
                    SET usd_price=0.98 WHERE privy_did=%s AND tx_hash=%s""",
                 (did,tx)),
                ("""DELETE FROM mininex_evm_immutable_fill_usd_prices
                    WHERE privy_did=%s AND tx_hash=%s""",(did,tx)),
            ):
                with pytest.raises(psycopg.errors.RaiseException):
                    with conn.transaction():
                        cur.execute(query,args)
            with pytest.raises(psycopg.errors.UniqueViolation):
                with conn.transaction():
                    cur.execute(insert,original)
            cur.execute(
                """SELECT usd_price,block_hash,block_unix
                   FROM mininex_evm_immutable_fill_usd_prices
                   WHERE privy_did=%s AND asset='LINK' AND tx_hash=%s""",
                (did,tx))
            value=cur.fetchone()
            assert str(value[0])=="1.000000000000000001"
            assert value[1:]==(block_hash,1800000)


@pytest.mark.skipif(not _ci_dsn(),reason="Only ephemeral CI PostgreSQL may run")
def test_legacy_twelve_coin_schema_migration_persists_real_arb_preferences():
    """An already-created 9-extra-coin database must accept ARB after upgrade.

    Real network permissions or onchain trades are never used.
    """
    did="did:privy:arb_migration_"+uuid.uuid4().hex
    with psycopg.connect(_ci_dsn(),connect_timeout=6) as conn:
        with conn.cursor() as cur:
            # Schema statements are designed to be safely repeated on startup.
            for sql in SCHEMA:
                cur.execute(sql)
            cur.execute("INSERT INTO mininex_accounts(privy_did) VALUES (%s)",(did,))
            cur.execute(
                """INSERT INTO mininex_extra_coin_preferences
                   (privy_did,asset,budget_usd,mode,stable,horizon)
                   VALUES (%s,'ARB',12500,'AUTO','USDC','1h')""",(did,))
            cur.execute(
                """INSERT INTO mininex_extra_paper_sessions
                   (privy_did,asset,settlement,strategy_mode,horizon,initial_demo_cash)
                   VALUES (%s,'ARB','USDC','LONG','1h',12500)""",(did,))
            cur.execute(
                """SELECT budget_usd,mode FROM mininex_extra_coin_preferences
                   WHERE privy_did=%s AND asset='ARB'""",(did,))
            assert cur.fetchone()==(12500,"AUTO")
            cur.execute(
                """SELECT initial_demo_cash FROM mininex_extra_paper_sessions
                   WHERE privy_did=%s AND asset='ARB'""",(did,))
            assert cur.fetchone()[0]==12500
            with pytest.raises(psycopg.errors.CheckViolation):
                with conn.transaction():
                    cur.execute(
                        """INSERT INTO mininex_extra_coin_preferences
                           (privy_did,asset) VALUES (%s,'ADA')""",(did,))
            # Existing ARB records survive a second migration pass unchanged.
            for sql in SCHEMA:
                cur.execute(sql)
            cur.execute(
                """SELECT COUNT(*) FROM mininex_extra_coin_preferences
                   WHERE privy_did=%s AND asset='ARB'""",(did,))
            assert cur.fetchone()[0]==1
