"""Confirm original user's explicit Privy signer removal for ONE EVM wallet.

Only after Privy's SDK removeSigners has completed may the authenticated
backend independently GET the original wallet and verify the additional
signer is absent. This operation never removes a signer or private key
server-side, cannot regrant authority and cannot send a transaction.
"""
from __future__ import annotations

import logging

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .privy_wallet_verification import fetch_privy_user
from .privy_live_signer import _owned_eth_wallet,fetch_privy_wallet_metadata
from .storage import _db_url,_ensure_schema,_safe_did

LOG=logging.getLogger("mininex.original_wallet_policy_revocation")


def _verified_original_absence(*,did:str,user:dict,remote:dict)->dict:
    try:
        wallet=_owned_eth_wallet(user,did)
    except (ValueError,TypeError,KeyError):
        raise HTTPException(409,"Original Privy wallet identity unavailable") from None
    if (wallet["delegated"] is not False
            or type(remote) is not dict
            or remote.get("id")!=wallet["wallet_id"]
            or remote.get("chain_type")!="ethereum"
            or str(remote.get("address","")).lower()!=wallet["address"]
            or type(remote.get("entity")) is not dict
            or remote["entity"].get("type")!="user"
            or remote["entity"].get("id")!=did
            or remote.get("archived_at") is not None
            or remote.get("additional_signers")!=[]):
        raise HTTPException(409,"Privy still reports an additional wallet signer")
    return wallet


def _confirm_original_revocation_locked(cur,*,did:str,wallet:dict)->dict:
    cur.execute(
        """SELECT wallet_id,wallet_address,signer_id,policy_id,status
           FROM mininex_live_evm_wallet_policy_grants
           WHERE privy_did=%s FOR UPDATE""",(did,),
    )
    saved=cur.fetchone()
    if saved is None:
        return {
            "mode":"ORIGINAL_EVM_ONE_SIGNER_REVOCATION",
            "status":"NOT_CONFIGURED","wallet_signer_absence_verified":True,
            "new_signer_granted":False,"future_auto_orders_blocked":True,
        }
    if (type(saved) is not dict
            or saved.get("wallet_id")!=wallet["wallet_id"]
            or saved.get("wallet_address")!=wallet["address"]):
        raise HTTPException(409,"Original wallet ledger differs from Privy")
    if saved.get("status")!="REVOKED":
        cur.execute(
            """UPDATE mininex_live_evm_wallet_policy_grants
               SET status='REVOKED',revoked_at=now(),updated_at=now()
               WHERE privy_did=%s AND wallet_id=%s
                 AND status<>'REVOKED' RETURNING status""",
            (did,wallet["wallet_id"]),
        )
        changed=cur.fetchone()
        if type(changed) is not dict or changed.get("status")!="REVOKED":
            raise HTTPException(409,"Original EVM wallet grant could not be revoked")
        # An in-flight/uncertain policy edit cannot reinstate an absent signer.
        # Preserve the exact attempted remote request in the journal,
        # but mark unconsumed permissions terminal.
        cur.execute(
            """UPDATE mininex_live_evm_wallet_policy_intents
               SET status='REVOKED'
               WHERE privy_did=%s AND status IN (
                 'PENDING','SIGNED_INTENT','PATCH_SUBMITTING','PATCH_UNCERTAIN'
               )""",(did,),
        )
        # Prevent new orders from any prior session, regardless of coin.
        cur.execute(
            """UPDATE mininex_live_evm_auto_sessions
               SET status='PAUSED',updated_at=now()
               WHERE privy_did=%s AND (status='ACTIVE' OR status='EXIT_PENDING')""",(did,),
        )
    return {
        "mode":"ORIGINAL_EVM_ONE_SIGNER_REVOCATION",
        "status":"REVOKED",
        "wallet_signer_absence_verified":True,
        "new_signer_granted":False,
        "future_auto_orders_blocked":True,
        "real_transaction_cancelled":False,
    }


async def confirm_original_wallet_policy_revocation(did:str)->dict:
    did=_safe_did(did)
    user=await fetch_privy_user(did)
    original=_owned_eth_wallet(user,did)
    remote=await fetch_privy_wallet_metadata(original["wallet_id"])
    wallet=_verified_original_absence(did=did,user=user,remote=remote)
    try:
        url=_db_url()
        _ensure_schema(url)
        with psycopg.connect(url,connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                return _confirm_original_revocation_locked(
                    cur,did=did,wallet=wallet,
                )
    except psycopg.Error:
        LOG.warning("Original Privy wallet revocation persistence unavailable")
        raise HTTPException(503,"Cannot confirm original EVM wallet revocation") from None
