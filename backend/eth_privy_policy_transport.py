"""Stage 20: Privy policy HTTP transport and strict remote read-back verifier.

No user-facing API route calls this module's POST helper. The public
provisioning entrypoint is intentionally disabled until a future worker
satisfies ALL gates: independently proven wallet ownership, policy-owner
authorization, user's explicit *Privy* signer approval, revoked-grant
handling, durable idempotency & amount/quote protection. An EIP-191 signed
MiniNex *intent* is not a Privy delegation.

The low-level helper is unit-tested with an httpx mock transport. Production
code cannot invoke a POST or grant today. No keys, network requests or
wallet mutations happen merely by importing the module.

Privy documentation:
https://docs.privy.io/controls/policies/create-a-policy
https://docs.privy.io/controls/policies/get-a-policy
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import logging
import os
import re

import httpx
from fastapi import HTTPException

from .eth_privy_user_policy_blueprint import inspect_eth_user_policy_blueprint
from .privy_wallet_verification import APP_ID

LOG = logging.getLogger("mininex.privy_policy_transport")
_URL = "https://api.privy.io/v1/policies"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,160}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_MAX_BODY = 256_000

# This is a compile-time hard stop, not a toggle controlled by a JWT,
# request body, paper settings, or a Render environment flag.
_REMOTE_PROVISIONING_ACTIVATED = False


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")).hexdigest()


def _validate_candidate(
    policy: object, *,
    did: str, wallet: str, policy_owner_id: str,
    executor_signer_id: str, limits_revision: int,
    usdc_max_input_micro: str, usdt_max_input_micro: str,
    weth_max_input_wei: str, native_wrap_max_wei: str,
    expires_at: int, now: int,
) -> dict:
    """Reject all extra/broadened ALLOW rules before assembling a POST.

    A future worker MUST obtain these fields from fresh, independently
    verified backend/Privy and live-limit records, never from client JSON.
    """
    if (type(did) is not str or not did.startswith("did:privy:")
            or len(did) > 256 or type(wallet) is not str
            or not _ADDR.fullmatch(wallet)
            or type(limits_revision) is not int or limits_revision < 0):
        raise ValueError("Verified DID, wallet and limits revision required")
    inspection = inspect_eth_user_policy_blueprint(
        policy, wallet=wallet, policy_owner_id=policy_owner_id,
        executor_signer_id=executor_signer_id,
        usdc_max_input_micro=usdc_max_input_micro,
        usdt_max_input_micro=usdt_max_input_micro,
        weth_max_input_wei=weth_max_input_wei,
        native_wrap_max_wei=native_wrap_max_wei,
        expires_at=expires_at, now=now,
    )
    if (inspection.get("policy_draft_matches_expected") is not True
            or inspection.get("wallet_bound") is not True
            or inspection.get("policy_owner_separate_from_executor") is not True):
        raise ValueError("Privy policy candidate differs from reviewed wallet limits")

    # Bind idempotency to the exact one-user/wallet/revision/policy bytes.
    # Privy's server idempotency scope is time-bounded; permanent duplicate
    # prevention MUST also happen in a durable DB before enabling POST.
    envelope = {
        "did": did,
        "wallet": wallet.lower(),
        "revision": limits_revision,
        "candidate_sha256": _digest(policy),
    }
    key = _digest(envelope)
    return {
        "candidate": deepcopy(policy),
        "idempotency_key": key,
        "candidate_sha256": envelope["candidate_sha256"],
    }


def verify_remote_privy_policy(remote: object, expected: object) -> dict:
    """Fail closed if ANY remote rule, condition, owner or method differs.

    Privy GET responses add generated policy/rule IDs and timestamps.
    These identity fields are not permissions and are removed solely from
    the comparison. No other rule mutation or permissive extra rule is
    accepted. Raw address case is preserved (Privy is case-sensitive).
    """
    if type(expected) is not dict or type(remote) is not dict:
        raise ValueError("Malformed policy verification inputs")
    if set(expected) != {"name", "version", "chain_type", "owner_id", "rules"}:
        raise ValueError("Unreviewed policy request shape")
    policy_id = remote.get("id")
    if type(policy_id) is not str or _ID_RE.fullmatch(policy_id) is None:
        raise ValueError("Missing Privy policy identifier")
    if any(remote.get(k) != expected[k] for k in (
        "name", "version", "chain_type", "owner_id",
    )):
        raise ValueError("Privy policy owner or metadata changed")
    stored = remote.get("rules")
    submitted = expected["rules"]
    if (type(stored) is not list or type(submitted) is not list
            or len(stored) != len(submitted) or len(stored) != 6):
        raise ValueError("Privy policy rule count mismatch")
    ids = set()
    normalized = []
    for rule in stored:
        if type(rule) is not dict:
            raise ValueError("Malformed Privy policy rule")
        rid = rule.get("id")
        if type(rid) is not str or not _ID_RE.fullmatch(rid) or rid in ids:
            raise ValueError("Missing or duplicate Privy rule ID")
        ids.add(rid)
        if set(rule) != {
            "id", "name", "method", "action", "conditions",
        }:
            raise ValueError("Unexpected Privy policy rule properties")
        if (type(rule.get("conditions")) is not list
                or any(type(c) is not dict for c in rule["conditions"])):
            raise ValueError("Malformed Privy policy condition")
        normalized.append({
            k: deepcopy(rule[k])
            for k in ("name", "method", "action", "conditions")
        })
    if normalized != submitted:
        raise ValueError("Remote Privy rules are not the reviewed exact template")
    return {
        "policy_id": policy_id,
        "policy_sha256": _digest(expected),
        "remote_exact_match": True,
        "wallet_signer_attached": False,
        "user_delegation_verified": False,
        "can_sign": False,
        "can_submit": False,
        "execution_enabled": False,
    }


def _auth() -> tuple[str, str]:
    secret = os.getenv("MININEX_PRIVY_APP_SECRET", "").strip()
    if not secret:
        raise HTTPException(503, "Privy server secret not configured")
    return APP_ID, secret


async def _post_and_readback_unattached(
    *, validated: dict, client: httpx.AsyncClient,
) -> dict:
    """Transport primitive ONLY. Never exposed as a deployed API handler.

    Single POST attempt, then independent GET of the returned policy.
    An uncertain POST outcome is NOT retried, to avoid duplicate policies.
    A future DB-backed worker must persist SUBMITTING/UNCERTAIN states
    before any call and never repeat a timed-out creation blindly.
    """
    if (type(validated) is not dict
            or set(validated) != {
                "candidate", "idempotency_key", "candidate_sha256",
            }
            or type(validated["idempotency_key"]) is not str
            or _SHA_RE.fullmatch(validated["idempotency_key"]) is None
            or validated["candidate_sha256"] != _digest(validated["candidate"])):
        raise ValueError("Unreviewed Privy request")
    auth = _auth()
    headers = {
        "privy-app-id": APP_ID,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "privy-idempotency-key": validated["idempotency_key"],
    }
    try:
        response = await client.post(
            _URL, json=validated["candidate"], auth=auth, headers=headers,
        )
        response.raise_for_status()
        if len(response.content) > _MAX_BODY:
            raise ValueError("Oversized Privy create response")
        created = response.json()
        policy_id = created.get("id") if type(created) is dict else None
        if type(policy_id) is not str or _ID_RE.fullmatch(policy_id) is None:
            raise ValueError("Missing new Privy policy ID")

        # GET from Privy is authoritative; a successful POST is insufficient.
        fetched = await client.get(
            _URL + "/" + policy_id,
            auth=auth,
            headers={"privy-app-id": APP_ID, "Accept": "application/json"},
        )
        fetched.raise_for_status()
        if len(fetched.content) > _MAX_BODY:
            raise ValueError("Oversized Privy policy readback")
        remote = fetched.json()
        if type(remote) is not dict or remote.get("id") != policy_id:
            raise ValueError("Privy GET/POST identity mismatch")
        return verify_remote_privy_policy(remote, validated["candidate"])
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        LOG.warning("Unattached Privy policy POST/readback uncertain; never auto-retry")
        raise HTTPException(
            503, "Privy policy creation verification uncertain; reconcile manually",
        ) from None


def provision_eth_privy_policy(*args: object, **kwargs: object) -> None:
    """Hard-disabled production entrypoint until a verified enrollment worker.

    In particular, authenticated GET /policy-candidate and signed EIP-191
    enrollment intent do NOT activate policy writes or wallet delegation.
    """
    raise HTTPException(
        503,
        "Privy policy creation and wallet delegation are not activated",
    )
