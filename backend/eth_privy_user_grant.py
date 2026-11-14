"""Step 24: wallet-owner Privy signer-grant offer and readback verification.

A signed EIP-191 INTENT is not wallet delegation. Only the owner, through
the Privy SDK addSigners UI, may grant one exact backend signing quorum and
one reviewed user-specific override policy. Operator grant flag defaults OFF
until a separate controlled Privy ABI and cumulative spend security audit.

All real trade, signing and transaction submission remain disabled.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .eth_user_consent import _check_locked_limits, _require_enrollment_evidence, _require_policy_roles
from .eth_privy_policy_transport import _auth, verify_remote_privy_policy
from .eth_user_enrollment_preflight import get_eth_user_enrollment_preflight
from .privy_live_signer import _owned_eth_wallet, _scoped_signer, fetch_privy_wallet_metadata
from .privy_wallet_verification import APP_ID, fetch_privy_user
from .storage import _db_url, _ensure_schema, _prepare_rows, _safe_did

LOG = logging.getLogger("mininex.eth_privy_user_grant")
_POLICY_URL = "https://api.privy.io/v1/policies/"
_ID = re.compile(r"^[A-Za-z0-9_-]{6,160}$")


def _grant_enabled() -> bool:
    return os.getenv("MININEX_PRIVY_ETH_GRANT_ENABLED", "").strip() == "enabled-after-audit"


def _read_policy_for_grant(did: str, *, wallet: str, owner: str,
                           signer: str, revision: int) -> dict:
    try:
        url = _db_url()
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                cur.execute(
                    """SELECT policy_id,candidate_json,candidate_sha256,
                              remote_policy_sha256,status,wallet_address,
                              policy_owner_id,execution_signer_id,
                              limits_revision,
                              extract(epoch FROM policy_expires_at)::bigint
                                  AS expires_at_unix
                       FROM mininex_live_eth_policy_enrollments
                       WHERE privy_did=%s""", (did,),
                )
                record = cur.fetchone()
    except psycopg.Error:
        raise HTTPException(503, "Privy user policy storage unavailable") from None
    if (
        type(record) is not dict
        or record.get("status") not in ("REMOTE_VERIFIED_UNATTACHED","GRANT_VERIFIED")
        or record.get("wallet_address") != wallet
        or record.get("policy_owner_id") != owner
        or record.get("execution_signer_id") != signer
        or record.get("limits_revision") != revision
        or type(record.get("candidate_json")) is not dict
        or type(record.get("candidate_sha256")) is not str
        or record.get("remote_policy_sha256") != record["candidate_sha256"]
        or type(record.get("policy_id")) is not str
        or _ID.fullmatch(record["policy_id"]) is None
        or type(record.get("expires_at_unix")) is not int
        or not 60 < record["expires_at_unix"] - int(time.time()) <= 86400
    ):
        raise HTTPException(409, "Current restricted Privy policy not available")
    return record


async def _fetch_policy(policy_id: str) -> dict:
    if type(policy_id) is not str or _ID.fullmatch(policy_id) is None:
        raise HTTPException(409, "Invalid Privy policy ID")
    auth = _auth()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(9.0), follow_redirects=False) as client:
            resp = await client.get(
                _POLICY_URL + policy_id, auth=auth,
                headers={"privy-app-id":APP_ID,"Accept":"application/json"},
            )
            resp.raise_for_status()
            if len(resp.content) > 256_000:
                raise ValueError("Oversized Privy policy")
            value = resp.json()
        if type(value) is not dict or value.get("id") != policy_id:
            raise ValueError("Wrong remote Privy policy")
        return value
    except (httpx.HTTPError, ValueError, TypeError):
        raise HTTPException(503, "Privy policy independent readback unavailable") from None


def _verify_full_policy(remote: dict, stored: dict, owner: str) -> bool:
    try:
        checked = verify_remote_privy_policy(remote, stored["candidate_json"])
        return (
            checked["policy_id"] == stored["policy_id"]
            and checked["policy_sha256"] == stored["candidate_sha256"]
            and checked["policy_sha256"] == stored["remote_policy_sha256"]
            and remote.get("owner_id") == owner
        )
    except (ValueError, TypeError, KeyError):
        return False


def _inert_grant_result() -> dict:
    return {
        "mode":"ETH_PRIVY_SCOPED_GRANT_REVIEW_ONLY",
        "asset":"ETH", "chain_id":1,
        "policy_id":None, "execution_signer_id":None,
        "verified_wallet_address":None,
        "policy_verified":False,
        "wallet_owned_by_user":False,
        "unexpected_signers":True,
        "user_privy_grant_verified":False,
        "can_request_user_grant":False,
        "grant_offer_enabled":False,
        "cumulative_spend_enforced_by_policy":False,
        "production_trading_enabled":False,
        "can_sign":False, "can_submit":False,
        "execution_enabled":False,
    }


def assess_privy_user_grant(
    *, did: str, user: dict, metadata: dict, stored: dict,
    remote: dict, evidence: dict, owner: str, signer: str,
) -> dict:
    result = _inert_grant_result()
    try:
        wallet = _owned_eth_wallet(user, did)
    except ValueError:
        return result
    if (
        evidence.get("wallet_identity_verified") is not True
        or evidence.get("wallet_owned_by_authenticated_user") is not True
        or evidence.get("wallet_chain_verified") is not True
        or evidence.get("user_live_limits_verified") is not True
        or evidence.get("live_limits_nonzero") is not True
        or evidence.get("trading_paused") is not True
        or evidence.get("verified_wallet_address") != wallet["address"]
        or type(metadata) is not dict
        or metadata.get("id") != wallet["wallet_id"]
        or metadata.get("chain_type") != "ethereum"
        or str(metadata.get("address", "")).lower() != wallet["address"]
        or type(metadata.get("owner")) is not dict
        or metadata["owner"].get("user_id") != did
        or type(metadata.get("additional_signers")) is not list
        or stored.get("wallet_address") != wallet["address"]
        or stored.get("policy_owner_id") != owner
        or stored.get("execution_signer_id") != signer
        or not _verify_full_policy(remote, stored, owner)
    ):
        return result
    result["wallet_owned_by_user"] = True
    result["policy_verified"] = True
    result["verified_wallet_address"] = wallet["address"]
    result["policy_id"] = stored["policy_id"]
    result["execution_signer_id"] = signer
    signers = metadata["additional_signers"]
    if len(signers) == 0 and stored["status"] == "REMOTE_VERIFIED_UNATTACHED":
        result["unexpected_signers"] = False
        result["grant_offer_enabled"] = _grant_enabled()
        result["can_request_user_grant"] = _grant_enabled()
    elif len(signers) == 1 and _scoped_signer(
        metadata, did=did, address=wallet["address"],
        config={"signer_id":signer, "policy_id":stored["policy_id"]},
    ):
        result["unexpected_signers"] = False
        result["user_privy_grant_verified"] = True
    return result


async def get_eth_user_grant_review(did: str) -> dict:
    did = _safe_did(did)
    user = await fetch_privy_user(did)
    try:
        owned = _owned_eth_wallet(user, did)
    except ValueError:
        return _inert_grant_result()
    metadata = await fetch_privy_wallet_metadata(owned["wallet_id"])
    evidence = await get_eth_user_enrollment_preflight(did)
    # Stage 16 refuses to trust any existing signer. Here the full policy,
    # owner and additional signer are independently reverified.
    if (
        evidence.get("wallet_identity_verified") is not True
        or evidence.get("wallet_owned_by_authenticated_user") is not True
        or evidence.get("user_live_limits_verified") is not True
        or evidence.get("trading_paused") is not True
    ):
        return _inert_grant_result()
    owner, signer = _require_policy_roles()
    try:
        stored = await asyncio.to_thread(
            _read_policy_for_grant, did, wallet=owned["address"],
            owner=owner, signer=signer,
            revision=evidence["live_limits_revision"],
        )
    except HTTPException:
        return _inert_grant_result()
    remote = await _fetch_policy(stored["policy_id"])
    return assess_privy_user_grant(
        did=did, user=user, metadata=metadata, stored=stored,
        remote=remote, evidence=evidence, owner=owner, signer=signer,
    )


def _confirm_grant_locked(cur, *, did: str, expected: dict) -> dict:
    cur.execute(
        """SELECT trading_paused,revision FROM mininex_live_eth_limits
           WHERE privy_did=%s FOR UPDATE""", (did,),
    )
    live = cur.fetchone()
    if (type(live) is not dict or live.get("trading_paused") is not True
            or live.get("revision") != expected["limits_revision"]):
        raise HTTPException(409, "LIVE limits changed during Privy permission review")
    cur.execute(
        """SELECT status,limits_revision,policy_owner_id,execution_signer_id,
                  wallet_address,confirmed_at,revoked_at
           FROM mininex_live_eth_consent_intents
           WHERE privy_did=%s FOR UPDATE""", (did,),
    )
    consent = cur.fetchone()
    if (
        type(consent) is not dict
        or consent.get("status") != "SIGNED_INTENT"
        or consent.get("confirmed_at") is None
        or consent.get("revoked_at") is not None
        or any(consent.get(k) != expected[k] for k in (
            "wallet_address","limits_revision","policy_owner_id",
            "execution_signer_id",
        ))
    ):
        raise HTTPException(409, "Signer intent is no longer authorized")
    cur.execute(
        """SELECT policy_id,status,wallet_address,limits_revision,
                  policy_owner_id,execution_signer_id,candidate_sha256,
                  remote_policy_sha256
           FROM mininex_live_eth_policy_enrollments
           WHERE privy_did=%s FOR UPDATE""", (did,),
    )
    row = cur.fetchone()
    if (
        type(row) is not dict
        or row.get("status") not in ("REMOTE_VERIFIED_UNATTACHED","GRANT_VERIFIED")
        or any(row.get(k) != expected[k] for k in (
            "policy_id","wallet_address","limits_revision",
            "policy_owner_id","execution_signer_id","candidate_sha256",
        ))
        or row.get("remote_policy_sha256") != expected["candidate_sha256"]
    ):
        raise HTTPException(409, "Privy signer policy changed during confirmation")
    if row["status"] == "REMOTE_VERIFIED_UNATTACHED":
        cur.execute(
            """UPDATE mininex_live_eth_policy_enrollments
               SET status='GRANT_VERIFIED',updated_at=now()
               WHERE privy_did=%s AND status='REMOTE_VERIFIED_UNATTACHED'
               RETURNING status""", (did,),
        )
        updated = cur.fetchone()
        if type(updated) is not dict or updated.get("status") != "GRANT_VERIFIED":
            raise HTTPException(409, "Concurrent signer-grant change")
    return {
        "mode":"ETH_PRIVY_USER_GRANT_VERIFIED_READ_ONLY",
        "status":"GRANT_VERIFIED",
        "policy_id":expected["policy_id"],
        "signer_policy_exactly_verified":True,
        "execution_enabled":False, "can_submit":False,
    }


def _commit_grant(did: str, expected: dict) -> dict:
    try:
        url = _db_url()
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                return _confirm_grant_locked(cur, did=did, expected=expected)
    except psycopg.Error:
        raise HTTPException(503, "Signer-grant ledger unavailable") from None


async def confirm_eth_user_grant(did: str) -> dict:
    did = _safe_did(did)
    # Must NEVER accept frontend-reported signer identity or policy ID.
    review = await get_eth_user_grant_review(did)
    if review["policy_verified"] is not True or review["user_privy_grant_verified"] is not True:
        raise HTTPException(409, "Exact policy-scoped signer is not verified by Privy")
    # Load the fresh user-specific verified wallet and limits, then lock
    # and re-evaluate the LIVE revision/consent/ledger in one DB transaction.
    evidence = await get_eth_user_enrollment_preflight(did)
    owner, signer = _require_policy_roles()
    stored = await asyncio.to_thread(
        _read_policy_for_grant, did, wallet=review["verified_wallet_address"],
        owner=owner, signer=signer, revision=evidence["live_limits_revision"],
    )
    expected = {
        "policy_id":stored["policy_id"],
        "candidate_sha256":stored["candidate_sha256"],
        "wallet_address":stored["wallet_address"],
        "limits_revision":stored["limits_revision"],
        "policy_owner_id":owner, "execution_signer_id":signer,
    }
    return await asyncio.to_thread(_commit_grant, did, expected)
