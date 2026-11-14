"""Durable MiniNex account and HWM ledger on PostgreSQL.

Never interpolate a DID or event ID into SQL; all user identifiers are bound
parameters and authenticated by Privy upstream. No public fee write API exists.
Neither a wallet balance nor ownership nor trade permission is inferred here.
"""
from __future__ import annotations

import logging
import os
from decimal import Decimal
from threading import Lock

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .fees import RATE, assess_realized_gain, decimal_amount

LOG = logging.getLogger("mininex.storage")
_BOOTSTRAPPED = False
_BOOTSTRAP_LOCK = Lock()
ASSETS = ("BTC", "ETH")

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS mininex_accounts (
        privy_did TEXT PRIMARY KEY,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS mininex_asset_hwm (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        asset TEXT NOT NULL CHECK (asset IN ('BTC','ETH')),
        cumulative_realized_net_usd NUMERIC(36,12) NOT NULL DEFAULT 0,
        high_water_mark_usd NUMERIC(36,12) NOT NULL DEFAULT 0
            CHECK (high_water_mark_usd >= 0),
        total_fees_assessed_usd NUMERIC(36,12) NOT NULL DEFAULT 0
            CHECK (total_fees_assessed_usd >= 0),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (privy_did,asset)
    )""",
    """CREATE TABLE IF NOT EXISTS mininex_budget_preferences (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        asset TEXT NOT NULL CHECK (asset IN ('BTC','ETH')),
        percent SMALLINT NOT NULL DEFAULT 0 CHECK (percent BETWEEN 0 AND 100),
        revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (privy_did,asset)
    )""",
    """CREATE TABLE IF NOT EXISTS mininex_live_eth_limits (
        privy_did TEXT PRIMARY KEY REFERENCES mininex_accounts(privy_did),
        planned_capital_usd NUMERIC(28,6) NOT NULL DEFAULT 0
            CHECK (planned_capital_usd >= 0 AND planned_capital_usd < 10000000),
        max_order_usd NUMERIC(28,6) NOT NULL DEFAULT 0
            CHECK (max_order_usd >= 0 AND max_order_usd < 10000000),
        max_daily_turnover_usd NUMERIC(28,6) NOT NULL DEFAULT 0
            CHECK (max_daily_turnover_usd >= 0 AND max_daily_turnover_usd < 10000000),
        trading_paused BOOLEAN NOT NULL DEFAULT TRUE,
        revision BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CHECK (max_order_usd <= planned_capital_usd),
        CHECK (max_order_usd <= max_daily_turnover_usd),
        CHECK ((planned_capital_usd = 0 AND max_order_usd = 0 AND
                max_daily_turnover_usd = 0) OR
               (planned_capital_usd > 0 AND max_order_usd > 0 AND
                max_daily_turnover_usd > 0))
    )""",
    # A signed EIP-191 enrollment intent is NOT Privy wallet authority.
    # One-time pending challenge, independently revocable and wallet-bound.
    """CREATE TABLE IF NOT EXISTS mininex_live_eth_consent_intents (
        privy_did TEXT PRIMARY KEY REFERENCES mininex_accounts(privy_did),
        wallet_address TEXT NOT NULL
            CHECK (wallet_address ~ '^0x[0-9a-f]{40}$'),
        session_sha256 TEXT NOT NULL
            CHECK (session_sha256 ~ '^[0-9a-f]{64}$'),
        limits_revision BIGINT NOT NULL CHECK (limits_revision >= 0),
        policy_owner_id TEXT NOT NULL,
        execution_signer_id TEXT NOT NULL,
        message_text TEXT NOT NULL CHECK (length(message_text) <= 3500),
        message_sha256 TEXT NOT NULL
            CHECK (message_sha256 ~ '^[0-9a-f]{64}$'),
        status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (status IN ('PENDING','SIGNED_INTENT','REVOKED')),
        issued_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        confirmed_at TIMESTAMPTZ,
        revoked_at TIMESTAMPTZ,
        CHECK (expires_at > issued_at),
        CHECK (expires_at <= issued_at + interval '5 minutes'),
        CHECK (policy_owner_id <> execution_signer_id)
    )""",
    # Stage 21: durable per-account pending Privy policy lifecycle.
    # Never conflate a policy record with wallet delegation or trade rights.
    """CREATE TABLE IF NOT EXISTS mininex_live_eth_policy_enrollments (
        privy_did TEXT PRIMARY KEY REFERENCES mininex_accounts(privy_did),
        wallet_address TEXT NOT NULL
            CHECK (wallet_address ~ '^0x[0-9a-f]{40}$'),
        limits_revision BIGINT NOT NULL CHECK (limits_revision >= 0),
        policy_owner_id TEXT NOT NULL CHECK (length(policy_owner_id) BETWEEN 6 AND 160),
        execution_signer_id TEXT NOT NULL
            CHECK (length(execution_signer_id) BETWEEN 6 AND 160),
        candidate_sha256 TEXT NOT NULL
            CHECK (candidate_sha256 ~ '^[0-9a-f]{64}$'),
        idempotency_key TEXT NOT NULL UNIQUE
            CHECK (idempotency_key ~ '^[0-9a-f]{64}$'),
        candidate_json JSONB NOT NULL,
        policy_expires_at TIMESTAMPTZ NOT NULL,
        status TEXT NOT NULL DEFAULT 'PREPARED'
            CHECK (status IN
                ('PREPARED','SUBMITTING','UNCERTAIN',
                 'REMOTE_VERIFIED_UNATTACHED','GRANT_VERIFIED',
                 'REVOKE_REQUESTED','REVOKED')),
        policy_id TEXT UNIQUE,
        remote_policy_sha256 TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_attempt_at TIMESTAMPTZ,
        remote_verified_at TIMESTAMPTZ,
        revoked_at TIMESTAMPTZ,
        CHECK (policy_owner_id <> execution_signer_id),
        CHECK (policy_id IS NULL OR policy_id ~ '^[A-Za-z0-9_-]{6,160}$'),
        CHECK (remote_policy_sha256 IS NULL OR
               remote_policy_sha256 ~ '^[0-9a-f]{64}$'),
        CHECK (status NOT IN ('REMOTE_VERIFIED_UNATTACHED','GRANT_VERIFIED') OR
               (policy_id IS NOT NULL AND remote_policy_sha256 IS NOT NULL
                AND remote_verified_at IS NOT NULL)),
        CHECK (status NOT IN ('SUBMITTING','UNCERTAIN') OR
               last_attempt_at IS NOT NULL),
        CHECK (status <> 'REVOKED' OR revoked_at IS NOT NULL)
    )""",
    # Existing installations have the original Stage-21 status CHECK. A
    # CREATE TABLE IF NOT EXISTS cannot extend that original constraint.
    # Replace only that non-destructive CHECK to add GRANT_VERIFIED safely.
    """ALTER TABLE mininex_live_eth_policy_enrollments
       DROP CONSTRAINT IF EXISTS mininex_live_eth_policy_enrollments_status_check""",
    """ALTER TABLE mininex_live_eth_policy_enrollments
       ADD CONSTRAINT mininex_live_eth_policy_enrollments_status_check
       CHECK (status IN (
           'PREPARED','SUBMITTING','UNCERTAIN',
           'REMOTE_VERIFIED_UNATTACHED','GRANT_VERIFIED',
           'REVOKE_REQUESTED','REVOKED'))""",
    """DO $$
       BEGIN
         IF NOT EXISTS (
           SELECT 1 FROM pg_constraint
           WHERE conname='mininex_live_eth_policy_grant_integrity_check'
         ) THEN
           ALTER TABLE mininex_live_eth_policy_enrollments
             ADD CONSTRAINT mininex_live_eth_policy_grant_integrity_check
             CHECK (status <> 'GRANT_VERIFIED' OR
               (policy_id IS NOT NULL AND remote_policy_sha256 IS NOT NULL
                AND remote_verified_at IS NOT NULL));
         END IF;
       END $$""",
    """CREATE TABLE IF NOT EXISTS mininex_live_order_intents (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        asset TEXT NOT NULL CHECK (asset IN ('BTC','ETH')),
        chain_network TEXT NOT NULL CHECK (chain_network IN ('bitcoin-mainnet','ethereum-mainnet')),
        intent_key TEXT NOT NULL CHECK (length(intent_key) BETWEEN 8 AND 96),
        side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
        stable TEXT NOT NULL CHECK (stable IN ('USDC','USDT')),
        state TEXT NOT NULL DEFAULT 'RESERVED'
            CHECK (state IN ('RESERVED','SENDING','RECONCILIATION_REQUIRED',
                             'SUBMITTED','CONFIRMED','REVERTED','REORG_PENDING',
                             'CANCELLED','EXPIRED')),
        reserved_notional_usd NUMERIC(28,6) NOT NULL
            CHECK (reserved_notional_usd > 0 AND reserved_notional_usd < 10000000),
        tx_hash TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        submitted_at TIMESTAMPTZ,
        finalized_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (privy_did,asset,intent_key),
        CHECK ((asset='BTC' AND chain_network='bitcoin-mainnet') OR
               (asset='ETH' AND chain_network='ethereum-mainnet')),
        CHECK (tx_hash IS NULL OR
               (length(tx_hash) BETWEEN 64 AND 66)),
        CHECK ((state NOT IN ('SUBMITTED','CONFIRMED','REVERTED','REORG_PENDING'))
               OR tx_hash IS NOT NULL),
        CHECK (state NOT IN ('CONFIRMED','REVERTED','CANCELLED','EXPIRED')
               OR finalized_at IS NOT NULL)
    )""",
    """CREATE INDEX IF NOT EXISTS mininex_live_orders_by_account
       ON mininex_live_order_intents(privy_did,asset,created_at DESC)""",
    # Future executor must atomically bind the immutable signal to an order.
    """CREATE TABLE IF NOT EXISTS mininex_live_signal_bindings (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        asset TEXT NOT NULL CHECK (asset IN ('BTC','ETH')),
        signal_digest TEXT NOT NULL CHECK (length(signal_digest)=64 AND signal_digest ~ '^[0-9a-f]+$'),
        intent_key TEXT NOT NULL CHECK (length(intent_key) BETWEEN 8 AND 96),
        payload_digest TEXT NOT NULL CHECK (length(payload_digest)=64 AND payload_digest ~ '^[0-9a-f]+$'),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (privy_did,asset,signal_digest),
        UNIQUE (privy_did,asset,intent_key),
        FOREIGN KEY (privy_did,asset,intent_key)
            REFERENCES mininex_live_order_intents(privy_did,asset,intent_key)
    )""",
    # One shared strategy template for all eleven tradable coins.
    # Each coin keeps independent budget, positions, fills, stablecoin and HWM.
    # ADA stays excluded until its wallet and trading route are implemented.
    """CREATE TABLE IF NOT EXISTS mininex_unified_strategy_profiles (
        privy_did TEXT PRIMARY KEY REFERENCES mininex_accounts(privy_did),
        mode TEXT NOT NULL DEFAULT 'LONG'
            CHECK (mode IN ('LONG','SPOT_SHORT')),
        horizon TEXT NOT NULL DEFAULT '15m'
            CHECK (horizon IN ('5m','15m','1h','24h')),
        revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS mininex_trade_preferences (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        asset TEXT NOT NULL CHECK (asset IN ('BTC','ETH')),
        mode TEXT NOT NULL DEFAULT 'LONG' CHECK (mode IN ('LONG','SPOT_SHORT')),
        stable TEXT NOT NULL DEFAULT 'USDC' CHECK (stable IN ('USDC','USDT')),
        horizon TEXT NOT NULL DEFAULT '15m' CHECK (horizon IN ('5m','15m','1h','24h')),
        revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (privy_did,asset)
    )""",
    """CREATE TABLE IF NOT EXISTS mininex_verified_activity (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        source TEXT NOT NULL CHECK (source IN ('BLOCKCHAIN','MININEX_TRADE')),
        record_id TEXT NOT NULL,
        asset TEXT NOT NULL CHECK (asset IN ('BTC','ETH','USDC','USDT')),
        activity_type TEXT NOT NULL CHECK (activity_type IN ('DEPOSIT','WITHDRAWAL','BUY','SELL','FEE','TRANSFER')),
        occurred_at TIMESTAMPTZ NOT NULL,
        quantity NUMERIC(36,18) NOT NULL CHECK (quantity >= 0),
        amount_usd NUMERIC(36,12),
        fee_usd NUMERIC(36,12),
        realized_net_usd NUMERIC(36,12),
        tx_hash TEXT,
        network TEXT,
        verification TEXT NOT NULL CHECK (verification='CONFIRMED'),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (privy_did,source,record_id),
        CHECK (amount_usd IS NULL OR amount_usd >= 0),
        CHECK (fee_usd IS NULL OR fee_usd >= 0)
    )""",
    """CREATE INDEX IF NOT EXISTS mininex_activity_by_user_year
       ON mininex_verified_activity(privy_did,occurred_at DESC)""",
    """CREATE TABLE IF NOT EXISTS mininex_user_paper_sessions (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        asset TEXT NOT NULL CHECK (asset IN ('BTC','ETH')),
        status TEXT NOT NULL DEFAULT 'NOT_STARTED'
            CHECK (status IN ('NOT_STARTED','ACTIVE','PAUSED')),
        settlement TEXT NOT NULL DEFAULT 'USDC'
            CHECK (settlement IN ('USDC','USDT')),
        strategy_mode TEXT NOT NULL DEFAULT 'LONG'
            CHECK (strategy_mode IN ('LONG','SPOT_SHORT')),
        horizon TEXT NOT NULL DEFAULT '15m'
            CHECK (horizon IN ('5m','15m','1h','24h')),
        budget_percent SMALLINT NOT NULL DEFAULT 0
            CHECK (budget_percent BETWEEN 0 AND 100),
        initial_demo_cash NUMERIC(28,8) NOT NULL DEFAULT 0
            CHECK (initial_demo_cash >= 0),
        state_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        revision BIGINT NOT NULL DEFAULT 0,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (privy_did,asset)
    )""",
    """CREATE TABLE IF NOT EXISTS mininex_user_paper_trades (
        privy_did TEXT NOT NULL,
        asset TEXT NOT NULL,
        event_id BIGINT NOT NULL,
        occurred_at TIMESTAMPTZ NOT NULL,
        event_json JSONB NOT NULL,
        PRIMARY KEY (privy_did,asset,event_id),
        FOREIGN KEY (privy_did,asset) REFERENCES
            mininex_user_paper_sessions(privy_did,asset)
    )""",
    """CREATE INDEX IF NOT EXISTS mininex_user_paper_trades_by_owner
       ON mininex_user_paper_trades(privy_did,asset,occurred_at DESC)""",
    # Permanent individual setup for the nine additional coins. This is NOT
    # shared across coins and does not modify a running PAPER or LIVE session.
    """CREATE TABLE IF NOT EXISTS mininex_extra_coin_preferences (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        asset TEXT NOT NULL CHECK (asset IN
            ('BNB','SOL','XRP','TRX','POL','AVAX','LINK','DOGE','UNI')),
        budget_usd INTEGER NOT NULL DEFAULT 500
            CHECK (budget_usd BETWEEN 100 AND 5000),
        mode TEXT NOT NULL DEFAULT 'LONG'
            CHECK (mode IN ('LONG','SPOT_SHORT','AUTO')),
        stable TEXT NOT NULL DEFAULT 'USDC'
            CHECK (stable IN ('USDC','USDT')),
        horizon TEXT NOT NULL DEFAULT '15m'
            CHECK (horizon IN ('5m','15m','1h','24h')),
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (privy_did,asset)
    )""",
    # Independent virtual accounts for nine extra coins. BTC/ETH schemas and
    # real HWM/fee/live ledgers remain untouched. ADA stays unavailable.
    """CREATE TABLE IF NOT EXISTS mininex_extra_paper_sessions (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        asset TEXT NOT NULL CHECK (asset IN
            ('BNB','SOL','XRP','TRX','POL','AVAX','LINK','DOGE','UNI')),
        status TEXT NOT NULL DEFAULT 'ACTIVE'
            CHECK (status IN ('ACTIVE','PAUSED')),
        settlement TEXT NOT NULL CHECK (settlement IN ('USDC','USDT')),
        strategy_mode TEXT NOT NULL CHECK (strategy_mode IN ('LONG','SPOT_SHORT')),
        horizon TEXT NOT NULL CHECK (horizon IN ('5m','15m','1h','24h')),
        initial_demo_cash NUMERIC(28,8) NOT NULL
            CHECK (initial_demo_cash BETWEEN 100 AND 5000),
        state_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        revision BIGINT NOT NULL DEFAULT 0,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (privy_did,asset)
    )""",
    """CREATE INDEX IF NOT EXISTS mininex_extra_paper_next
       ON mininex_extra_paper_sessions(status,updated_at)""",
    """CREATE TABLE IF NOT EXISTS mininex_extra_paper_trades (
        privy_did TEXT NOT NULL,
        asset TEXT NOT NULL,
        event_id BIGINT NOT NULL CHECK (event_id > 0),
        occurred_at TIMESTAMPTZ NOT NULL,
        event_json JSONB NOT NULL,
        PRIMARY KEY (privy_did,asset,event_id),
        FOREIGN KEY (privy_did,asset)
            REFERENCES mininex_extra_paper_sessions(privy_did,asset)
    )""",
    """CREATE INDEX IF NOT EXISTS mininex_extra_paper_trade_history
       ON mininex_extra_paper_trades(privy_did,asset,occurred_at DESC)""",
    """CREATE TABLE IF NOT EXISTS mininex_fee_events (
        privy_did TEXT NOT NULL,
        asset TEXT NOT NULL,
        execution_id TEXT NOT NULL,
        realized_net_delta_usd NUMERIC(36,12) NOT NULL,
        cumulative_realized_net_usd NUMERIC(36,12) NOT NULL,
        high_water_mark_usd NUMERIC(36,12) NOT NULL,
        fee_rate NUMERIC(10,8) NOT NULL DEFAULT 0.05 CHECK (fee_rate = 0.05),
        fee_assessed_usd NUMERIC(36,12) NOT NULL DEFAULT 0
            CHECK (fee_assessed_usd >= 0),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (privy_did,asset,execution_id),
        FOREIGN KEY (privy_did,asset)
            REFERENCES mininex_asset_hwm(privy_did,asset)
    )""",
    """CREATE TABLE IF NOT EXISTS mininex_treasury_obligations (
        privy_did TEXT NOT NULL,
        asset TEXT NOT NULL CHECK (asset IN ('BTC','ETH')),
        execution_id TEXT NOT NULL,
        fee_due_usd NUMERIC(36,12) NOT NULL CHECK (fee_due_usd > 0),
        quote_currency TEXT NOT NULL DEFAULT 'USD' CHECK (quote_currency='USD'),
        destination_network TEXT NOT NULL DEFAULT 'ethereum'
            CHECK (destination_network='ethereum'),
        settlement_token TEXT
            CHECK (settlement_token IN ('USDC','USDT')),
        status TEXT NOT NULL DEFAULT 'AWAITING_VERIFIED_LIVE_SETTLEMENT'
            CHECK (status='AWAITING_VERIFIED_LIVE_SETTLEMENT'),
        settled_token_amount NUMERIC(36,18),
        settlement_tx_hash TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (privy_did,asset,execution_id),
        FOREIGN KEY (privy_did,asset,execution_id)
            REFERENCES mininex_fee_events(privy_did,asset,execution_id),
        CHECK (settled_token_amount IS NULL AND settlement_tx_hash IS NULL)
    )""",
    """CREATE INDEX IF NOT EXISTS mininex_treasury_obligations_pending
       ON mininex_treasury_obligations(status,created_at)""",
    # Existing installations also need an upgrade: CREATE TABLE IF NOT EXISTS
    # cannot change a previous LONG/SPOT_SHORT check. Each ALTER is atomic.
    # Keeps all previous sessions/positions/fills/budgets intact.
    """ALTER TABLE mininex_unified_strategy_profiles
       DROP CONSTRAINT IF EXISTS mininex_unified_strategy_profiles_mode_check,
       ADD CONSTRAINT mininex_unified_strategy_profiles_mode_check
       CHECK (mode IN ('LONG','SPOT_SHORT','AUTO'))""",
    """ALTER TABLE mininex_trade_preferences
       DROP CONSTRAINT IF EXISTS mininex_trade_preferences_mode_check,
       ADD CONSTRAINT mininex_trade_preferences_mode_check
       CHECK (mode IN ('LONG','SPOT_SHORT','AUTO'))""",
    """ALTER TABLE mininex_user_paper_sessions
       DROP CONSTRAINT IF EXISTS mininex_user_paper_sessions_strategy_mode_check,
       ADD CONSTRAINT mininex_user_paper_sessions_strategy_mode_check
       CHECK (strategy_mode IN ('LONG','SPOT_SHORT','AUTO'))""",
    """ALTER TABLE mininex_extra_paper_sessions
       DROP CONSTRAINT IF EXISTS mininex_extra_paper_sessions_strategy_mode_check,
       ADD CONSTRAINT mininex_extra_paper_sessions_strategy_mode_check
       CHECK (strategy_mode IN ('LONG','SPOT_SHORT','AUTO'))""",

)


def _db_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise HTTPException(503, "MiniNex persistent database not configured")
    if not url.startswith(("postgresql://", "postgres://")):
        raise HTTPException(503, "MiniNex persistent database URL invalid")
    return url


def _ensure_schema(url: str) -> None:
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    with _BOOTSTRAP_LOCK:
        if _BOOTSTRAPPED:
            return
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor() as cur:
                for statement in SCHEMA:
                    cur.execute(statement)
        _BOOTSTRAPPED = True


def _safe_did(privy_did: str) -> str:
    if not isinstance(privy_did, str) or not privy_did.startswith("did:privy:") or len(privy_did) > 256:
        raise ValueError("verified Privy DID required")
    return privy_did


def _prepare_rows(cur, did: str) -> None:
    cur.execute(
        """INSERT INTO mininex_accounts(privy_did) VALUES (%s)
           ON CONFLICT (privy_did) DO UPDATE SET last_seen_at=now()""",
        (did,),
    )
    for asset in ASSETS:
        cur.execute(
            """INSERT INTO mininex_asset_hwm(privy_did,asset) VALUES (%s,%s)
               ON CONFLICT (privy_did,asset) DO NOTHING""",
            (did,asset),
        )

    # Budget preferences are inert settings, not wallet permissions.
    for asset in ASSETS:
        cur.execute(
            """INSERT INTO mininex_budget_preferences(privy_did,asset,percent)
               VALUES (%s,%s,0) ON CONFLICT (privy_did,asset) DO NOTHING""",
            (did,asset),
        )
    # Independent LIVE limits are OFF by default. The paper slider never
    # authorizes real transfers, even when set to 100%.
    cur.execute(
        """INSERT INTO mininex_live_eth_limits(privy_did)
           VALUES (%s) ON CONFLICT (privy_did) DO NOTHING""",
        (did,),
    )
    # Initialize a single cross-coin strategy template, without touching
    # any already running session or existing per-coin allocation.
    cur.execute(
        """INSERT INTO mininex_unified_strategy_profiles(privy_did)
           VALUES (%s) ON CONFLICT (privy_did) DO NOTHING""",
        (did,),
    )
    # Strategy settings are user preferences, never trading authorization.
    for asset in ASSETS:
        cur.execute(
            """INSERT INTO mininex_trade_preferences(privy_did,asset)
               VALUES (%s,%s) ON CONFLICT (privy_did,asset) DO NOTHING""",
            (did,asset),
        )


def get_account_summary(privy_did: str) -> dict:
    """Create/read a private account only after successful JWT verification."""
    did = _safe_did(privy_did)
    url = _db_url()
    try:
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                cur.execute(
                    """SELECT asset, cumulative_realized_net_usd,high_water_mark_usd,
                              total_fees_assessed_usd
                       FROM mininex_asset_hwm WHERE privy_did=%s ORDER BY asset""",
                    (did,),
                )
                ledgers = list(cur.fetchall())
    except psycopg.Error:
        LOG.warning("MiniNex database unavailable (error details withheld)")
        raise HTTPException(503, "MiniNex database currently unavailable") from None

    return {
        "authenticated": True,
        "account_persistent": True,
        "mode": "PAPER_ONLY_NO_LIVE_ORDERS",
        "wallet_connected": False,
        "trading_enabled": False,
        "performance_fee_pct": "5.00",
        "hwm_mode": "REALIZED_NET_USD_PREPARED_NOT_COLLECTED",
        "ledger": {
            row["asset"]: {
                "cumulative_realized_net_usd": str(row["cumulative_realized_net_usd"]),
                "high_water_mark_usd": str(row["high_water_mark_usd"]),
                "total_fees_assessed_usd": str(row["total_fees_assessed_usd"]),
            }
            for row in ledgers
        },
    }


def record_trusted_realized_close(
    *, privy_did: str, asset: str, execution_id: str, realized_net_usd: object
) -> dict:
    """Server-INTERNAL accounting only. NOT called by current paper executor.

    Execute only after an independently confirmed, immutable live trade fill and
    correct cost basis are reconciled. Actual fee collection is not implemented.
    A repeated execution ID is idempotent even under concurrent workers.
    """
    did = _safe_did(privy_did)
    if asset not in ASSETS:
        raise ValueError("asset must be BTC or ETH")
    if not isinstance(execution_id, str) or not 1 <= len(execution_id) <= 180:
        raise ValueError("invalid execution id")
    if any(c.isspace() for c in execution_id):
        raise ValueError("execution id cannot contain whitespace")
    delta = decimal_amount(realized_net_usd)
    url = _db_url()
    _ensure_schema(url)
    with psycopg.connect(url, connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            _prepare_rows(cur,did)
            cur.execute(
                """SELECT cumulative_realized_net_usd,high_water_mark_usd
                   FROM mininex_asset_hwm WHERE privy_did=%s AND asset=%s FOR UPDATE""",
                (did,asset),
            )
            row = cur.fetchone()
            cur.execute(
                """SELECT fee_assessed_usd,cumulative_realized_net_usd,high_water_mark_usd
                   FROM mininex_fee_events
                   WHERE privy_did=%s AND asset=%s AND execution_id=%s""",
                (did,asset,execution_id),
            )
            existing=cur.fetchone()
            if existing:
                return {"duplicate":True,"fee_assessed_usd":str(existing["fee_assessed_usd"]),
                        "high_water_mark_usd":str(existing["high_water_mark_usd"]),
                        "cumulative_realized_net_usd":str(existing["cumulative_realized_net_usd"])}
            assessment=assess_realized_gain(
                row["cumulative_realized_net_usd"],row["high_water_mark_usd"],delta,
            )
            cur.execute(
                """INSERT INTO mininex_fee_events
                   (privy_did,asset,execution_id,realized_net_delta_usd,
                    cumulative_realized_net_usd,high_water_mark_usd,fee_rate,fee_assessed_usd)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (did,asset,execution_id,delta,assessment.cumulative_usd,
                 assessment.high_water_mark_usd,RATE,assessment.fee_assessed_usd),
            )
            cur.execute(
                """UPDATE mininex_asset_hwm
                   SET cumulative_realized_net_usd=%s,high_water_mark_usd=%s,
                       total_fees_assessed_usd=total_fees_assessed_usd+%s,
                       updated_at=now()
                   WHERE privy_did=%s AND asset=%s""",
                (assessment.cumulative_usd,assessment.high_water_mark_usd,
                 assessment.fee_assessed_usd,did,asset),
            )
            if assessment.fee_assessed_usd>0:
                # This is a USD-denominated ACCOUNTING obligation, not an
                # ERC20 transfer instruction. No paper event can reach here.
                cur.execute(
                    """INSERT INTO mininex_treasury_obligations
                       (privy_did,asset,execution_id,fee_due_usd)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT (privy_did,asset,execution_id) DO NOTHING""",
                    (did,asset,execution_id,assessment.fee_assessed_usd),
                )
    return {"duplicate":False,"fee_assessed_usd":str(assessment.fee_assessed_usd),
            "high_water_mark_usd":str(assessment.high_water_mark_usd),
            "cumulative_realized_net_usd":str(assessment.cumulative_usd)}
