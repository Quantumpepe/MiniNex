"""Real original Solana Privy sign-only RPC, internal use after owner grant.

Unlike the offline preview, this module can ask the authentic Privy wallet to
sign a prepared Solana mainnet transfer. It NEVER broadcasts by itself and
does not expose HTTP endpoints, grant policies or start autonomous sessions.

Only a trusted worker with current owner consent, immutable order journal,
exact per-wallet policy, registered backend quorum and reviewed budget may
call this module. It independently re-reads the original wallet metadata and
cryptographically validates the signed returned transaction. The shared native
dispatcher commits one irreversible SENDING claim before any broadcast.
"""
from __future__ import annotations

import base64
import binascii
import os
import re

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException

from .eth_privy_p256_signer import _canonical_authorization_payload
from .native_privy_owner_signer import (
    _original_privy_native_wallet, inspect_original_native_signer_metadata,
)
from .native_privy_unsigned_signing_requests import (
    preview_privy_sol_sign_transaction_request,
)
from .native_sol_privy_signed_readback import inspect_original_sol_signed_transaction
from .privy_live_signer import fetch_privy_wallet_metadata
from .privy_wallet_verification import APP_ID, fetch_privy_user
from .native_live_signed_dispatch import dispatch_signed_original_native_once
from .storage import _safe_did

_ID = re.compile(r"^[A-Za-z0-9_-]{6,160}$")
_KEY = re.compile(r"^[a-z0-9]{64}$")
_RPC = "https://api.privy.io/v1/wallets/"
_PRIVATE_ENV = "MININEX_PRIVY_SOL_AUTH_P256_PKCS8"
_PUBLIC_ENV = "MININEX_PRIVY_SOL_SIGNER_PUBLIC_KEY_SPKI"
_QUORUM_ENV = "MININEX_PRIVY_SOL_SIGNER_ID"


def _signed_privy_sol_authorization(url: str, body: dict, key: str) -> str:
    """Sign the exact RPC payload using the separately pinned SOL backend key."""
    private64 = os.getenv(_PRIVATE_ENV, "").strip()
    public64 = os.getenv(_PUBLIC_ENV, "").strip()
    if not private64 or not public64 or len(private64) > 4096 or len(public64) > 2048:
        raise HTTPException(503, "SOL Privy authorization key pair not configured")
    try:
        private = serialization.load_der_private_key(
            base64.b64decode(private64, validate=True), password=None)
        public = serialization.load_der_public_key(
            base64.b64decode(public64, validate=True))
    except (ValueError, TypeError, binascii.Error):
        raise HTTPException(503, "SOL Privy authorization key malformed") from None
    if (not isinstance(private, ec.EllipticCurvePrivateKey)
            or not isinstance(private.curve, ec.SECP256R1)
            or not isinstance(public, ec.EllipticCurvePublicKey)
            or not isinstance(public.curve, ec.SECP256R1)
            or private.public_key().public_numbers() != public.public_numbers()):
        raise HTTPException(503, "SOL signer private key differs from pinned quorum member")
    payload = _canonical_authorization_payload(url=url, body=body, key=key)
    return base64.b64encode(private.sign(
        payload, ec.ECDSA(hashes.SHA256()))).decode("ascii")


def _signed_sol_permission(audit: object, *, did: str, intent: str,
                           owner: str, wallet_id: str,
                           signer_id: str, policy_id: str) -> None:
    """Permission must come from a current independent DB+Privy grant audit."""
    if (type(audit) is not dict
            or audit.get("audit_mode") != "CURRENT_NATIVE_OWNER_SIGNED_GRANT"
            or audit.get("did") != did
            or audit.get("asset") != "SOL"
            or audit.get("intent_key") != intent
            or audit.get("source_wallet") != owner
            or audit.get("wallet_id") != wallet_id
            or audit.get("signer_id") != signer_id
            or audit.get("policy_id") != policy_id
            or audit.get("owner_wallet_current") is not True
            or audit.get("exact_remote_policy_verified") is not True
            or audit.get("backend_quorum_current") is not True
            or audit.get("owner_signed_budget_current") is not True
            or audit.get("user_consent_current") is not True
            or type(audit.get("budget_revision")) is not int
            or audit["budget_revision"] < 1
            or type(audit.get("policy_sha256")) is not str
            or not _KEY.fullmatch(audit["policy_sha256"])
            or type(audit.get("owner_consent_sha256")) is not str
            or not _KEY.fullmatch(audit["owner_consent_sha256"])):
        raise HTTPException(409, "Exact owner SOL signer consent and grant absent")


async def sign_original_sol_with_privy_once(
    did: str, *, original_message: dict, intent_key: str,
    permission_audit: dict, client: httpx.AsyncClient,
) -> dict:
    """One REAL Privy sign-only request; returned wire is INTERNAL, not an API."""
    did = _safe_did(did)
    if type(intent_key) is not str or not re.fullmatch(r"native_[0-9a-f]{64}", intent_key):
        raise ValueError("Invalid original SOL order identity")
    owner = original_message.get("source_wallet") if type(original_message) is dict else None
    user = await fetch_privy_user(did)
    linked = _original_privy_native_wallet(user, did, "SOL")
    if linked["address"] != owner or linked["delegated"] is not True:
        raise HTTPException(409, "Original SOL wallet not delegated by owner")
    remote = await fetch_privy_wallet_metadata(linked["wallet_id"])
    state = inspect_original_native_signer_metadata(
        did=did, asset="SOL", linked=user, remote=remote)
    if (state.get("original_wallet_remote_owner_verified") is not True
            or state.get("original_public_key_matches_wallet") is not True
            or state.get("privy_native_wallet_api_supported") is not True):
        raise HTTPException(409, "Original SOL signer ownership cannot be proven")
    signer_id = os.getenv(_QUORUM_ENV, "").strip()
    policy_id = permission_audit.get("policy_id") if type(permission_audit) is dict else None
    if (not _ID.fullmatch(signer_id) or type(policy_id) is not str
            or not _ID.fullmatch(policy_id) or signer_id == policy_id):
        raise HTTPException(409, "Original SOL signer quorum or policy missing")
    signers = remote.get("additional_signers")
    if (type(signers) is not list or len([
            x for x in signers if type(x) is dict
            and x.get("signer_id") == signer_id
            and x.get("override_policy_ids") == [policy_id]
        ]) != 1):
        raise HTTPException(409, "Privy has not attached exact SOL owner signer")
    _signed_sol_permission(
        permission_audit, did=did, intent=intent_key,
        owner=owner, wallet_id=linked["wallet_id"],
        signer_id=signer_id, policy_id=policy_id,
    )
    preview = preview_privy_sol_sign_transaction_request(
        original_message, owner_public_key_hex=remote["public_key"],
        wallet_id=linked["wallet_id"], intent_key=intent_key,
    )
    url = _RPC + linked["wallet_id"] + "/rpc"
    body = {"method": preview["method"], "params": preview["params"]}
    key = preview["idempotency_key"]
    signature = _signed_privy_sol_authorization(url, body, key)
    secret = os.getenv("MININEX_PRIVY_APP_SECRET", "").strip()
    if len(secret) < 12:
        raise HTTPException(503, "Privy API secret not configured")
    # Never auto-retry a sign RPC timeout: the same immutable request may
    # already have produced a signature. It has NOT broadcast funds.
    try:
        response = await client.post(
            url, auth=(APP_ID, secret), json=body,
            headers={"privy-app-id": APP_ID,
                     "privy-idempotency-key": key,
                     "privy-authorization-signature": signature,
                     "Content-Type": "application/json"})
        response.raise_for_status()
        if len(response.content) > 25000:
            raise ValueError("Oversized Privy SOL signature response")
        payload = response.json()
        data = payload.get("data") if type(payload) is dict else None
        if type(data) is not dict:
            raise ValueError("Unrecognized Privy SOL sign response")
        wire = data.get("signed_transaction")
        if type(wire) is not str:
            raise ValueError("Privy did not return signed SOL bytes")
        checked = inspect_original_sol_signed_transaction(
            original_message, signed_transaction_base64=wire)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(
            503, "Original SOL Privy signature unavailable or unverifiable") from None
    return {
        "mode": "SIGNED_ORIGINAL_SOL_PRIVY_INTERNAL_ONLY",
        "asset": "SOL",
        "intent_key": intent_key,
        "source_wallet": owner,
        "original_message_sha256": original_message["message_sha256"],
        "signed_transaction_base64": wire,
        "signed_wire_sha256": checked["signed_wire_sha256"],
        "original_owner_signature_verified": True,
        "wallet_signed": True,
        "source_transaction_sent": False,
        "destination_settled": False,
    }


async def sign_and_dispatch_original_sol_sell_once(
    did: str, *, intent_key: str, quote: dict, manifest: dict,
    permission_audit: dict, client: httpx.AsyncClient,
) -> dict:
    """Connect REAL Privy sign-only to durable single-send SOL mainnet dispatcher.

    The independent worker must obtain quote, manifest and audit from private
    server records. No public endpoint, startup task or scheduler calls this.
    """
    signed = await sign_original_sol_with_privy_once(
        did, original_message=manifest, intent_key=intent_key,
        permission_audit=permission_audit, client=client)
    return await dispatch_signed_original_native_once(
        did, asset="SOL", intent_key=intent_key, quote=quote,
        manifest=manifest,
        signed_wire_base64=signed["signed_transaction_base64"],
        permission_audit=permission_audit)
