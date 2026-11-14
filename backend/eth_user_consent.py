"""Signed, one-time MiniNex ETH enrollment INTENT; no wallet delegation.

A user signing this human-readable EIP-191 message acknowledges the precise
wallet, plan revision and proposed actions. It does not grant Privy signer
authority and is NEVER sufficient to authorize a live order. The separate
Privy wallet delegation must still be user-approved, policy-scoped, verified
against Privy and revocable.

Only authenticated server-side routes call these functions. Challenges are
stored in PostgreSQL under the account's ETH LIVE limit row lock. Any changed
live limit revision, new challenge, expiry, session change, wrong EOA wallet,
or replay invalidates confirmation. EIP-1271 contract wallets are NOT
supported by this proof and must not be silently accepted.

No private key, raw JWT or API secret is ever requested or logged here.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import time

import psycopg
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_keys.exceptions import BadSignature
from fastapi import HTTPException
from psycopg.rows import dict_row

from .eth_swaprouter02_preflight import SWAPROUTER02_MAINNET, WETH_MAINNET
from .eth_user_enrollment_preflight import get_eth_user_enrollment_preflight
from .live_eth_controls import validate_limits
from .privy_wallet_verification import APP_ID
from .storage import _db_url, _ensure_schema, _prepare_rows

LOG = logging.getLogger("mininex.eth_consent")
_DID = re.compile(r"^did:privy:[A-Za-z0-9_-]{4,180}$")
_ID = re.compile(r"^[A-Za-z0-9_-]{6,160}$")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SIG = re.compile(r"^0x[0-9a-fA-F]{130}$")
_EXPIRY_SEC = 300


def _require_identity(did: object, session_id: object) -> tuple[str, str]:
    if (type(did) is not str or _DID.fullmatch(did) is None
            or type(session_id) is not str
            or not 8 <= len(session_id) <= 256
            or any(ord(x) < 33 or ord(x) > 126 for x in session_id)):
        raise HTTPException(401, "Verified Privy user session required")
    return did, hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _require_enrollment_evidence(evidence: object) -> dict:
    if type(evidence) is not dict:
        raise HTTPException(409, "Verified wallet and LIVE limits required")
    requirements = (
        "wallet_identity_verified", "wallet_owned_by_authenticated_user",
        "wallet_chain_verified", "wallet_additional_signers_reviewed",
        "policy_owner_independent", "user_live_limits_verified",
        "live_limits_nonzero", "trading_paused",
    )
    if (any(evidence.get(k) is not True for k in requirements)
            or evidence.get("unrecognized_existing_signers") is not False):
        raise HTTPException(409, "LIVE wallet, policy owner or limits not ready")
    wallet = evidence.get("verified_wallet_address")
    revision = evidence.get("live_limits_revision")
    if (type(wallet) is not str or not _ADDRESS.fullmatch(wallet)
            or type(revision) is not int or revision < 0):
        raise HTTPException(409, "LIVE wallet and limits revision invalid")
    values = (
        evidence.get("live_capital_usd"),
        evidence.get("live_max_order_usd"),
        evidence.get("live_daily_turnover_usd"),
    )
    try:
        cap, order, daily = validate_limits(*values)
    except (HTTPException, TypeError, ValueError):
        raise HTTPException(409, "LIVE limits invalid") from None
    if min(cap, order, daily) <= 0:
        raise HTTPException(409, "Nonzero LIVE limits required")
    return {
        "wallet": wallet.lower(),
        "revision": revision,
        "capital": str(cap),
        "order": str(order),
        "daily": str(daily),
    }


def _require_policy_roles() -> tuple[str, str]:
    owner = os.getenv("MININEX_PRIVY_ETH_POLICY_OWNER_ID", "").strip()
    signer = os.getenv("MININEX_PRIVY_ETH_SIGNER_ID", "").strip()
    if (not _ID.fullmatch(owner) or not _ID.fullmatch(signer)
            or owner == signer):
        raise HTTPException(409, "Independent policy owner and signer required")
    return owner, signer


def _message(*, did: str, wallet: str, session_digest: str, nonce: str,
             issued: int, expires: int, rev: int, capital: str,
             per_order: str, daily: str, owner: str, signer: str) -> str:
    """Canonical user-readable message; never executable EVM calldata."""
    return "\n".join((
        "MiniNex ETH - request to prepare automated trading",
        "This signature records my enrollment request ONLY.",
        "It does NOT delegate my wallet or authorize any transaction.",
        "A separate explicit Privy wallet approval will be required.",
        "I can withdraw this request in MiniNex at any time.",
        f"Application: {APP_ID}",
        "Network: Ethereum Mainnet (chain 1)",
        f"Privy User ID: {did}",
        f"Wallet: {wallet}",
        f"Session SHA256: {session_digest}",
        f"Nonce: {nonce}",
        f"Issued Unix: {issued}",
        f"Expires Unix: {expires}",
        f"Live limits revision: {rev}",
        f"Planned capital USD: {capital}",
        f"Maximum order USD: {per_order}",
        f"Maximum daily turnover USD: {daily}",
        "Eligible pairs: ETH / USDC and ETH / USDT only",
        f"Uniswap SwapRouter02: {SWAPROUTER02_MAINNET}",
        f"WETH9: {WETH_MAINNET}",
        f"Policy owner ID: {owner}",
        f"Execution signer ID: {signer}",
        "No approval for transfers, seed export, or arbitrary contracts.",
    ))


def _check_locked_limits(cur, did: str, values: dict) -> None:
    cur.execute(
        """SELECT planned_capital_usd,max_order_usd,max_daily_turnover_usd,
                  trading_paused,revision
           FROM mininex_live_eth_limits WHERE privy_did=%s FOR UPDATE""",
        (did,),
    )
    row = cur.fetchone()
    if (type(row) is not dict or row.get("trading_paused") is not True
            or row.get("revision") != values["revision"]):
        raise HTTPException(409, "LIVE limits changed; request fresh consent")
    cap, order, daily = validate_limits(
        str(row["planned_capital_usd"]), str(row["max_order_usd"]),
        str(row["max_daily_turnover_usd"]),
    )
    if (str(cap), str(order), str(daily)) != (
        values["capital"], values["order"], values["daily"]
    ):
        raise HTTPException(409, "LIVE limits changed; request fresh consent")


def _begin_locked(cur, *, did: str, session_digest: str, evidence: dict,
                  owner: str, signer: str, now: int, nonce: str) -> dict:
    """One SQL transaction under a LIVE limits row lock; old nonce replaced."""
    _check_locked_limits(cur, did, evidence)
    expires = now + _EXPIRY_SEC
    message = _message(
        did=did, wallet=evidence["wallet"], session_digest=session_digest,
        nonce=nonce, issued=now, expires=expires, rev=evidence["revision"],
        capital=evidence["capital"], per_order=evidence["order"],
        daily=evidence["daily"], owner=owner, signer=signer,
    )
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
    cur.execute(
        """INSERT INTO mininex_live_eth_consent_intents
              (privy_did,wallet_address,session_sha256,limits_revision,
               policy_owner_id,execution_signer_id,
               message_text,message_sha256,status,issued_at,expires_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',to_timestamp(%s),
                   to_timestamp(%s))
           ON CONFLICT (privy_did) DO UPDATE SET
               wallet_address=EXCLUDED.wallet_address,
               session_sha256=EXCLUDED.session_sha256,
               limits_revision=EXCLUDED.limits_revision,
               policy_owner_id=EXCLUDED.policy_owner_id,
               execution_signer_id=EXCLUDED.execution_signer_id,
               message_text=EXCLUDED.message_text,
               message_sha256=EXCLUDED.message_sha256,
               status='PENDING',
               issued_at=EXCLUDED.issued_at,
               expires_at=EXCLUDED.expires_at,
               confirmed_at=NULL,revoked_at=NULL""",
        (did, evidence["wallet"], session_digest, evidence["revision"],
         owner, signer, message, digest, now, expires),
    )
    return {
        "mode": "ETH_CONSENT_SIGNED_INTENT_ONLY",
        "asset": "ETH", "chain_id": 1, "wallet": evidence["wallet"],
        "challenge": message, "expires_at_unix": expires,
        "live_limits_revision": evidence["revision"],
        "status": "PENDING",
        "wallet_signature_required": True,
        "user_grant_created": False,
        "privy_signer_delegated": False,
        "execution_enabled": False,
        "can_submit": False,
    }


def verify_eoa_eip191_signature(*, message: str, signature: object,
                               wallet: str) -> bool:
    """EIP-191 personal_sign only; never trust wallet from request body.

    Contract-wallet EIP-1271 needs a separately reviewed RPC verifier.
    Signature malleability and protocol checks are handled by eth-account.
    """
    if (type(signature) is not str or _SIG.fullmatch(signature) is None
            or type(message) is not str or len(message) > 3500
            or type(wallet) is not str or _ADDRESS.fullmatch(wallet) is None):
        return False
    try:
        recovered = Account.recover_message(
            encode_defunct(text=message), signature=signature
        )
        return recovered.lower() == wallet.lower()
    except (BadSignature, ValueError, TypeError, OverflowError):
        return False


def _confirm_locked(cur, *, did: str, session_digest: str,
                    evidence: dict, signature: str, now: int,
                    owner: str, signer: str) -> dict:
    """One-use nonce, exact signed text and locked live limits required."""
    _check_locked_limits(cur, did, evidence)
    cur.execute(
        """SELECT wallet_address,session_sha256,limits_revision,
                  policy_owner_id,execution_signer_id,message_text,
                  message_sha256,status,extract(epoch FROM expires_at)::bigint
                      AS expires_at_unix
           FROM mininex_live_eth_consent_intents
           WHERE privy_did=%s FOR UPDATE""",
        (did,),
    )
    row = cur.fetchone()
    if (type(row) is not dict or row.get("status") != "PENDING"
            or row.get("wallet_address") != evidence["wallet"]
            or row.get("session_sha256") != session_digest
            or row.get("limits_revision") != evidence["revision"]
            or row.get("policy_owner_id") != owner
            or row.get("execution_signer_id") != signer
            or type(row.get("message_text")) is not str
            or row.get("expires_at_unix") is None
            or now >= int(row["expires_at_unix"])
            or row.get("message_sha256") != hashlib.sha256(
                row["message_text"].encode("utf-8")
            ).hexdigest()):
        raise HTTPException(409, "Consent challenge missing, stale or already used")
    if not verify_eoa_eip191_signature(
        message=row["message_text"], signature=signature,
        wallet=evidence["wallet"],
    ):
        raise HTTPException(403, "Wallet signature does not match Privy owner")
    cur.execute(
        """UPDATE mininex_live_eth_consent_intents
           SET status='SIGNED_INTENT',confirmed_at=now()
           WHERE privy_did=%s AND status='PENDING'""",
        (did,),
    )
    return {
        "mode": "ETH_CONSENT_SIGNED_INTENT_ONLY",
        "asset": "ETH", "chain_id": 1,
        "status": "SIGNED_INTENT",
        "wallet": evidence["wallet"],
        "user_intent_signature_verified": True,
        "user_privy_delegation_granted": False,
        "separate_privy_permission_still_required": True,
        "live_signing_approved": False,
        "execution_enabled": False,
        "can_submit": False,
    }


async def prepare_eth_consent_challenge(did: str, session_id: str) -> dict:
    did, session_digest = _require_identity(did, session_id)
    evidence = _require_enrollment_evidence(
        await get_eth_user_enrollment_preflight(did)
    )
    owner, signer = _require_policy_roles()
    nonce = secrets.token_hex(32)
    now = int(time.time())
    try:
        url = _db_url()
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                return _begin_locked(
                    cur, did=did, session_digest=session_digest,
                    evidence=evidence, owner=owner, signer=signer,
                    now=now, nonce=nonce,
                )
    except psycopg.Error:
        LOG.warning("ETH signed intent storage unavailable")
        raise HTTPException(503, "Consent storage temporarily unavailable") from None


async def confirm_eth_consent_intent(
    did: str, session_id: str, signature: str,
) -> dict:
    did, session_digest = _require_identity(did, session_id)
    evidence = _require_enrollment_evidence(
        await get_eth_user_enrollment_preflight(did)
    )
    owner, signer = _require_policy_roles()
    now = int(time.time())
    try:
        url = _db_url()
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                return _confirm_locked(
                    cur, did=did, session_digest=session_digest,
                    evidence=evidence, signature=signature, now=now,
                    owner=owner, signer=signer,
                )
    except psycopg.Error:
        LOG.warning("ETH consent confirmation temporarily unavailable")
        raise HTTPException(503, "Consent storage temporarily unavailable") from None


def revoke_eth_consent_intent(did: str) -> dict:
    """Revoke MiniNex-signed intent AND pause LIVE sends in ONE DB transaction.

    This does NOT remove an already Privy-registered additional signer!
    Privy remote revocation will need its own independently verified flow
    BEFORE any actual delegation can ever be enabled.
    """
    _require_identity(did, "server_verified_session")
    try:
        url = _db_url()
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                cur.execute(
                    """UPDATE mininex_live_eth_limits
                       SET trading_paused=TRUE,revision=revision+1,
                           updated_at=now()
                       WHERE privy_did=%s
                       RETURNING revision""",
                    (did,),
                )
                if type(cur.fetchone()) is not dict:
                    raise HTTPException(503, "ETH stop state unavailable")
                cur.execute(
                    """UPDATE mininex_live_eth_consent_intents
                       SET status='REVOKED',revoked_at=now()
                       WHERE privy_did=%s""",
                    (did,),
                )
                # All local state transitions are ONE transaction. A remote
                # signer is NOT removed by this DB update. It must be
                # independently revoked and verified in Privy later.
                cur.execute(
                    """UPDATE mininex_live_eth_policy_enrollments
                       SET status='REVOKE_REQUESTED',updated_at=now()
                       WHERE privy_did=%s AND status<>'REVOKED'
                       RETURNING status""",
                    (did,),
                )
                policy_revocation_recorded = type(cur.fetchone()) is dict
        return {
            "mode": "ETH_CONSENT_SIGNED_INTENT_ONLY",
            "privy_policy_ledger_revoke_requested": policy_revocation_recorded,
            "asset": "ETH", "status": "REVOKED_LOCALLY",
            "mininex_live_sends_paused": True,
            "local_consent_intent_revoked": True,
            "privy_remote_signer_removed": False,
            "privy_remote_revocation_required_if_delegated": True,
            "execution_enabled": False, "can_submit": False,
        }
    except psycopg.Error:
        LOG.warning("ETH consent revocation temporarily unavailable")
        raise HTTPException(503, "ETH consent could not be revoked") from None
