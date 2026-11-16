"""Read-only independent Privy Backend-Signer quorum member verification.

Only GETs the configured quorum and compares its exact sole public P-256
authorization key against the private/public pair held in Render environment.
A matching quorum proves neither owner wallet delegation nor trade authority.
No secret, public key or signature bytes are returned to app users.
"""
from __future__ import annotations

import base64
import binascii
import os
import re

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .eth_privy_p256_signer import (
    inspect_privy_p256_key_configuration,
    _configured_quorum_public_key,
)
from .privy_wallet_verification import APP_ID

_QUORUM_ID = re.compile(r"^[A-Za-z0-9_-]{6,160}$")
_URL_BASE = "https://api.privy.io/v1/key_quorums/"
_MAX_RESPONSE_BYTES = 16_384


def _parse_public_key(raw: object) -> ec.EllipticCurvePublicKey | None:
    """Privy public authorization keys use base64-encoded DER/SPKI."""
    if type(raw) is not str or not raw or len(raw) > 2048:
        return None
    try:
        decoded = base64.b64decode(raw, validate=True)
        public = serialization.load_der_public_key(decoded)
        if (isinstance(public, ec.EllipticCurvePublicKey)
                and isinstance(public.curve, ec.SECP256R1)):
            return public
    except (ValueError, TypeError, binascii.Error):
        return None
    return None


def inspect_remote_privy_backend_quorum(
    payload: object, *, expected_id: str,
) -> bool:
    """Require exactly one registered key and a 1-of-1 authorization threshold.

    Reject unexpected/ambiguous membership, missing fields and mismatched
    public points. A valid local key pair alone cannot satisfy this.
    """
    local = inspect_privy_p256_key_configuration()
    pin = _configured_quorum_public_key()
    if (local["local_private_key_matches_configured_quorum_member"] is not True
            or pin is None
            or type(expected_id) is not str
            or _QUORUM_ID.fullmatch(expected_id) is None
            or type(payload) is not dict
            or payload.get("id") != expected_id
            or type(payload.get("authorization_threshold")) is not int
            or payload["authorization_threshold"] != 1):
        return False
    # A quorum can contain authorization keys, users *and nested quorums*.
    # A threshold of 1 does not mean our pinned key is the only authority
    # unless other member classes are explicitly absent as well.
    if (payload.get("user_ids") not in (None, [])
            or payload.get("key_quorum_ids") not in (None, [])):
        return False
    keys = payload.get("authorization_keys")
    if type(keys) is not list or len(keys) != 1 or type(keys[0]) is not dict:
        return False
    public = _parse_public_key(keys[0].get("public_key"))
    return public is not None and public.public_numbers() == pin.public_numbers()


async def check_privy_backend_quorum(
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Read back the registered quorum using server-only Privy credentials.

    Failure is a reported blocker, never a fallback to local-only trust.
    The result is deliberately limited to booleans for authenticated reports.
    """
    local = inspect_privy_p256_key_configuration()
    result = {
        "remote_quorum_get_succeeded": False,
        "remote_quorum_public_key_verified": False,
        "local_signer_key_pair_matches_pin":
            local["local_private_key_matches_configured_quorum_member"],
        "owner_wallet_delegation_verified": False,
        "live_execution_enabled": False,
    }
    quorum = os.getenv("MININEX_PRIVY_ETH_SIGNER_ID", "").strip()
    secret = os.getenv("MININEX_PRIVY_APP_SECRET", "").strip()
    if (not secret or not local["local_private_key_matches_configured_quorum_member"]
            or not _QUORUM_ID.fullmatch(quorum)):
        return result
    headers = {"privy-app-id": APP_ID, "Accept": "application/json"}
    url = _URL_BASE + quorum
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(7.0), follow_redirects=False,
        )
    try:
        response = await client.get(
            url, auth=(APP_ID, secret), headers=headers,
        )
        response.raise_for_status()
        if (len(response.content) > _MAX_RESPONSE_BYTES
                or not response.content):
            return result
        payload = response.json()
        if type(payload) is not dict:
            return result
        result["remote_quorum_get_succeeded"] = True
        result["remote_quorum_public_key_verified"] = (
            inspect_remote_privy_backend_quorum(payload, expected_id=quorum)
        )
    except (httpx.HTTPError, ValueError, TypeError):
        # Never expose endpoint response bodies or credentials in results.
        return result
    finally:
        if owns_client:
            await client.aclose()
    return result
