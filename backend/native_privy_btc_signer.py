"""Real Privy raw BTC input signing with original owner, then one-shot wire.

Unlike the previous BIP143 previews this module makes actual server-side
Privy secp256k1_sign calls when and ONLY when an independently audited owner
consent+policy accompanies a trusted internal worker. Each original UTXO
sighash is signed exactly once; every detached signature is verified before
the canonical SegWit transaction is passed to the existing durable dispatcher.
No public API route or automatic scheduler may call this module.
"""
from __future__ import annotations

import base64
import os
import re

import httpx
from fastapi import HTTPException

from .eth_privy_p256_signer import sign_privy_rpc_payload
from .native_btc_signed_wire import verify_original_btc_detached_signatures
from .native_btc_privy_signed_readback import inspect_original_btc_signed_transaction
from .native_privy_owner_signer import (
    _original_privy_native_wallet, inspect_original_native_signer_metadata,
)
from .native_privy_unsigned_signing_requests import preview_privy_btc_raw_sign_requests
from .native_live_signed_dispatch import dispatch_signed_original_native_once
from .privy_live_signer import fetch_privy_wallet_metadata
from .privy_wallet_verification import APP_ID, fetch_privy_user
from .storage import _safe_did

_ID = re.compile(r"^[A-Za-z0-9_-]{6,160}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_INTENT = re.compile(r"^native_[0-9a-f]{64}$")
_ENDPOINT = "https://api.privy.io/v1/wallets/"
_QUORUM_ENV = "MININEX_PRIVY_ETH_SIGNER_ID"


def _require_owner_btc_signing(audit: object, *, did: str, key: str,
                               wallet_id: str, wallet: str,
                               policy_id: str, signer_id: str,
                               psbt_hash: str) -> None:
    """No additional trading limits; require only actual owner permission."""
    if (type(audit) is not dict
            or audit.get("audit_mode") != "CURRENT_NATIVE_OWNER_SIGNED_GRANT"
            or audit.get("asset") != "BTC"
            or audit.get("did") != did
            or audit.get("intent_key") != key
            or audit.get("wallet_id") != wallet_id
            or audit.get("source_wallet") != wallet
            or audit.get("signer_id") != signer_id
            or audit.get("policy_id") != policy_id
            or audit.get("original_psbt_sha256") != psbt_hash
            or audit.get("owner_wallet_current") is not True
            or audit.get("exact_remote_policy_verified") is not True
            or audit.get("backend_quorum_current") is not True
            or audit.get("owner_signed_budget_current") is not True
            or audit.get("user_consent_current") is not True
            or type(audit.get("budget_revision")) is not int
            or audit["budget_revision"] < 1
            or type(audit.get("owner_consent_sha256")) is not str
            or not _SHA.fullmatch(audit["owner_consent_sha256"])
            or type(audit.get("policy_sha256")) is not str
            or not _SHA.fullmatch(audit["policy_sha256"])):
        raise HTTPException(409, "Original BTC user-signed current grant unavailable")


def _parse_privy_secp256k1_signature(payload: object) -> str:
    """Privy may append an ECDSA recovery byte; BIP143 takes 64 raw bytes."""
    data = payload.get("data") if type(payload) is dict else None
    sig = data.get("signature") if type(data) is dict else None
    if type(sig) is not str:
        raise ValueError("Original Privy BTC ECDSA signature missing")
    raw = sig.removeprefix("0x")
    if not re.fullmatch(r"[0-9a-fA-F]{128}(?:[0-9a-fA-F]{2})?",raw):
        raise ValueError("Malformed BTC Privy signature")
    if len(raw) == 130 and int(raw[-2:],16) not in (0,1,27,28):
        raise ValueError("Invalid BTC ECDSA recovery parity")
    return raw[:128].lower()


async def sign_original_btc_with_privy_once(
    did: str, *, psbt: dict, original_utxos: list,
    intent_key: str, permission_audit: dict,
    client: httpx.AsyncClient,
) -> dict:
    did = _safe_did(did)
    if type(intent_key) is not str or not _INTENT.fullmatch(intent_key):
        raise ValueError("Unrecognized BTC original order")
    user = await fetch_privy_user(did)
    linked = _original_privy_native_wallet(user,did,"BTC")
    if linked["delegated"] is not True:
        raise HTTPException(409, "Original BTC wallet not delegated")
    remote = await fetch_privy_wallet_metadata(linked["wallet_id"])
    meta = inspect_original_native_signer_metadata(
        did=did,asset="BTC",linked=user,remote=remote)
    if (meta.get("original_wallet_remote_owner_verified") is not True
            or meta.get("original_public_key_matches_wallet") is not True
            or meta.get("privy_native_wallet_api_supported") is not True):
        raise HTTPException(409, "Original BTC public signer unverified")
    signer_id=os.getenv(_QUORUM_ENV,"").strip()
    policy_id=permission_audit.get("policy_id") if type(permission_audit) is dict else None
    if (not _ID.fullmatch(signer_id)
            or type(policy_id) is not str
            or not _ID.fullmatch(policy_id) or signer_id==policy_id):
        raise HTTPException(409, "Original BTC policy or quorum not configured")
    signers=remote.get("additional_signers")
    if (type(signers) is not list
            or len([x for x in signers if type(x) is dict
                    and x.get("signer_id")==signer_id
                    and x.get("override_policy_ids")==[policy_id]])!=1):
        raise HTTPException(409, "Original BTC wallet signer not attached")
    preview=preview_privy_btc_raw_sign_requests(
        psbt,original_utxos=original_utxos,
        owner_public_key_hex=remote["public_key"],
        wallet_id=linked["wallet_id"],intent_key=intent_key)
    if preview["source_wallet"]!=linked["address"]:
        raise HTTPException(409, "Original BTC source owner changed")
    _require_owner_btc_signing(
        permission_audit,did=did,key=intent_key,
        wallet_id=linked["wallet_id"],wallet=linked["address"],
        policy_id=policy_id,signer_id=signer_id,
        psbt_hash=preview["original_psbt_sha256"])
    secret=os.getenv("MININEX_PRIVY_APP_SECRET","").strip()
    if len(secret)<12:
        raise HTTPException(503, "Privy BTC service secret not configured")
    url=_ENDPOINT+linked["wallet_id"]+"/rpc"
    raw_signatures=[]
    for item in preview["unsigned_sign_requests"]:
        body={"method":item["method"],"params":item["params"]}
        key=item["idempotency_key"]
        signature=sign_privy_rpc_payload(
            url=url,body=body,idempotency_key=key)
        try:
            response=await client.post(
                url,auth=(APP_ID,secret),json=body,
                headers={"privy-app-id":APP_ID,
                         "privy-idempotency-key":key,
                         "privy-authorization-signature":signature,
                         "Content-Type":"application/json"})
            response.raise_for_status()
            if len(response.content)>10000:
                raise ValueError("Privy BTC raw signature response oversized")
            value=_parse_privy_secp256k1_signature(response.json())
        except (httpx.HTTPError,ValueError,TypeError):
            # No network transaction was sent. Do not assemble a partial PSBT
            # from input signatures whose origin cannot be verified.
            raise HTTPException(503,"Original BTC wallet signing incomplete") from None
        raw_signatures.append(value)
    # Every signature must match the original owner's BIP143 SIGHASH_ALL
    # for precisely its corresponding UTXO, vault, change and memo outputs.
    public=remote["public_key"].removeprefix("0x")
    verified=verify_original_btc_detached_signatures(
        psbt,original_utxos=original_utxos,
        original_owner_public_key_hex=public,
        original_signatures_hex=raw_signatures)
    reread=inspect_original_btc_signed_transaction(
        psbt,original_utxos=original_utxos,
        signed_wire_base64=verified["signed_wire_base64"])
    if (reread.get("original_owner_signatures_verified") is not True
            or reread.get("original_unsigned_outputs_unchanged") is not True
            or reread["signed_wire_sha256"]!=verified["signed_wire_sha256"]):
        raise ValueError("BTC signed owner wire no longer matches original order")
    return {
        "mode":"SIGNED_ORIGINAL_BTC_PRIVY_INTERNAL_ONLY",
        "asset":"BTC","intent_key":intent_key,
        "source_wallet":linked["address"],
        "original_psbt_sha256":preview["original_psbt_sha256"],
        "verified_input_count":len(raw_signatures),
        "signed_wire_sha256":verified["signed_wire_sha256"],
        "signed_wire_base64":verified["signed_wire_base64"],
        "owner_signatures_verified":True,
        "wallet_signed":True,
        "source_transaction_sent":False,
        "destination_settled":False,
    }


async def sign_and_dispatch_original_btc_sell_once(
    did: str, *, intent_key: str, quote: dict, psbt: dict,
    original_utxos: list, fee_rate_sats_per_vbyte: int,
    permission_audit: dict, client: httpx.AsyncClient,
) -> dict:
    """No public caller. Existing dispatcher commits SENDING before HTTP."""
    signed=await sign_original_btc_with_privy_once(
        did,psbt=psbt,original_utxos=original_utxos,
        intent_key=intent_key,permission_audit=permission_audit,
        client=client)
    return await dispatch_signed_original_native_once(
        did,asset="BTC",intent_key=intent_key,
        quote=quote,manifest=psbt,
        signed_wire_base64=signed["signed_wire_base64"],
        permission_audit=permission_audit,
        original_utxos=original_utxos,
        fee_rate_sats_per_vbyte=fee_rate_sats_per_vbyte)
