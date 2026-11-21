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
            CHECK (planned_capital_usd >= 0),
        max_order_usd NUMERIC(28,6) NOT NULL DEFAULT 0
            CHECK (max_order_usd >= 0),
        max_daily_turnover_usd NUMERIC(28,6) NOT NULL DEFAULT 0
            CHECK (max_daily_turnover_usd >= 0),
        trading_paused BOOLEAN NOT NULL DEFAULT TRUE,
        revision BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    # BTC native-UTXO LIVE caps are independent of ETH limits and PAPER budgets.
    # They never grant wallet authority; BTC remains paused until reviewed
    # native signing, cross-chain settlement and reconciliation are released.
    """CREATE TABLE IF NOT EXISTS mininex_live_btc_limits (
        privy_did TEXT PRIMARY KEY REFERENCES mininex_accounts(privy_did),
        planned_capital_usd NUMERIC(28,6) NOT NULL DEFAULT 0
            CHECK (planned_capital_usd >= 0),
        max_order_usd NUMERIC(28,6) NOT NULL DEFAULT 0
            CHECK (max_order_usd >= 0),
        max_daily_turnover_usd NUMERIC(28,6) NOT NULL DEFAULT 0
            CHECK (max_daily_turnover_usd >= 0),
        trading_paused BOOLEAN NOT NULL DEFAULT TRUE,
        revision BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
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
    """ALTER TABLE mininex_live_eth_policy_enrollments
       ADD COLUMN IF NOT EXISTS authorization_method TEXT NOT NULL
       DEFAULT 'EIP191_AND_PRIVY'
       CHECK (authorization_method IN
           ('EIP191_AND_PRIVY','PRIVY_SCOPED_GRANT'))""",
    """ALTER TABLE mininex_live_eth_policy_enrollments
       ADD COLUMN IF NOT EXISTS grant_budget_sha256 TEXT
       CHECK (grant_budget_sha256 IS NULL OR
          grant_budget_sha256 ~ '^[0-9a-f]{64}$')""",
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
            CHECK (reserved_notional_usd > 0),
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
    """CREATE TABLE IF NOT EXISTS mininex_live_eth_verified_legs (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        tx_hash TEXT PRIMARY KEY CHECK (tx_hash ~ '^0x[0-9a-f]{64}$'),
        side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
        stable TEXT NOT NULL CHECK (stable IN ('USDC','USDT')),
        kind TEXT NOT NULL CHECK (kind IN ('SWAP','WRAP','UNWRAP')),
        source_amount NUMERIC(45,18) NOT NULL CHECK (source_amount > 0),
        destination_amount NUMERIC(45,18) NOT NULL CHECK (destination_amount > 0),
        network_fee_eth NUMERIC(45,18) NOT NULL CHECK (network_fee_eth >= 0),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CHECK ((kind='WRAP' AND side='SELL') OR
               (kind='UNWRAP' AND side='BUY') OR kind='SWAP')
    )""",
    """CREATE INDEX IF NOT EXISTS mininex_eth_verified_legs_by_owner
       ON mininex_live_eth_verified_legs(privy_did,created_at DESC)""",
    """CREATE TABLE IF NOT EXISTS mininex_live_evm_policy_publications (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        asset TEXT NOT NULL CHECK(asset IN ('BNB','POL','AVAX','LINK','UNI','ARB')),
        policy_sha256 TEXT NOT NULL CHECK(policy_sha256 ~ '^[0-9a-f]{64}$'),
        idempotency_key TEXT NOT NULL CHECK(idempotency_key ~ '^[0-9a-f]{64}$'),
        status TEXT NOT NULL CHECK(status IN (
            'SUBMITTING','UNCERTAIN','REMOTE_VERIFIED_UNATTACHED')),
        policy_id TEXT CHECK(policy_id IS NULL OR
            policy_id ~ '^[A-Za-z0-9_-]{6,160}$'),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(privy_did,asset),
        UNIQUE(idempotency_key)
    )""",
    """CREATE TABLE IF NOT EXISTS mininex_live_evm_auto_sessions (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        asset TEXT NOT NULL CHECK(asset IN ('BNB','POL','AVAX','LINK','UNI','ARB')),
        chain_id INTEGER NOT NULL CHECK(chain_id IN (1,56,137,43114,42161)),
        stable TEXT NOT NULL CHECK(stable IN ('USDC','USDT')),
        wallet_address TEXT NOT NULL CHECK(wallet_address ~ '^0x[0-9a-f]{40}$'),
        budget_revision BIGINT NOT NULL CHECK(budget_revision >= 1),
        status TEXT NOT NULL DEFAULT 'PAUSED'
            CHECK(status IN ('ACTIVE','PAUSED','STOPPED')),
        started_by_owner_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(privy_did,asset),
        CHECK (status <> 'ACTIVE' OR started_by_owner_at IS NOT NULL)
    )""",
    """CREATE TABLE IF NOT EXISTS mininex_live_evm_budget_reservations (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        asset TEXT NOT NULL CHECK(asset IN ('BNB','POL','AVAX','LINK','UNI','ARB')),
        intent_key TEXT NOT NULL CHECK(intent_key ~ '^[A-Za-z0-9_-]{8,96}$'),
        stable TEXT NOT NULL CHECK(stable IN ('USDC','USDT')),
        side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
        budget_revision BIGINT NOT NULL CHECK(budget_revision >= 1),
        amount_usd NUMERIC(36,6) NOT NULL CHECK(amount_usd > 0),
        quote_sha256 TEXT NOT NULL CHECK(quote_sha256 ~ '^[0-9a-f]{64}$'),
        state TEXT NOT NULL DEFAULT 'RESERVED'
            CHECK(state IN ('RESERVED','SENDING','SUBMITTED',
                'RECONCILIATION_REQUIRED','FINALIZED','CANCELLED')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(privy_did,asset,intent_key),
        CHECK(
            (asset='BNB' AND stable='USDT') OR
            (asset IN ('POL','AVAX') AND stable='USDC') OR
            asset IN ('LINK','UNI','ARB'))
    )""",
    """CREATE INDEX IF NOT EXISTS mininex_evm_budget_reserved_by_owner
       ON mininex_live_evm_budget_reservations(privy_did,asset,state)""",
    """CREATE TABLE IF NOT EXISTS mininex_live_evm_consent_challenges (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        asset TEXT NOT NULL CHECK(asset IN ('BNB','POL','AVAX','LINK','UNI','ARB')),
        session_sha256 TEXT NOT NULL CHECK(session_sha256 ~ '^[0-9a-f]{64}$'),
        wallet_id TEXT NOT NULL CHECK(wallet_id ~ '^[A-Za-z0-9_-]{6,160}$'),
        wallet_address TEXT NOT NULL CHECK(wallet_address ~ '^0x[0-9a-f]{40}$'),
        chain_id INTEGER NOT NULL CHECK(chain_id IN (1,56,137,43114,42161)),
        stable TEXT NOT NULL CHECK(stable IN ('USDC','USDT')),
        budget_revision BIGINT NOT NULL CHECK(budget_revision >= 1),
        budget_usd NUMERIC(36,6) NOT NULL CHECK(budget_usd > 0),
        permission_revision BIGINT NOT NULL CHECK(permission_revision >= 0),
        message_text TEXT NOT NULL CHECK(length(message_text) BETWEEN 120 AND 3500),
        message_sha256 TEXT NOT NULL CHECK(message_sha256 ~ '^[0-9a-f]{64}$'),
        issued_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK(status IN ('PENDING','SIGNED_INTENT','REVOKED')),
        confirmed_at TIMESTAMPTZ,
        revoked_at TIMESTAMPTZ,
        PRIMARY KEY(privy_did,asset),
        CHECK(expires_at>issued_at AND expires_at<=issued_at+interval '5 minutes'),
        CHECK(
            (asset='BNB' AND chain_id=56 AND stable='USDT') OR
            (asset='POL' AND chain_id=137 AND stable='USDC') OR
            (asset='AVAX' AND chain_id=43114 AND stable='USDC') OR
            (asset IN ('LINK','UNI') AND chain_id=1) OR
            (asset='ARB' AND chain_id=42161 AND stable IN ('USDC','USDT')))
    )""",
    """CREATE TABLE IF NOT EXISTS mininex_live_evm_owner_permissions (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        asset TEXT NOT NULL CHECK(asset IN ('BNB','POL','AVAX','LINK','UNI','ARB')),
        chain_id INTEGER NOT NULL CHECK(chain_id IN (1,56,137,43114,42161)),
        stable TEXT NOT NULL CHECK(stable IN ('USDC','USDT')),
        wallet_id TEXT NOT NULL CHECK(wallet_id ~ '^[A-Za-z0-9_-]{6,160}$'),
        wallet_address TEXT NOT NULL CHECK(wallet_address ~ '^0x[0-9a-f]{40}$'),
        owner_id TEXT NOT NULL CHECK(owner_id ~ '^[A-Za-z0-9_-]{6,160}$'),
        signer_id TEXT NOT NULL CHECK(signer_id ~ '^[A-Za-z0-9_-]{6,160}$'),
        policy_id TEXT CHECK(policy_id IS NULL OR
            policy_id ~ '^[A-Za-z0-9_-]{6,160}$'),
        policy_sha256 TEXT NOT NULL CHECK(policy_sha256 ~ '^[0-9a-f]{64}$'),
        remote_policy_sha256 TEXT CHECK(remote_policy_sha256 IS NULL OR
            remote_policy_sha256 ~ '^[0-9a-f]{64}$'),
        consent_sha256 TEXT CHECK(consent_sha256 IS NULL OR
            consent_sha256 ~ '^[0-9a-f]{64}$'),
        consent_verified_at TIMESTAMPTZ,
        grant_verified_at TIMESTAMPTZ,
        revoked_at TIMESTAMPTZ,
        expires_at TIMESTAMPTZ NOT NULL,
        status TEXT NOT NULL DEFAULT 'DRAFT'
            CHECK(status IN ('DRAFT','CONSENT_VERIFIED',
              'GRANT_VERIFIED','REVOKED')),
        revision BIGINT NOT NULL DEFAULT 0 CHECK(revision >= 0),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(privy_did,asset),
        CHECK(owner_id <> signer_id),
        CHECK (
            (asset='BNB' AND chain_id=56 AND stable='USDT') OR
            (asset='POL' AND chain_id=137 AND stable='USDC') OR
            (asset='AVAX' AND chain_id=43114 AND stable='USDC') OR
            (asset IN ('LINK','UNI') AND chain_id=1) OR
            (asset='ARB' AND chain_id=42161 AND stable IN ('USDC','USDT'))),
        CHECK(status NOT IN ('CONSENT_VERIFIED','GRANT_VERIFIED') OR
            (consent_sha256 IS NOT NULL AND consent_verified_at IS NOT NULL)),
        CHECK(status <> 'GRANT_VERIFIED' OR
            (policy_id IS NOT NULL AND remote_policy_sha256=policy_sha256
                AND grant_verified_at IS NOT NULL)),
        CHECK(status <> 'REVOKED' OR revoked_at IS NOT NULL)
    )""",
    """CREATE INDEX IF NOT EXISTS mininex_evm_owner_permissions_chain
       ON mininex_live_evm_owner_permissions(privy_did,chain_id,status)""",
    """ALTER TABLE mininex_live_evm_owner_permissions
       ADD COLUMN IF NOT EXISTS budget_revision BIGINT""",
    """ALTER TABLE mininex_live_evm_owner_permissions
       ADD COLUMN IF NOT EXISTS candidate_json JSONB""",
    """ALTER TABLE mininex_live_evm_owner_permissions
       ADD COLUMN IF NOT EXISTS authorization_method TEXT NOT NULL
       DEFAULT 'EIP191_AND_PRIVY'
       CHECK (authorization_method IN
          ('EIP191_AND_PRIVY','PRIVY_SCOPED_GRANT'))""",
    """CREATE TABLE IF NOT EXISTS mininex_live_evm_auto_broadcasts (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        asset TEXT NOT NULL CHECK(asset IN ('BNB','POL','AVAX','LINK','UNI','ARB')),
        chain_id INTEGER NOT NULL CHECK(chain_id IN (1,56,137,43114,42161)),
        intent_key TEXT NOT NULL CHECK (length(intent_key) BETWEEN 8 AND 96),
        wallet_address TEXT NOT NULL CHECK (
            wallet_address ~ '^0x[0-9a-f]{40}$'),
        wallet_id TEXT NOT NULL CHECK (length(wallet_id) BETWEEN 6 AND 160),
        request_digest TEXT NOT NULL CHECK (
            request_digest ~ '^[0-9a-f]{64}$'),
        side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
        stable TEXT NOT NULL CHECK(stable IN ('USDC','USDT')),
        status TEXT NOT NULL CHECK(status IN (
            'SENDING','SUBMITTED','RECONCILIATION_REQUIRED',
            'FINALIZED_SUCCESS','FINALIZED_REVERTED')),
        tx_hash TEXT CHECK (tx_hash IS NULL OR
            tx_hash ~ '^0x[0-9a-f]{64}$'),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(privy_did,asset,intent_key),
        UNIQUE(privy_did,chain_id,intent_key),
        CHECK (
            (asset='BNB' AND chain_id=56 AND stable='USDT') OR
            (asset='POL' AND chain_id=137 AND stable='USDC') OR
            (asset='AVAX' AND chain_id=43114 AND stable='USDC') OR
            (asset IN ('LINK','UNI') AND chain_id=1) OR
            (asset='ARB' AND chain_id=42161 AND stable IN ('USDC','USDT'))),
        CHECK (status NOT IN (
            'SUBMITTED','FINALIZED_SUCCESS','FINALIZED_REVERTED')
            OR tx_hash IS NOT NULL)
    )""",
    """CREATE INDEX IF NOT EXISTS mininex_live_evm_pending_by_wallet
       ON mininex_live_evm_auto_broadcasts(
          privy_did,chain_id,status)""",
    """CREATE TABLE IF NOT EXISTS mininex_live_eth_auto_broadcasts (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        intent_key TEXT NOT NULL,
        asset TEXT NOT NULL DEFAULT 'ETH' CHECK (asset='ETH'),
        wallet_address TEXT NOT NULL,
        wallet_id TEXT NOT NULL,
        request_digest TEXT NOT NULL CHECK (request_digest ~ '^[a-f0-9]{64}$'),
        side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
        stable TEXT NOT NULL CHECK (stable IN ('USDC','USDT')),
        status TEXT NOT NULL CHECK (
           status IN ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED')),
        tx_hash TEXT CHECK (tx_hash IS NULL OR tx_hash ~ '^0x[a-f0-9]{64}$'),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(privy_did,intent_key),
        FOREIGN KEY(privy_did,asset,intent_key)
           REFERENCES mininex_live_order_intents(privy_did,asset,intent_key),
        CHECK (status <> 'SUBMITTED' OR tx_hash IS NOT NULL)
    )""",
    """CREATE TABLE IF NOT EXISTS mininex_live_eth_auto_sessions (
        privy_did TEXT PRIMARY KEY REFERENCES mininex_accounts(privy_did),
        run_id TEXT NOT NULL UNIQUE,
        stable TEXT NOT NULL CHECK(stable IN ('USDC','USDT')),
        status TEXT NOT NULL DEFAULT 'PAUSED' CHECK(status IN ('ACTIVE','PAUSED')),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS mininex_live_eth_auto_native_steps (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        source_key TEXT NOT NULL,
        intent_key TEXT NOT NULL UNIQUE,
        operation TEXT NOT NULL CHECK(operation IN ('DEPOSIT','WITHDRAW')),
        stable TEXT NOT NULL CHECK(stable IN ('USDC','USDT')),
        amount_wei NUMERIC(45,0) NOT NULL CHECK(amount_wei>0),
        wallet_address TEXT NOT NULL,
        wallet_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN
            ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED',
             'CONFIRMED','REVERTED')),
        tx_hash TEXT UNIQUE CHECK(
            tx_hash IS NULL OR tx_hash ~ '^0x[a-f0-9]{64}$'),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(privy_did,source_key,operation)
    )""",
    """CREATE INDEX IF NOT EXISTS mininex_live_eth_native_pending
       ON mininex_live_eth_auto_native_steps(privy_did,status)""",
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
    # Permanent individual setup for the ten additional coins. This is NOT
    # shared across coins and does not modify a running PAPER or LIVE session.
    """CREATE TABLE IF NOT EXISTS mininex_extra_coin_preferences (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        asset TEXT NOT NULL CHECK (asset IN
            ('BNB','SOL','XRP','TRX','POL','AVAX','LINK','DOGE','UNI','ARB')),
        budget_usd BIGINT NOT NULL DEFAULT 500
            CHECK (budget_usd > 0),
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
    # Independent virtual accounts for ten extra coins. BTC/ETH schemas and
    # real HWM/fee/live ledgers remain untouched.
    """CREATE TABLE IF NOT EXISTS mininex_extra_paper_sessions (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        asset TEXT NOT NULL CHECK (asset IN
            ('BNB','SOL','XRP','TRX','POL','AVAX','LINK','DOGE','UNI','ARB')),
        status TEXT NOT NULL DEFAULT 'ACTIVE'
            CHECK (status IN ('ACTIVE','PAUSED')),
        settlement TEXT NOT NULL CHECK (settlement IN ('USDC','USDT')),
        strategy_mode TEXT NOT NULL CHECK (strategy_mode IN ('LONG','SPOT_SHORT','AUTO')),
        horizon TEXT NOT NULL CHECK (horizon IN ('5m','15m','1h','24h')),
        initial_demo_cash NUMERIC(28,8) NOT NULL
            CHECK (initial_demo_cash > 0),
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
    """CREATE TABLE IF NOT EXISTS mininex_live_evm_verified_fills (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        asset TEXT NOT NULL CHECK(asset IN ('BNB','POL','AVAX','LINK','UNI','ARB')),
        chain_id INTEGER NOT NULL CHECK(chain_id IN (1,56,137,43114,42161)),
        intent_key TEXT NOT NULL,
        tx_hash TEXT NOT NULL CHECK(tx_hash ~ '^0x[0-9a-f]{64}$'),
        wallet_address TEXT NOT NULL CHECK(wallet_address ~ '^0x[0-9a-f]{40}$'),
        side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
        stable TEXT NOT NULL CHECK(stable IN ('USDC','USDT')),
        source_amount NUMERIC(48,18) NOT NULL CHECK(source_amount > 0),
        destination_amount NUMERIC(48,18) NOT NULL CHECK(destination_amount > 0),
        network_fee_native NUMERIC(48,18) NOT NULL CHECK(network_fee_native >= 0),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(privy_did,asset,intent_key),
        UNIQUE(chain_id,tx_hash),
        FOREIGN KEY(privy_did,asset,intent_key)
            REFERENCES mininex_live_evm_auto_broadcasts(privy_did,asset,intent_key)
    )""",
    """CREATE TABLE IF NOT EXISTS mininex_live_evm_native_sell_intents (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        asset TEXT NOT NULL CHECK(asset IN ('BNB','POL','AVAX')),
        intent_key TEXT NOT NULL CHECK(intent_key ~ '^[A-Za-z0-9_-]{8,96}$'),
        strategy_run_id TEXT NOT NULL CHECK(length(strategy_run_id) BETWEEN 8 AND 128),
        signal_event_id TEXT NOT NULL CHECK(length(signal_event_id) BETWEEN 8 AND 128),
        chain_id INTEGER NOT NULL CHECK(chain_id IN (56,137,43114)),
        stable TEXT NOT NULL CHECK(stable IN ('USDC','USDT')),
        wallet_id TEXT NOT NULL CHECK(length(wallet_id) BETWEEN 6 AND 160),
        wallet_address TEXT NOT NULL CHECK(wallet_address ~ '^0x[0-9a-f]{40}$'),
        budget_revision BIGINT NOT NULL CHECK(budget_revision >= 1),
        amount_wei NUMERIC(48,0) NOT NULL CHECK(amount_wei > 0),
        request_digest TEXT NOT NULL CHECK(request_digest ~ '^[0-9a-f]{64}$'),
        status TEXT NOT NULL DEFAULT 'SENDING' CHECK(status IN
          ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED',
           'FINALIZED_SUCCESS','FINALIZED_REVERTED')),
        tx_hash TEXT CHECK(tx_hash IS NULL OR tx_hash ~ '^0x[0-9a-f]{64}$'),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(privy_did,asset,intent_key),
        UNIQUE(chain_id,tx_hash)
    )""",
    """CREATE TABLE IF NOT EXISTS mininex_live_evm_native_auto_steps (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        asset TEXT NOT NULL CHECK(asset IN ('BNB','POL','AVAX')),
        intent_key TEXT NOT NULL CHECK(intent_key ~ '^[A-Za-z0-9_-]{8,96}$'),
        operation TEXT NOT NULL CHECK(operation IN ('DEPOSIT','WITHDRAW')),
        chain_id INTEGER NOT NULL CHECK(chain_id IN (56,137,43114)),
        stable TEXT NOT NULL CHECK(stable IN ('USDC','USDT')),
        wallet_address TEXT NOT NULL CHECK(wallet_address ~ '^0x[0-9a-f]{40}$'),
        wallet_id TEXT NOT NULL CHECK(length(wallet_id) BETWEEN 6 AND 160),
        amount_wei NUMERIC(48,0) NOT NULL CHECK(amount_wei > 0),
        request_digest TEXT NOT NULL CHECK(request_digest ~ '^[0-9a-f]{64}$'),
        status TEXT NOT NULL DEFAULT 'SENDING' CHECK(status IN
           ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED',
            'FINALIZED_SUCCESS','FINALIZED_REVERTED')),
        tx_hash TEXT CHECK(tx_hash IS NULL OR tx_hash ~ '^0x[0-9a-f]{64}$'),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(privy_did,asset,intent_key,operation),
        UNIQUE(chain_id,tx_hash),
        FOREIGN KEY(privy_did,asset,intent_key)
            REFERENCES mininex_live_evm_auto_broadcasts(privy_did,asset,intent_key)
    )""",
    """CREATE TABLE IF NOT EXISTS mininex_live_evm_completed_native_cycles (
        privy_did TEXT NOT NULL,
        asset TEXT NOT NULL CHECK(asset IN ('BNB','POL','AVAX')),
        intent_key TEXT NOT NULL,
        chain_id INTEGER NOT NULL CHECK(chain_id IN (56,137,43114)),
        wallet_address TEXT NOT NULL CHECK(wallet_address ~ '^0x[0-9a-f]{40}$'),
        swap_tx_hash TEXT NOT NULL CHECK(swap_tx_hash ~ '^0x[0-9a-f]{64}$'),
        conversion_tx_hash TEXT NOT NULL CHECK(
            conversion_tx_hash ~ '^0x[0-9a-f]{64}$'),
        side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
        stable TEXT NOT NULL CHECK(stable IN ('USDC','USDT')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(privy_did,asset,intent_key),
        UNIQUE(chain_id,conversion_tx_hash),
        FOREIGN KEY(privy_did,asset,intent_key)
            REFERENCES mininex_live_evm_auto_broadcasts(privy_did,asset,intent_key)
    )""",
    """CREATE TABLE IF NOT EXISTS mininex_evm_fee_auto_permissions (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        asset TEXT NOT NULL CHECK(asset IN ('BNB','POL','AVAX','LINK','UNI','ARB')),
        stable TEXT NOT NULL CHECK(stable IN ('USDC','USDT')),
        chain_id INTEGER NOT NULL CHECK(chain_id IN (1,56,137,43114,42161)),
        wallet_id TEXT NOT NULL CHECK(length(wallet_id) BETWEEN 6 AND 160),
        wallet_address TEXT NOT NULL CHECK(wallet_address ~ '^0x[0-9a-f]{40}$'),
        treasury_wallet TEXT NOT NULL CHECK(treasury_wallet ~ '^0x[0-9a-f]{40}$'),
        owner_id TEXT NOT NULL,
        signer_id TEXT NOT NULL,
        owner_max_fee_atomic NUMERIC(48,0) NOT NULL CHECK(owner_max_fee_atomic > 0),
        expires_at TIMESTAMPTZ NOT NULL,
        challenge_text TEXT NOT NULL,
        challenge_sha256 TEXT NOT NULL CHECK(challenge_sha256 ~ '^[0-9a-f]{64}$'),
        challenge_expires_at TIMESTAMPTZ NOT NULL,
        owner_signed_at TIMESTAMPTZ,
        policy_json JSONB NOT NULL,
        policy_sha256 TEXT NOT NULL CHECK(policy_sha256 ~ '^[0-9a-f]{64}$'),
        policy_id TEXT,
        remote_policy_sha256 TEXT,
        grant_verified_at TIMESTAMPTZ,
        status TEXT NOT NULL CHECK(status IN
            ('PENDING_INTENT','CONSENT_SIGNED','REMOTE_UNATTACHED',
             'GRANT_VERIFIED','REVOKED','REMOTE_UNCERTAIN')),
        idempotency_key TEXT,
        policy_attempted_at TIMESTAMPTZ,
        revoked_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(privy_did,asset),
        CHECK(wallet_address <> treasury_wallet),
        CHECK(owner_id <> signer_id),
        CHECK((asset='BNB' AND chain_id=56 AND stable='USDT') OR
              (asset='POL' AND chain_id=137 AND stable='USDC') OR
              (asset='AVAX' AND chain_id=43114 AND stable='USDC') OR
              (asset IN ('LINK','UNI') AND chain_id=1) OR
            (asset='ARB' AND chain_id=42161 AND stable IN ('USDC','USDT'))),
        CHECK(status <> 'GRANT_VERIFIED' OR
              (owner_signed_at IS NOT NULL AND grant_verified_at IS NOT NULL
               AND policy_id IS NOT NULL AND
               remote_policy_sha256=policy_sha256)),
        CHECK(status <> 'PENDING_INTENT' OR owner_signed_at IS NULL),
        CHECK(status <> 'REVOKED' OR revoked_at IS NOT NULL)
    )""",
    """CREATE TABLE IF NOT EXISTS mininex_evm_immutable_fill_usd_prices (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        asset TEXT NOT NULL CHECK(asset IN ('BNB','POL','AVAX','LINK','UNI','ARB')),
        chain_id INTEGER NOT NULL CHECK(chain_id IN (1,56,137,43114,42161)),
        tx_hash TEXT NOT NULL CHECK(tx_hash ~ '^0x[0-9a-f]{64}$'),
        quoted_asset TEXT NOT NULL CHECK(quoted_asset IN
            ('ETH','BNB','POL','AVAX','USDC','USDT')),
        block_number BIGINT NOT NULL CHECK(block_number > 0),
        block_hash TEXT NOT NULL CHECK(block_hash ~ '^0x[0-9a-f]{64}$'),
        block_unix BIGINT NOT NULL CHECK(block_unix > 0),
        observed_unix BIGINT NOT NULL CHECK(observed_unix > 0),
        CHECK (abs(observed_unix-block_unix) <= 180),
        usd_price NUMERIC(36,18) NOT NULL CHECK(usd_price > 0),
        provider TEXT NOT NULL CHECK(provider='COINGECKO_MARKET_CHART_RANGE'),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(privy_did,asset,tx_hash,quoted_asset),
        FOREIGN KEY(chain_id,tx_hash)
            REFERENCES mininex_live_evm_verified_fills(chain_id,tx_hash)
    )""",
    """CREATE OR REPLACE FUNCTION mininex_prevent_historical_rate_mutation()
       RETURNS trigger LANGUAGE plpgsql AS $$
       BEGIN
           RAISE EXCEPTION 'Verified original historical USD quote is immutable';
       END $$""",
    """DO $$
       BEGIN
           IF NOT EXISTS (
               SELECT 1 FROM pg_trigger
                WHERE tgname='mininex_historical_usd_rate_write_once'
           ) THEN
               CREATE TRIGGER mininex_historical_usd_rate_write_once
               BEFORE UPDATE OR DELETE
               ON mininex_evm_immutable_fill_usd_prices
               FOR EACH ROW EXECUTE FUNCTION
                   mininex_prevent_historical_rate_mutation();
           END IF;
       END $$""",
    """CREATE TABLE IF NOT EXISTS mininex_evm_asset_hwm (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        asset TEXT NOT NULL CHECK(asset IN ('BNB','POL','AVAX','LINK','UNI','ARB')),
        cumulative_realized_net_usd NUMERIC(36,12) NOT NULL DEFAULT 0,
        high_water_mark_usd NUMERIC(36,12) NOT NULL DEFAULT 0
            CHECK(high_water_mark_usd >= 0),
        total_fees_assessed_usd NUMERIC(36,12) NOT NULL DEFAULT 0
            CHECK(total_fees_assessed_usd >= 0),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(privy_did,asset)
    )""",
    """CREATE TABLE IF NOT EXISTS mininex_evm_fee_events (
        privy_did TEXT NOT NULL,
        asset TEXT NOT NULL CHECK(asset IN ('BNB','POL','AVAX','LINK','UNI','ARB')),
        execution_id TEXT NOT NULL CHECK(length(execution_id) BETWEEN 1 AND 180),
        realized_net_delta_usd NUMERIC(36,12) NOT NULL,
        cumulative_realized_net_usd NUMERIC(36,12) NOT NULL,
        high_water_mark_usd NUMERIC(36,12) NOT NULL,
        fee_rate NUMERIC(6,5) NOT NULL CHECK(fee_rate=0.05),
        fee_assessed_usd NUMERIC(36,12) NOT NULL CHECK(fee_assessed_usd >= 0),
        source_tx_hash TEXT NOT NULL CHECK(source_tx_hash ~ '^0x[0-9a-f]{64}$'),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(privy_did,asset,execution_id),
        FOREIGN KEY(privy_did,asset)
            REFERENCES mininex_evm_asset_hwm(privy_did,asset)
    )""",
    """CREATE TABLE IF NOT EXISTS mininex_evm_fee_obligations (
        privy_did TEXT NOT NULL,
        asset TEXT NOT NULL,
        execution_id TEXT NOT NULL,
        fee_due_usd NUMERIC(36,12) NOT NULL CHECK(fee_due_usd > 0),
        settlement_status TEXT NOT NULL DEFAULT 'AWAITING_VERIFIED_TRANSFER',
        settlement_tx_hash TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(privy_did,asset,execution_id),
        FOREIGN KEY(privy_did,asset,execution_id)
            REFERENCES mininex_evm_fee_events(privy_did,asset,execution_id),
        CONSTRAINT mininex_evm_fee_obligations_payment_state_check CHECK (
            (settlement_status='AWAITING_VERIFIED_TRANSFER'
             AND settlement_tx_hash IS NULL)
            OR (settlement_status='PAID_VERIFIED'
                AND settlement_tx_hash ~ '^0x[0-9a-f]{64}$'))
    )""",
    """DO $$
       BEGIN
         ALTER TABLE mininex_evm_fee_obligations
           DROP CONSTRAINT IF EXISTS
           mininex_evm_fee_obligations_settlement_status_check;
         ALTER TABLE mininex_evm_fee_obligations
           DROP CONSTRAINT IF EXISTS
           mininex_evm_fee_obligations_settlement_tx_hash_check;
         IF NOT EXISTS (
           SELECT 1 FROM pg_constraint
           WHERE conname='mininex_evm_fee_obligations_payment_state_check'
             AND conrelid='mininex_evm_fee_obligations'::regclass
         ) THEN
           ALTER TABLE mininex_evm_fee_obligations
             ADD CONSTRAINT mininex_evm_fee_obligations_payment_state_check
             CHECK (
               (settlement_status='AWAITING_VERIFIED_TRANSFER'
                AND settlement_tx_hash IS NULL)
               OR (settlement_status='PAID_VERIFIED'
                   AND settlement_tx_hash ~ '^0x[0-9a-f]{64}$'));
         END IF;
       END
       $$""",
    """CREATE TABLE IF NOT EXISTS mininex_evm_verified_fee_payments (
        privy_did TEXT NOT NULL,
        asset TEXT NOT NULL CHECK(asset IN ('BNB','POL','AVAX','LINK','UNI','ARB')),
        execution_id TEXT NOT NULL,
        chain_id INTEGER NOT NULL CHECK(chain_id IN (1,56,137,43114,42161)),
        stable TEXT NOT NULL CHECK(stable IN ('USDC','USDT')),
        stable_token_address TEXT NOT NULL CHECK(
            stable_token_address ~ '^0x[0-9a-f]{40}$'),
        owner_wallet TEXT NOT NULL CHECK(owner_wallet ~ '^0x[0-9a-f]{40}$'),
        treasury_wallet TEXT NOT NULL CHECK(treasury_wallet ~ '^0x[0-9a-f]{40}$'),
        fee_due_usd NUMERIC(36,12) NOT NULL CHECK(fee_due_usd > 0),
        stable_usd NUMERIC(36,12) NOT NULL CHECK(
            stable_usd >= 0.95 AND stable_usd <= 1.05),
        token_amount_atomic NUMERIC(48,0) NOT NULL CHECK(token_amount_atomic > 0),
        state TEXT NOT NULL DEFAULT 'PREPARED' CHECK(
            state IN ('PREPARED','PAID_VERIFIED')),
        tx_hash TEXT CHECK(tx_hash IS NULL OR tx_hash ~ '^0x[0-9a-f]{64}$'),
        prepared_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        paid_at TIMESTAMPTZ,
        PRIMARY KEY(privy_did,asset,execution_id),
        UNIQUE(chain_id,tx_hash),
        FOREIGN KEY(privy_did,asset,execution_id)
            REFERENCES mininex_evm_fee_obligations(privy_did,asset,execution_id),
        CHECK (
            (state='PREPARED' AND tx_hash IS NULL AND paid_at IS NULL)
            OR (state='PAID_VERIFIED' AND tx_hash IS NOT NULL AND paid_at IS NOT NULL))
    )""",
    """CREATE TABLE IF NOT EXISTS mininex_evm_auto_fee_sends (
        privy_did TEXT NOT NULL,
        asset TEXT NOT NULL CHECK(asset IN ('BNB','POL','AVAX','LINK','UNI','ARB')),
        execution_id TEXT NOT NULL,
        chain_id INTEGER NOT NULL CHECK(chain_id IN (1,56,137,43114,42161)),
        stable TEXT NOT NULL CHECK(stable IN ('USDC','USDT')),
        wallet_id TEXT NOT NULL CHECK(length(wallet_id) BETWEEN 6 AND 160),
        wallet_address TEXT NOT NULL CHECK(wallet_address ~ '^0x[0-9a-f]{40}$'),
        treasury_wallet TEXT NOT NULL CHECK(treasury_wallet ~ '^0x[0-9a-f]{40}$'),
        policy_id TEXT NOT NULL,
        policy_sha256 TEXT NOT NULL CHECK(policy_sha256 ~ '^[0-9a-f]{64}$'),
        token_address TEXT NOT NULL CHECK(token_address ~ '^0x[0-9a-f]{40}$'),
        amount_atomic NUMERIC(48,0) NOT NULL CHECK(amount_atomic > 0),
        request_digest TEXT NOT NULL CHECK(request_digest ~ '^[0-9a-f]{64}$'),
        idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 16 AND 96),
        status TEXT NOT NULL CHECK(status IN
            ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED',
             'PAID_VERIFIED','FINALIZED_REVERTED')),
        tx_hash TEXT CHECK(tx_hash IS NULL OR tx_hash ~ '^0x[0-9a-f]{64}$'),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(privy_did,asset,execution_id),
        UNIQUE(chain_id,tx_hash),
        UNIQUE(privy_did,idempotency_key),
        FOREIGN KEY(privy_did,asset,execution_id)
            REFERENCES mininex_evm_verified_fee_payments(privy_did,asset,execution_id),
        CHECK ((status IN ('SENDING','RECONCILIATION_REQUIRED')
                AND tx_hash IS NULL)
            OR (status IN ('SUBMITTED','PAID_VERIFIED','FINALIZED_REVERTED')
                AND tx_hash IS NOT NULL))
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

    """DO $paper_budget_migration$
    DECLARE item RECORD;
    BEGIN
      FOR item IN
        SELECT c.conname, c.conrelid::regclass AS table_name
        FROM pg_constraint c
        WHERE c.contype='c'
          AND c.conrelid IN (
              'mininex_extra_coin_preferences'::regclass,
              'mininex_extra_paper_sessions'::regclass)
          AND pg_get_constraintdef(c.oid) LIKE '%5000%'
      LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I',
                       item.table_name,item.conname);
      END LOOP;
    END $paper_budget_migration$""",
    """ALTER TABLE mininex_extra_coin_preferences
       ALTER COLUMN budget_usd TYPE BIGINT""",
    # Existing owner accounts were created before ARB replaced ADA. CREATE
    # TABLE IF NOT EXISTS cannot update their constraints: migrate both tables
    # in place WITHOUT deleting an existing balance, row or user preference.
    """ALTER TABLE mininex_extra_coin_preferences
       DROP CONSTRAINT IF EXISTS
       mininex_extra_coin_preferences_asset_check""",
    """ALTER TABLE mininex_extra_coin_preferences
       ADD CONSTRAINT mininex_extra_coin_preferences_asset_check
       CHECK (asset IN
         ('BNB','SOL','XRP','TRX','POL','AVAX','LINK','DOGE','UNI','ARB'))""",
    """ALTER TABLE mininex_extra_paper_sessions
       DROP CONSTRAINT IF EXISTS
       mininex_extra_paper_sessions_asset_check""",
    """ALTER TABLE mininex_extra_paper_sessions
       ADD CONSTRAINT mininex_extra_paper_sessions_asset_check
       CHECK (asset IN
         ('BNB','SOL','XRP','TRX','POL','AVAX','LINK','DOGE','UNI','ARB'))""",
    # The common per-coin AUTO strategy exists in application code; repair the
    # historic SQL check so independently selected AUTO settings can persist.
    """ALTER TABLE mininex_extra_paper_sessions
       DROP CONSTRAINT IF EXISTS
       mininex_extra_paper_sessions_strategy_mode_check""",
    """ALTER TABLE mininex_extra_paper_sessions
       ADD CONSTRAINT mininex_extra_paper_sessions_strategy_mode_check
       CHECK (strategy_mode IN ('LONG','SPOT_SHORT','AUTO'))""",
    # Existing PostgreSQL tables predate ARB. Preserve records while
    # replacing ONLY their previous auto-generated asset/chain CHECKs.
    """DO $arb_evm_schema_upgrade$
    DECLARE name text; definition text; oldcheck record;
    BEGIN
      FOREACH name IN ARRAY ARRAY['mininex_live_evm_policy_publications','mininex_live_evm_auto_sessions','mininex_live_evm_budget_reservations','mininex_live_evm_consent_challenges','mininex_live_evm_owner_permissions','mininex_live_evm_auto_broadcasts','mininex_live_evm_verified_fills','mininex_evm_fee_auto_permissions','mininex_evm_immutable_fill_usd_prices','mininex_evm_asset_hwm','mininex_evm_fee_events','mininex_evm_verified_fee_payments','mininex_evm_auto_fee_sends'] LOOP
        IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE
          conrelid=name::regclass AND conname='mininex_evm_arb_asset_v2') THEN
          EXECUTE format('ALTER TABLE %I DROP CONSTRAINT IF EXISTS %I',
                         name,name||'_asset_check');
          EXECUTE format($arbcheck$ALTER TABLE %I ADD CONSTRAINT mininex_evm_arb_asset_v2 CHECK (asset IN ('BNB','POL','AVAX','LINK','UNI','ARB'))$arbcheck$,name);
        END IF;
      END LOOP;
      FOREACH name IN ARRAY ARRAY['mininex_live_evm_auto_sessions','mininex_live_evm_consent_challenges','mininex_live_evm_owner_permissions','mininex_live_evm_auto_broadcasts','mininex_live_evm_verified_fills','mininex_evm_fee_auto_permissions','mininex_evm_immutable_fill_usd_prices','mininex_evm_verified_fee_payments','mininex_evm_auto_fee_sends'] LOOP
        IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE
          conrelid=name::regclass AND conname='mininex_evm_arb_chain_v2') THEN
          EXECUTE format('ALTER TABLE %I DROP CONSTRAINT IF EXISTS %I',
                         name,name||'_chain_id_check');
          EXECUTE format('ALTER TABLE %I ADD CONSTRAINT mininex_evm_arb_chain_v2 CHECK (chain_id IN (1,56,137,43114,42161))',name);
        END IF;
      END LOOP;
      name:='mininex_live_evm_budget_reservations';
      IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE
        conrelid=name::regclass AND conname='mininex_evm_arb_route_v2') THEN
        FOR oldcheck IN SELECT conname FROM pg_constraint c
          WHERE c.conrelid=name::regclass AND c.contype='c'
            AND pg_get_constraintdef(c.oid) LIKE '%LINK%'
            AND pg_get_constraintdef(c.oid) LIKE '%UNI%'
            AND pg_get_constraintdef(c.oid) NOT LIKE '%ARB%'
            AND (pg_get_constraintdef(c.oid) LIKE '%stable%'
                 OR pg_get_constraintdef(c.oid) LIKE '%chain_id%') LOOP
          EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I',name,oldcheck.conname);
        END LOOP;
        EXECUTE format($arbcheck$ALTER TABLE %I ADD CONSTRAINT mininex_evm_arb_route_v2 CHECK ((asset='BNB' AND stable='USDT') OR (asset IN ('POL','AVAX') AND stable='USDC') OR (asset IN ('LINK','UNI','ARB')))$arbcheck$,name);
      END IF;
      name:='mininex_live_evm_consent_challenges';
      IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE
        conrelid=name::regclass AND conname='mininex_evm_arb_route_v2') THEN
        FOR oldcheck IN SELECT conname FROM pg_constraint c
          WHERE c.conrelid=name::regclass AND c.contype='c'
            AND pg_get_constraintdef(c.oid) LIKE '%LINK%'
            AND pg_get_constraintdef(c.oid) LIKE '%UNI%'
            AND pg_get_constraintdef(c.oid) NOT LIKE '%ARB%'
            AND (pg_get_constraintdef(c.oid) LIKE '%stable%'
                 OR pg_get_constraintdef(c.oid) LIKE '%chain_id%') LOOP
          EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I',name,oldcheck.conname);
        END LOOP;
        EXECUTE format($arbcheck$ALTER TABLE %I ADD CONSTRAINT mininex_evm_arb_route_v2 CHECK ((asset='BNB' AND chain_id=56 AND stable='USDT') OR (asset='POL' AND chain_id=137 AND stable='USDC') OR (asset='AVAX' AND chain_id=43114 AND stable='USDC') OR (asset IN ('LINK','UNI') AND chain_id=1) OR (asset='ARB' AND chain_id=42161 AND stable IN ('USDC','USDT')))$arbcheck$,name);
      END IF;
      name:='mininex_live_evm_owner_permissions';
      IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE
        conrelid=name::regclass AND conname='mininex_evm_arb_route_v2') THEN
        FOR oldcheck IN SELECT conname FROM pg_constraint c
          WHERE c.conrelid=name::regclass AND c.contype='c'
            AND pg_get_constraintdef(c.oid) LIKE '%LINK%'
            AND pg_get_constraintdef(c.oid) LIKE '%UNI%'
            AND pg_get_constraintdef(c.oid) NOT LIKE '%ARB%'
            AND (pg_get_constraintdef(c.oid) LIKE '%stable%'
                 OR pg_get_constraintdef(c.oid) LIKE '%chain_id%') LOOP
          EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I',name,oldcheck.conname);
        END LOOP;
        EXECUTE format($arbcheck$ALTER TABLE %I ADD CONSTRAINT mininex_evm_arb_route_v2 CHECK ((asset='BNB' AND chain_id=56 AND stable='USDT') OR (asset='POL' AND chain_id=137 AND stable='USDC') OR (asset='AVAX' AND chain_id=43114 AND stable='USDC') OR (asset IN ('LINK','UNI') AND chain_id=1) OR (asset='ARB' AND chain_id=42161 AND stable IN ('USDC','USDT')))$arbcheck$,name);
      END IF;
      name:='mininex_live_evm_auto_broadcasts';
      IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE
        conrelid=name::regclass AND conname='mininex_evm_arb_route_v2') THEN
        FOR oldcheck IN SELECT conname FROM pg_constraint c
          WHERE c.conrelid=name::regclass AND c.contype='c'
            AND pg_get_constraintdef(c.oid) LIKE '%LINK%'
            AND pg_get_constraintdef(c.oid) LIKE '%UNI%'
            AND pg_get_constraintdef(c.oid) NOT LIKE '%ARB%'
            AND (pg_get_constraintdef(c.oid) LIKE '%stable%'
                 OR pg_get_constraintdef(c.oid) LIKE '%chain_id%') LOOP
          EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I',name,oldcheck.conname);
        END LOOP;
        EXECUTE format($arbcheck$ALTER TABLE %I ADD CONSTRAINT mininex_evm_arb_route_v2 CHECK ((asset='BNB' AND chain_id=56 AND stable='USDT') OR (asset='POL' AND chain_id=137 AND stable='USDC') OR (asset='AVAX' AND chain_id=43114 AND stable='USDC') OR (asset IN ('LINK','UNI') AND chain_id=1) OR (asset='ARB' AND chain_id=42161 AND stable IN ('USDC','USDT')))$arbcheck$,name);
      END IF;
      name:='mininex_evm_fee_auto_permissions';
      IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE
        conrelid=name::regclass AND conname='mininex_evm_arb_route_v2') THEN
        FOR oldcheck IN SELECT conname FROM pg_constraint c
          WHERE c.conrelid=name::regclass AND c.contype='c'
            AND pg_get_constraintdef(c.oid) LIKE '%LINK%'
            AND pg_get_constraintdef(c.oid) LIKE '%UNI%'
            AND pg_get_constraintdef(c.oid) NOT LIKE '%ARB%'
            AND (pg_get_constraintdef(c.oid) LIKE '%stable%'
                 OR pg_get_constraintdef(c.oid) LIKE '%chain_id%') LOOP
          EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I',name,oldcheck.conname);
        END LOOP;
        EXECUTE format($arbcheck$ALTER TABLE %I ADD CONSTRAINT mininex_evm_arb_route_v2 CHECK ((asset='BNB' AND chain_id=56 AND stable='USDT') OR (asset='POL' AND chain_id=137 AND stable='USDC') OR (asset='AVAX' AND chain_id=43114 AND stable='USDC') OR (asset IN ('LINK','UNI') AND chain_id=1) OR (asset='ARB' AND chain_id=42161 AND stable IN ('USDC','USDT')))$arbcheck$,name);
      END IF;
    END $arb_evm_schema_upgrade$""",

    """DO $migration$
    DECLARE item RECORD;
    BEGIN
      FOR item IN
        SELECT c.conname, c.conrelid::regclass AS table_name
        FROM pg_constraint c
        WHERE c.contype='c'
          AND c.conrelid IN (
            'mininex_live_eth_limits'::regclass,
            'mininex_live_btc_limits'::regclass,
            'mininex_live_order_intents'::regclass)
          AND (
            pg_get_constraintdef(c.oid) LIKE '%10000000%'
            OR pg_get_constraintdef(c.oid) LIKE '%max_order_usd <=%'
            OR pg_get_constraintdef(c.oid) LIKE '%max_daily_turnover_usd%'
          )
      LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I',
                       item.table_name,item.conname);
      END LOOP;
    END $migration$""",
)


# Owner-granted original Router02 approval sends, separate from swaps.
SCHEMA += (
    """CREATE TABLE IF NOT EXISTS mininex_live_evm_router_approval_sends (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        asset TEXT NOT NULL CHECK(asset IN ('BNB','POL','AVAX','LINK','UNI','ARB')),
        side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
        stable TEXT NOT NULL CHECK(stable IN ('USDC','USDT')),
        chain_id INTEGER NOT NULL CHECK(chain_id IN (1,56,137,43114,42161)),
        wallet_id TEXT NOT NULL,
        wallet_address TEXT NOT NULL CHECK(wallet_address ~ '^0x[0-9a-f]{40}$'),
        token_address TEXT NOT NULL CHECK(token_address ~ '^0x[0-9a-f]{40}$'),
        router_address TEXT NOT NULL CHECK(router_address ~ '^0x[0-9a-f]{40}$'),
        amount_atomic NUMERIC(78,0) NOT NULL CHECK(
            amount_atomic>0 AND amount_atomic<1e45),
        policy_sha256 TEXT NOT NULL CHECK(policy_sha256 ~ '^[0-9a-f]{64}$'),
        budget_revision BIGINT NOT NULL CHECK(budget_revision>0),
        idempotency_key TEXT NOT NULL UNIQUE,
        request_digest TEXT NOT NULL CHECK(request_digest ~ '^[0-9a-f]{64}$'),
        status TEXT NOT NULL CHECK(status IN
            ('SENDING','SUBMITTED','RECONCILIATION_REQUIRED',
             'FINALIZED_SUCCESS','FINALIZED_REVERTED')),
        tx_hash TEXT CHECK(tx_hash IS NULL OR tx_hash ~ '^0x[0-9a-f]{64}$'),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(privy_did,asset,idempotency_key)
    )""",
)


# Native BTC/SOL/XRP durable chain settlement schema from PR #202.
SCHEMA += (
    # Each native original intent is written before any future Privy submission.
    # PREPARED records are inert, and unknown SENDING outcomes cannot be retried.
    """CREATE TABLE IF NOT EXISTS mininex_live_native_order_intents (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        asset TEXT NOT NULL CHECK (asset IN ('BTC','SOL','XRP')),
        intent_key TEXT NOT NULL CHECK (length(intent_key) BETWEEN 6 AND 160),
        chain TEXT NOT NULL CHECK (chain IN
            ('bitcoin-mainnet','solana-mainnet','xrpl-mainnet','ethereum-mainnet')),
        side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
        stable TEXT NOT NULL CHECK (stable IN ('USDC','USDT')),
        budget_revision BIGINT NOT NULL CHECK (budget_revision >= 0),
        source_wallet TEXT NOT NULL,
        destination_wallet TEXT NOT NULL,
        input_amount NUMERIC(60,18) NOT NULL CHECK (input_amount > 0),
        immutable_quote_sha256 TEXT NOT NULL
            CHECK (immutable_quote_sha256 ~ '^[0-9a-f]{64}$'),
        owner_consent_sha256 TEXT NOT NULL
            CHECK (owner_consent_sha256 ~ '^[0-9a-f]{64}$'),
        privy_grant_sha256 TEXT NOT NULL
            CHECK (privy_grant_sha256 ~ '^[0-9a-f]{64}$'),
        payload_sha256 TEXT NOT NULL
            CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
        quote_expires_at TIMESTAMPTZ NOT NULL,
        status TEXT NOT NULL DEFAULT 'PREPARED'
          CHECK (status IN ('PREPARED','SENDING','SUBMITTED',
                            'RECONCILIATION_REQUIRED','SOURCE_FINALIZED',
                            'DESTINATION_FINALIZED','REFUNDED_VERIFIED',
                            'CANCELLED')),
        source_tx_hash TEXT,
        source_signed_tx_hash TEXT CHECK (source_signed_tx_hash IS NULL
            OR source_signed_tx_hash ~ '^[A-Za-z0-9]{64,90}$'),
        source_proof_sha256 TEXT CHECK (source_proof_sha256 IS NULL OR
            source_proof_sha256 ~ '^[0-9a-f]{64}$'),
        source_finalized_at TIMESTAMPTZ,
        destination_tx_hash TEXT,
        destination_proof_sha256 TEXT CHECK (
            destination_proof_sha256 IS NULL OR
            destination_proof_sha256 ~ '^[0-9a-f]{64}$'),
        destination_received_atomic NUMERIC(78,0) CHECK (
            destination_received_atomic > 0),
        destination_finalized_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(privy_did,asset,intent_key),
        CHECK ((status IN ('PREPARED','SENDING','RECONCILIATION_REQUIRED',
                           'CANCELLED') AND source_tx_hash IS NULL)
            OR (status IN ('SUBMITTED','SOURCE_FINALIZED',
                           'DESTINATION_FINALIZED','REFUNDED_VERIFIED')
                AND source_tx_hash IS NOT NULL)),
        CHECK (source_tx_hash IS NULL OR
               source_tx_hash ~ '^[A-Za-z0-9]{64,90}$'),
        CHECK (destination_tx_hash IS NULL OR
               destination_tx_hash ~ '^[A-Za-z0-9]{64,90}$'),
        CHECK (destination_tx_hash IS NULL OR
               status IN ('DESTINATION_FINALIZED','REFUNDED_VERIFIED')),
        CHECK (status != 'DESTINATION_FINALIZED' OR (
            destination_tx_hash IS NOT NULL AND
            destination_proof_sha256 IS NOT NULL AND
            destination_received_atomic IS NOT NULL AND
            destination_finalized_at IS NOT NULL)),
        CHECK ((status IN ('SOURCE_FINALIZED','DESTINATION_FINALIZED',
                           'REFUNDED_VERIFIED') AND
                source_proof_sha256 IS NOT NULL AND
                source_finalized_at IS NOT NULL)
             OR status NOT IN ('SOURCE_FINALIZED','DESTINATION_FINALIZED',
                                'REFUNDED_VERIFIED'))
    )""",
    """ALTER TABLE mininex_live_native_order_intents
        ADD COLUMN IF NOT EXISTS source_signed_tx_hash TEXT
        CHECK (source_signed_tx_hash IS NULL
          OR source_signed_tx_hash ~ '^[A-Za-z0-9]{64,90}$')""",
    """CREATE UNIQUE INDEX IF NOT EXISTS mininex_native_signed_source_hash_unique
       ON mininex_live_native_order_intents(chain,source_signed_tx_hash)
       WHERE source_signed_tx_hash IS NOT NULL""",
    """CREATE INDEX IF NOT EXISTS mininex_native_pending_owner_scan
       ON mininex_live_native_order_intents
       (privy_did,chain,updated_at)
       WHERE status IN ('SENDING','SUBMITTED',
                        'RECONCILIATION_REQUIRED','SOURCE_FINALIZED')""",
    """CREATE UNIQUE INDEX IF NOT EXISTS mininex_native_original_source_hash_unique
       ON mininex_live_native_order_intents(chain,source_tx_hash)
       WHERE source_tx_hash IS NOT NULL""",

)


# Independently observed server-side LIVE worker heartbeat (not a trade,
# wallet signature, deposit, or evidence of a finalized chain transaction).
SCHEMA += (
    """CREATE TABLE IF NOT EXISTS mininex_live_worker_heartbeats (
        worker_name TEXT PRIMARY KEY,
        last_poll_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        status TEXT NOT NULL CHECK(status IN ('RUNNING'))
    )""",
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
    # Independent BTC LIVE limits are separate from ETH and always paused.
    cur.execute(
        """INSERT INTO mininex_live_btc_limits(privy_did)
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
                """SELECT fee_assessed_usd,realized_net_delta_usd,
                          cumulative_realized_net_usd,high_water_mark_usd
                   FROM mininex_fee_events
                   WHERE privy_did=%s AND asset=%s AND execution_id=%s""",
                (did,asset,execution_id),
            )
            existing=cur.fetchone()
            if existing:
                # Idempotency does not permit silently changing a previously
                # reconciled trade's realized PnL under the same execution ID.
                # Treat differing economic facts as an accounting conflict.
                if Decimal(str(existing["realized_net_delta_usd"])) != delta:
                    raise HTTPException(409, "Execution accounting conflict")
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
