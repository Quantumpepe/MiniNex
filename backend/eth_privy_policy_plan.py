"""Stage 19: automatically prepare PER-USER Privy policy data, read-only.

This module bridges verified Privy wallet ownership, separate LIVE risk caps,
short-lived independently retrieved market prices, the two Privy quorums and
the already audited 6-rule ETH policy blueprint.

IMPORTANT:
- A price snapshot is NEVER signing authorization or a verified on-chain
  executable quote. It is used for a *preview* ceiling only.
- A user's saved budget and EIP-191 signed INTENT are NOT an additional-signer
  delegation. A future separate wallet-native Privy grant must be required.
- No POST to Privy, no policy mutation, signer enrollment, allowance,
  transaction or remote revocation is implemented here.
- No frontend body supplies wallet, DID, caps, prices, owners or policy ID.
- Never trust this preview as a live execution readiness signal.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR, InvalidOperation
import hashlib
import json
import logging
import os
import time

import httpx
from fastapi import HTTPException

from .eth_privy_user_policy_blueprint import (
    build_eth_privy_user_policy_blueprint,
    inspect_eth_user_policy_blueprint,
)
from .eth_user_enrollment_preflight import get_eth_user_enrollment_preflight
from .eth_user_consent import _require_enrollment_evidence, _require_policy_roles
from .storage import _safe_did

LOG = logging.getLogger("mininex.eth_privy_policy_plan")

# Public token prices; NEVER infer an exact live trade quote from these.
COINGECKO_SIMPLE_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
_PRICE_ASSETS = ("ethereum", "usd-coin", "tether")
_MAX_PRICE_AGE_SEC = 90
_POLICY_DRAFT_LIFETIME_SEC = 24 * 3600
_TOKENS = {"ETH": "ethereum", "USDC": "usd-coin", "USDT": "tether"}


def _parsed_price(payload: object, symbol: str, now: int) -> Decimal:
    if type(payload) is not dict:
        raise ValueError("Unrecognized reference price snapshot")
    raw = payload.get(_TOKENS[symbol])
    if type(raw) is not dict:
        raise ValueError("Missing market reference")
    timestamp = raw.get("last_updated_at")
    usd = raw.get("usd")
    if (type(timestamp) is not int or timestamp > now + 5
            or now - timestamp > _MAX_PRICE_AGE_SEC
            or type(usd) not in (str, int, float, Decimal)
            or isinstance(usd, bool)):
        raise ValueError("Reference price missing or stale")
    try:
        value = Decimal(str(usd))
    except (InvalidOperation, ValueError):
        raise ValueError("Bad reference price") from None
    if not value.is_finite():
        raise ValueError("Bad reference price")
    if symbol == "ETH":
        if not Decimal("100") <= value <= Decimal("1000000"):
            raise ValueError("ETH reference price outside sanity bounds")
    else:
        # A reference price significantly off peg requires manual review.
        if not Decimal("0.99") <= value <= Decimal("1.01"):
            raise ValueError("Stablecoin reference off peg")
    return value


def derive_policy_atomic_caps(
    *, max_order_usd: str, snapshot: object, now: int,
) -> dict:
    """Convert one verified LIVE order ceiling to exact atomic draft caps.

    The derived values are not cumulative and do not limit daily turnover.
    Runtime spend verification MUST still re-quote, re-lock the current LIVE
    limits and account for gas, balances and pending orders.
    """
    if type(now) is not int or now <= 0:
        raise ValueError("Bad timestamp")
    if type(max_order_usd) is not str:
        raise ValueError("Exact verified LIVE order amount required")
    try:
        order = Decimal(max_order_usd)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Bad verified per-order cap") from None
    if (not order.is_finite() or order <= 0
            or order >= Decimal("10000000")
            or order.as_tuple().exponent < -6):
        raise ValueError("Invalid policy order cap")
    rates = {k: _parsed_price(snapshot, k, now) for k in _TOKENS}
    factor = Decimal(10) ** 18
    weth = int((order / rates["ETH"] * factor).to_integral_value(
        rounding=ROUND_FLOOR))
    if weth <= 0:
        raise ValueError("ETH cap too small")
    caps = {
        "usdc_max_input_micro": str(int(
            (order / rates["USDC"] * 1_000_000).to_integral_value(
                rounding=ROUND_FLOOR))),
        "usdt_max_input_micro": str(int(
            (order / rates["USDT"] * 1_000_000).to_integral_value(
                rounding=ROUND_FLOOR))),
        "weth_max_input_wei": str(weth),
        "native_wrap_max_wei": str(weth),
    }
    if any(int(value) <= 0 for value in caps.values()):
        raise ValueError("Policy cap too small")
    return caps


async def fetch_reference_prices() -> dict:
    """Read the fixed external HTTPS reference price URL, no client targets."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(7.0), follow_redirects=False,
        ) as client:
            response = await client.get(
                COINGECKO_SIMPLE_PRICE_URL,
                params={
                    "ids": ",".join(_PRICE_ASSETS),
                    "vs_currencies": "usd",
                    "include_last_updated_at": "true",
                },
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            if len(response.content) > 24_000:
                raise ValueError("Oversized price snapshot")
            payload = response.json()
            if type(payload) is not dict:
                raise ValueError("Unrecognized quote payload")
            return payload
    except (httpx.HTTPError, ValueError, TypeError):
        LOG.warning("ETH reference price snapshot unavailable")
        raise HTTPException(503, "Fresh ETH/stablecoin references unavailable") from None


def build_verified_eth_policy_candidate(
    *, evidence: dict, snapshot: dict, owner: str, executor: str, now: int,
) -> dict:
    """Private deterministic *candidate* builder. Never a Privy API write.

    'evidence' must be freshly obtained from the server's own Privy/DB GET
    path; 'snapshot' must come from our own HTTPS fetch. The function is also
    independently unit-testable with synthetic trusted fixtures.
    """
    values = _require_enrollment_evidence(evidence)
    if (type(owner) is not str or type(executor) is not str
            or not owner or not executor or owner == executor):
        raise HTTPException(409, "Independent policy owner and signer required")
    caps = derive_policy_atomic_caps(
        max_order_usd=values["order"], snapshot=snapshot, now=now,
    )
    params = {
        "wallet": values["wallet"],
        "policy_owner_id": owner,
        "executor_signer_id": executor,
        **caps,
        "expires_at": now + _POLICY_DRAFT_LIFETIME_SEC,
        "now": now,
    }
    candidate = build_eth_privy_user_policy_blueprint(**params)
    inspection = inspect_eth_user_policy_blueprint(candidate, **params)
    if (inspection["policy_draft_matches_expected"] is not True
            or inspection["wallet_bound"] is not True):
        raise HTTPException(503, "ETH policy candidate validation failed")
    digest = hashlib.sha256(json.dumps(
        candidate, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return {
        "policy": candidate,
        "snapshot": {
            "wallet": values["wallet"],
            "limits_revision": values["revision"],
            "max_order_usd": values["order"],
            "max_daily_turnover_usd": values["daily"],
            "capital_usd": values["capital"],
            "reference_price_source": "CoinGecko read-only spot",
            "caps": caps,
            "expires_at_unix": now + _POLICY_DRAFT_LIFETIME_SEC,
            "sha256": digest,
        },
    }


async def get_eth_policy_candidate_preflight(did: str) -> dict:
    """Authenticated user-visible report, with NO reusable policy body.

    Missing settings and stale prices produce blocked diagnostics, never
    an unsafe fallback or a change to user wallet permissions.
    """
    did = _safe_did(did)
    evidence = await get_eth_user_enrollment_preflight(did)
    base = {
        "mode": "ETH_USER_POLICY_CANDIDATE_PREVIEW_ONLY",
        "asset": "ETH",
        "chain_id": 1,
        "wallet_verified": evidence.get("wallet_identity_verified") is True,
        "live_limits_verified": evidence.get("user_live_limits_verified") is True,
        "policy_owner_independent": evidence.get("policy_owner_independent") is True,
        "policy_candidate_valid": False,
        "policy_candidate_sha256": None,
        "live_limits_revision": evidence.get("live_limits_revision"),
        "max_order_usd": evidence.get("live_max_order_usd"),
        "policy_created_in_privy": False,
        "user_privy_delegation_verified": False,
        "remote_revocation_implemented": False,
        "allowances_implemented": False,
        "onchain_quote_verified": False,
        "can_grant_permission": False,
        "can_submit": False,
        "execution_enabled": False,
    }
    # Never fetch network prices when account is not independently ready.
    try:
        _require_enrollment_evidence(evidence)
        owner, executor = _require_policy_roles()
    except HTTPException:
        return base
    prices = await fetch_reference_prices()
    now = int(time.time())
    try:
        plan = build_verified_eth_policy_candidate(
            evidence=evidence, snapshot=prices, owner=owner,
            executor=executor, now=now,
        )
    except (HTTPException, ValueError):
        return base
    base["policy_candidate_valid"] = True
    base["policy_candidate_sha256"] = plan["snapshot"]["sha256"]
    base["policy_candidate_expiration_unix"] = plan["snapshot"]["expires_at_unix"]
    base["reference_price_source"] = "CoinGecko"
    return base
