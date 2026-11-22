"""Exact policy-owner P-256 auth signature for one scoped EVM policy PATCH.

The original Privy policy owner, *not* the additional trading signer,
authorizes a policy update. This primitive reuses an existing pinned P-256
authorization key only when its public member is independently verified
in the configured Policy Owner quorum. It NEVER sends a network request.

Privy request signature schema:
https://docs.privy.io/controls/authorization-keys/using-owners/sign/overview

No unrestricted Allow-all action, owner reassignment, wallet export,
additional signer creation, or direct transaction signing is accepted.
"""
from __future__ import annotations

import base64
import json
import re

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from .eth_privy_p256_signer import (
    _read_valid_p256_private_key,
    _configured_quorum_public_key,
    _private_key_matches_public_pin,
)
from .privy_wallet_verification import APP_ID

_POLICY_URL=re.compile(
    r"^https://api\.privy\.io/v1/policies/[A-Za-z0-9_-]{6,160}$"
)
_SHA=re.compile(r"^[0-9a-f]{64}$")
_FIELDS=frozenset(("name","method","action","conditions"))


def exact_owner_policy_patch_body(candidate:object)->dict:
    """Only same-name, same-owner policies whose rules are individually strict.

    The trusted caller must compare this candidate to the signed original
    wallet scopes and their backend-locked budget snapshots; this alone is
    never a sufficient authorization to update a Privy resource.
    """
    if (type(candidate) is not dict
            or set(candidate)!={
                "version","chain_type","name","owner_id","rules"
            } or candidate.get("version")!="1.0"
            or candidate.get("chain_type")!="ethereum"
            or type(candidate.get("name")) is not str
            or not 1 <= len(candidate["name"]) <= 50
            or type(candidate.get("rules")) is not list
            or not candidate["rules"]):
        raise ValueError("Unreviewed full wallet EVM policy")
    names=set()
    for rule in candidate["rules"]:
        if (type(rule) is not dict or set(rule)!=_FIELDS
                or type(rule.get("name")) is not str
                or rule["name"] in names
                or rule.get("method")!="eth_sendTransaction"
                or rule.get("action")!="ALLOW"
                or type(rule.get("conditions")) is not list
                or not rule["conditions"]
                or any(type(c) is not dict for c in rule["conditions"])):
            raise ValueError("Unbounded or ambiguous Privy policy rule")
        names.add(rule["name"])
    # Never change or remove policy ownership as a side effect of adding coin.
    return {"name":candidate["name"],"rules":candidate["rules"]}


def owner_policy_patch_signature_payload(
    *,url:str,body:dict,idempotency_key:str,expiry_unix_ms:int,
)->bytes:
    """RFC8785-equivalent canonical JSON for these constrained ASCII fields."""
    if (type(url) is not str or not _POLICY_URL.fullmatch(url)
            or type(idempotency_key) is not str or not _SHA.fullmatch(idempotency_key)
            or type(expiry_unix_ms) is not int or expiry_unix_ms<=0
            or type(body) is not dict or set(body)!={"name","rules"}):
        raise ValueError("Invalid policy-owner PATCH authorization context")
    payload={
        "version":1,
        "method":"PATCH",
        "url":url,
        "body":body,
        "headers":{
            "privy-app-id":APP_ID,
            "privy-idempotency-key":idempotency_key,
            "privy-request-expiry":str(expiry_unix_ms),
        },
    }
    raw=json.dumps(
        payload,sort_keys=True,separators=(",",":"),
        ensure_ascii=False,allow_nan=False,
    ).encode("utf-8")
    if not raw.isascii():
        raise ValueError("Unexpected non-ASCII Privy authorization context")
    return raw


def sign_exact_privy_owner_patch(
    *,url:str,candidate:dict,idempotency_key:str,
    expiry_unix_ms:int,owner_quorum_verified:bool,
)->dict:
    """Create signature ONLY after independent Policy Owner quorum readback.

    The returned signature MUST be sent only with the exact URL, method,
    body, idempotency and expiry headers used to construct it. This does
    not post a policy and does not prove wallet owner consent by itself.
    """
    if owner_quorum_verified is not True:
        raise ValueError("Existing Privy Policy Owner quorum not verified")
    body=exact_owner_policy_patch_body(candidate)
    payload=owner_policy_patch_signature_payload(
        url=url,body=body,idempotency_key=idempotency_key,
        expiry_unix_ms=expiry_unix_ms,
    )
    private=_read_valid_p256_private_key()
    pin=_configured_quorum_public_key()
    if not _private_key_matches_public_pin(private,pin):
        raise ValueError("Configured P-256 key cannot authorize Policy Owner")
    signature=base64.b64encode(
        private.sign(payload,ec.ECDSA(hashes.SHA256())),
    ).decode("ascii")
    return {
        "url":url,
        "body":body,
        "headers":{
            "privy-app-id":APP_ID,
            "privy-idempotency-key":idempotency_key,
            "privy-request-expiry":str(expiry_unix_ms),
            "privy-authorization-signature":signature,
            "Accept":"application/json",
            "Content-Type":"application/json",
        },
        "signature_owner_quorum_verified":True,
        "request_sent":False,
    }
