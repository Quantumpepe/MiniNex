"""Authenticated, read-only status for one EVM signer across user coins.

A row in the new wallet journal is NOT wallet authority. Report remote
readiness only after server-side verification of the exact original wallet,
its single additional signer, current per-coin budget revision, full Privy
policy and the configured P-256 quorum. This route NEVER signs, creates,
alters or attaches any Privy policy, and never starts a live trade.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from .eth_privy_remote_quorum import check_privy_backend_quorum
from .eth_privy_user_grant import _fetch_policy
from .privy_live_signer import fetch_privy_wallet_metadata
from .privy_wallet_verification import fetch_privy_user
from .shared_evm_wallet_policy_remote_review import (
    review_original_one_signer_wallet,
)
from .storage import _db_url, _ensure_schema, _safe_did

LOG = logging.getLogger("mininex.evm_wallet_one_signer_status")
_EVM_ASSETS = frozenset(("ETH","BNB","POL","AVAX","LINK","QNT","UNI","ARB","BTC","XRP"))
_VALID_STATES = frozenset((
    "DRAFT","SUBMITTING","UNCERTAIN","REMOTE_VERIFIED_UNATTACHED",
    "GRANT_VERIFIED","UPDATE_PENDING","UPDATING","UPDATE_UNCERTAIN","REVOKED",
))


def _inert_response(status: str = "NOT_CONFIGURED") -> dict:
    return {
        "mode": "MININEX_ORIGINAL_EVM_WALLET_POLICY_READONLY_STATUS",
        "status": status,
        "scope_coins": [],
        "settlement_policy_by_asset": {},
        "current_user_budgets_verified": False,
        "one_scoped_signer_verified": False,
        "remote_policy_exactly_verified": False,
        "backend_quorum_current": False,
        "verified_policy_sha256": None,
        "verified_wallet_id": None,
        "verified_signer_id": None,
        "policy_owner_quorum_current": False,
        "legacy_reconciliation_required": True,
        "privy_wallet_grant_changed": False,
        "new_policy_created": False,
        "can_submit": False,
        "auto_execution_enabled": False,
        "trade_sent": False,
    }


def _read_saved_and_current_preferences(did: str):
    url = _db_url()
    _ensure_schema(url)
    with psycopg.connect(url,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT wallet_id,wallet_address,owner_id,signer_id,
                          policy_id,policy_sha256,status,
                          scopes_json,revisions_json,
                          legacy_reconciliation_required
                   FROM mininex_live_evm_wallet_policy_grants
                   WHERE privy_did=%s""",
                (did,),
            )
            record = cur.fetchone()
            if record is None:
                return None, []
            if not isinstance(record,dict):
                raise ValueError("Invalid original wallet policy record")
            scopes = record.get("scopes_json")
            revisions = record.get("revisions_json")
            if (type(scopes) is not dict or type(revisions) is not dict
                    or not scopes or set(scopes) != set(revisions)
                    or not set(scopes).issubset(_EVM_ASSETS)):
                raise ValueError("Saved EVM budgets are not canonical")
            ordered = []
            for asset in sorted(scopes):
                scope = scopes[asset]
                revision = revisions[asset]
                if (type(scope) is not dict or scope.get("asset") != asset
                        or type(revision) is not int or revision <= 0):
                    raise ValueError("Stored original coin scope is invalid")
                ordered.append(scope)
            # Every stored revision must still match actual user configuration.
            # ETH has a separate original LIVE-capital table; it cannot be
            # inferred from generic per-coin preferences.
            coins = [row["asset"] for row in ordered if row["asset"] not in ("ETH","BTC")]
            if coins:
                cur.execute(
                    """SELECT asset,stable,budget_usd,revision
                       FROM mininex_extra_coin_preferences
                       WHERE privy_did=%s AND asset=ANY(%s)""",
                    (did,coins),
                )
                rows = cur.fetchall()
            else:
                rows = []
            actual = {row["asset"]:row for row in rows}
            if set(actual) != set(coins):
                raise ValueError("Original coin preferences missing")
            for asset in coins:
                saved = scopes[asset]
                current = actual[asset]
                if (type(current.get("revision")) is not int
                        or current["revision"] != revisions[asset]
                        or current.get("stable") != saved.get("stable")
                        or type(current.get("budget_usd")) is not int
                        or current["budget_usd"] <= 0):
                    raise ValueError("Original user coin budget changed")
            if "BTC" in scopes:
                cur.execute("""SELECT b.percent,b.revision,t.stable
                    FROM mininex_budget_preferences b JOIN mininex_trade_preferences t
                      ON t.privy_did=b.privy_did AND t.asset=b.asset
                    WHERE b.privy_did=%s AND b.asset='BTC'""",(did,))
                btc=cur.fetchone()
                if (type(btc) is not dict or type(btc.get("percent")) is not int
                        or not 0<btc["percent"]<=100
                        or btc.get("revision")!=revisions["BTC"]
                        or btc.get("stable")!=scopes["BTC"].get("stable")):
                    raise ValueError("Original BTC user budget changed")
            if "ETH" in scopes:
                cur.execute(
                    """SELECT planned_capital_usd,revision
                       FROM mininex_live_eth_limits
                       WHERE privy_did=%s""", (did,),
                )
                eth = cur.fetchone()
                if (type(eth) is not dict
                        or type(eth.get("revision")) is not int
                        or eth["revision"] != revisions["ETH"]
                        or eth.get("planned_capital_usd") is None
                        or eth["planned_capital_usd"] <= 0):
                    raise ValueError("Original ETH user budget changed")
            return record,ordered


async def read_owner_central_wallet_policy_status(did: str) -> dict:
    """Only authenticated user ID, never an address or ID from a request."""
    did = _safe_did(did)
    result = _inert_response()
    try:
        record, scopes = await asyncio.to_thread(
            _read_saved_and_current_preferences, did,
        )
    except (psycopg.Error,ValueError,KeyError,TypeError):
        LOG.warning("Central wallet original budget verification unavailable")
        return {**result,"status":"REQUIRES_RECONCILIATION"}
    if record is None:
        return result
    status=record.get("status")
    if status not in _VALID_STATES:
        return {**result,"status":"REQUIRES_RECONCILIATION"}
    result["status"]=status
    result["scope_coins"]=sorted(row["asset"] for row in scopes)
    result["settlement_policy_by_asset"]={row["asset"]:row.get("settlement_policy") for row in scopes}
    result["legacy_reconciliation_required"]=(
        record.get("legacy_reconciliation_required") is not False
    )
    if status != "GRANT_VERIFIED" or result["legacy_reconciliation_required"]:
        return result
    result["current_user_budgets_verified"]=True
    expected_signer=os.getenv("MININEX_PRIVY_ETH_SIGNER_ID","").strip()
    if not expected_signer or expected_signer != record.get("signer_id"):
        return {**result,"status":"REQUIRES_RECONCILIATION"}
    try:
        user=await fetch_privy_user(did)
        metadata=await fetch_privy_wallet_metadata(record["wallet_id"])
        policy=await _fetch_policy(record["policy_id"])
        verified=review_original_one_signer_wallet(
            did=did,user=user,wallet_metadata=metadata,
            remote_policy=policy,saved_grant=record,scopes=scopes,
            now=int(datetime.now(timezone.utc).timestamp()),
        )
        quorum=await check_privy_backend_quorum(
            expected_quorum_id=expected_signer,
        )
    except Exception:
        LOG.warning("Original central Privy wallet audit not confirmed")
        return {**result,"status":"REQUIRES_RECONCILIATION"}
    if quorum.get("remote_quorum_public_key_verified") is not True:
        return {**result,"status":"REQUIRES_RECONCILIATION"}
    # A policy OWNER quorum signs future policy PATCH operations. It is NOT
    # part of the already-consented additional-signature transaction path.
    # A failed owner-quorum read must not invalidate an independently
    # verified, unchanged wallet grant for existing coins.
    try:
        owner_quorum=await check_privy_backend_quorum(
            expected_quorum_id=record["owner_id"],
        )
    except Exception:
        LOG.warning("Original Privy policy-owner audit unavailable for scope updates")
        owner_quorum={}
    result.update({
        "status":"REMOTE_VERIFIED_READONLY",
        "one_scoped_signer_verified":verified["one_scoped_signer_verified"],
        "remote_policy_exactly_verified":verified["remote_policy_exactly_verified"],
        "backend_quorum_current":True,
        "policy_owner_quorum_current":(
            owner_quorum.get("remote_quorum_public_key_verified") is True
        ),
        "verified_policy_sha256":record["policy_sha256"],
        "verified_wallet_id":record["wallet_id"],
        "verified_signer_id":record["signer_id"],
    })
    # Automatic signing is NOT connected to these consolidated records yet.
    return result
