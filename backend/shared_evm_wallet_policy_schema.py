"""Durable no-grant journal for a single EVM Privy signer per user wallet.

Creating these tables never registers a signer, authorizes a policy,
starts an order, or migrates existing per-coin user permissions.
"""
WALLET_POLICY_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS mininex_live_evm_wallet_policy_grants (
        privy_did TEXT PRIMARY KEY REFERENCES mininex_accounts(privy_did),
        wallet_id TEXT NOT NULL CHECK(length(wallet_id) BETWEEN 6 AND 160),
        wallet_address TEXT NOT NULL
            CHECK(wallet_address ~ '^0x[0-9a-f]{40}$'),
        owner_id TEXT NOT NULL CHECK(length(owner_id) BETWEEN 6 AND 160),
        signer_id TEXT NOT NULL CHECK(length(signer_id) BETWEEN 6 AND 160),
        policy_id TEXT UNIQUE CHECK(policy_id IS NULL OR
            policy_id ~ '^[A-Za-z0-9_-]{6,160}$'),
        policy_sha256 TEXT CHECK(policy_sha256 IS NULL OR
            policy_sha256 ~ '^[0-9a-f]{64}$'),
        scopes_json JSONB NOT NULL DEFAULT '{}'::jsonb
            CHECK(jsonb_typeof(scopes_json)='object'),
        revisions_json JSONB NOT NULL DEFAULT '{}'::jsonb
            CHECK(jsonb_typeof(revisions_json)='object'),
        status TEXT NOT NULL DEFAULT 'DRAFT'
            CHECK(status IN ('DRAFT','SUBMITTING','UNCERTAIN',
                'REMOTE_VERIFIED_UNATTACHED','GRANT_VERIFIED',
                'UPDATE_PENDING','UPDATING','UPDATE_UNCERTAIN',
                'REVOKED')),
        legacy_reconciliation_required BOOLEAN NOT NULL DEFAULT TRUE,
        revision BIGINT NOT NULL DEFAULT 1 CHECK(revision>0),
        remote_verified_at TIMESTAMPTZ,
        original_owner_granted_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        revoked_at TIMESTAMPTZ,
        CHECK(owner_id <> signer_id),
        CHECK(status NOT IN ('REMOTE_VERIFIED_UNATTACHED','GRANT_VERIFIED')
              OR (policy_id IS NOT NULL AND policy_sha256 IS NOT NULL
                  AND remote_verified_at IS NOT NULL)),
        CHECK(status <> 'GRANT_VERIFIED' OR
              original_owner_granted_at IS NOT NULL),
        CHECK(status <> 'REVOKED' OR revoked_at IS NOT NULL)
    )""",
    """CREATE TABLE IF NOT EXISTS mininex_live_evm_wallet_policy_intents (
        privy_did TEXT NOT NULL REFERENCES mininex_accounts(privy_did),
        nonce TEXT NOT NULL CHECK(nonce ~ '^[0-9a-f]{64}$'),
        wallet_id TEXT NOT NULL CHECK(length(wallet_id) BETWEEN 6 AND 160),
        wallet_address TEXT NOT NULL
            CHECK(wallet_address ~ '^0x[0-9a-f]{40}$'),
        previous_policy_sha256 TEXT NOT NULL
            CHECK(previous_policy_sha256 ~ '^[0-9a-f]{64}$'),
        target_policy_sha256 TEXT NOT NULL
            CHECK(target_policy_sha256 ~ '^[0-9a-f]{64}$'),
        challenge_text TEXT NOT NULL CHECK(length(challenge_text) <= 3500),
        challenge_sha256 TEXT NOT NULL
            CHECK(challenge_sha256 ~ '^[0-9a-f]{64}$'),
        status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK(status IN ('PENDING','SIGNED_INTENT','CONSUMED',
                             'REVOKED','UNCERTAIN')),
        issued_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        signed_at TIMESTAMPTZ,
        consumed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(privy_did,nonce),
        CHECK(expires_at > issued_at AND
              expires_at <= issued_at + interval '5 minutes'),
        CHECK(status NOT IN ('SIGNED_INTENT','CONSUMED') OR
              signed_at IS NOT NULL),
        CHECK(status <> 'CONSUMED' OR consumed_at IS NOT NULL)
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS
       mininex_live_evm_wallet_policy_intent_active_once
       ON mininex_live_evm_wallet_policy_intents(privy_did)
       WHERE status IN ('PENDING','SIGNED_INTENT')""",
)
