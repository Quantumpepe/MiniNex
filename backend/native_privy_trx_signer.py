"""TRON transaction-byte signing and durable SELL dispatch.

Only a current independent owner-grant auditor may authorize this internal
entrypoint. Privy must evaluate signTransactionBytes; never fall back to a
hash-only grant. The existing journal and passive finalizer handle the result.
"""
from __future__ import annotations
import base64
import hashlib
import os
import re
import time
from datetime import datetime
from decimal import Decimal

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .eth_privy_p256_signer import sign_privy_rpc_payload
from .native_privy_owner_signer import (
    _original_privy_native_wallet,inspect_original_native_signer_metadata,
)
from .native_live_signed_dispatch import _get_original_prepared_source,_original_quote_digest
from .native_live_order_journal import (
    _claim_signed_native_source_once_locked,_record_signed_native_source_result_locked,
)
from .native_trx_owner_preflight import verify_tron_swap_wire,verify_signed_tron_swap
from .native_trx_manual_send import trx_to_sun
from .native_trx_user_signed_send import _tron_rpc
from .privy_live_signer import fetch_privy_wallet_metadata
from .privy_wallet_verification import APP_ID,fetch_privy_user
from .storage import _safe_did,_db_url,_ensure_schema

_SHA=re.compile(r"^[0-9a-f]{64}$")
_ID=re.compile(r"^[A-Za-z0-9_-]{6,160}$")


def bound_trx_transaction_bytes(manifest,quote):
    txid,tx=verify_tron_swap_wire(manifest["unsigned_transaction"],
        source=quote["source_wallet"],destination=quote["inbound_vault"],
        sun=trx_to_sun(quote["amount"]),memo=quote["memo"])
    if (manifest.get("original_transaction_sha256")!=txid.lower()
            or manifest.get("fee_reserve_verified") is not True
            or manifest.get("source_wallet")!=quote["source_wallet"]
            or manifest.get("destination_wallet")!=quote["destination_wallet"]
            or manifest.get("inbound_vault")!=quote["inbound_vault"]
            or manifest.get("stable")!="USDT" or quote.get("destination_asset")!="USDT"):
        raise ValueError("Original TRX quote or fee reserve changed")
    return {"params":{"bytes":base64.b64encode(bytes.fromhex(tx["raw_data_hex"])).decode("ascii"),
                     "encoding":"base64","hash_function":"sha256"}}


async def sign_and_dispatch_original_trx_sell_once(did,*,intent_key,quote,manifest,permission_audit,client):
    did=_safe_did(did)
    order=_get_original_prepared_source(did,"TRX",intent_key)
    audit=permission_audit
    digest=manifest.get("original_transaction_sha256")
    if (type(audit) is not dict or type(digest) is not str or not _SHA.fullmatch(digest)
            or audit.get("audit_mode")!="CURRENT_NATIVE_OWNER_SIGNED_GRANT"
            or audit.get("did")!=did or audit.get("asset")!="TRX" or audit.get("side")!="SELL"
            or audit.get("stable")!="USDT" or audit.get("intent_key")!=intent_key
            or audit.get("original_manifest_sha256")!=digest
            or audit.get("original_quote_sha256")!=_original_quote_digest(quote)
            or audit.get("tron_transaction_bytes_policy_verified") is not True
            or any(audit.get(k) is not True for k in ("owner_wallet_current",
                "exact_remote_policy_verified","backend_quorum_current",
                "owner_signed_budget_current","user_consent_current"))
            or any(audit.get(k)!=order.get(k) for k in ("source_wallet","destination_wallet",
                "payload_sha256","owner_consent_sha256","privy_grant_sha256","budget_revision"))
            or order.get("side")!="SELL" or order.get("stable")!="USDT"
            or order.get("immutable_quote_sha256")!=_original_quote_digest(quote)
            or Decimal(str(order["input_amount"]))!=Decimal(quote["amount"])
            or order["source_wallet"]!=quote["source_wallet"]
            or order["destination_wallet"]!=quote["destination_wallet"]):
        raise HTTPException(409,"Original TRX transaction-byte owner grant missing")
    end=quote.get("quote_expires_at")
    if (not isinstance(end,datetime) or end.tzinfo is None or end.timestamp()<=time.time()
            or str(end)!=str(order.get("quote_expires_at"))):
        raise HTTPException(409,"Original TRX quote expired")
    body=bound_trx_transaction_bytes(manifest,quote)
    user=await fetch_privy_user(did)
    linked=_original_privy_native_wallet(user,did,"TRX")
    remote=await fetch_privy_wallet_metadata(linked["wallet_id"])
    state=inspect_original_native_signer_metadata(did=did,asset="TRX",linked=user,remote=remote)
    signer=os.getenv("MININEX_PRIVY_ETH_SIGNER_ID","").strip()
    policy=audit.get("policy_id")
    if (linked["address"]!=order["source_wallet"] or not linked["delegated"]
            or linked["wallet_id"]!=audit.get("wallet_id") or not _ID.fullmatch(signer)
            or type(policy) is not str or not _ID.fullmatch(policy) or signer==policy
            or audit.get("signer_id")!=signer
            or state.get("original_public_key_matches_wallet") is not True
            or remote.get("additional_signers") is None
            or len(remote["additional_signers"])!=1
            or remote["additional_signers"][0].get("signer_id")!=signer
            or remote["additional_signers"][0].get("override_policy_ids")!=[policy]):
        raise HTTPException(409,"Original TRX wallet signer changed")
    secret=os.getenv("MININEX_PRIVY_APP_SECRET","").strip()
    if len(secret)<12:
        raise HTTPException(503,"Privy service secret missing")
    url="https://api.privy.io/v1/wallets/"+linked["wallet_id"]+"/raw_sign"
    key="trx_"+hashlib.sha256((intent_key+digest).encode("ascii")).hexdigest()
    auth=sign_privy_rpc_payload(url=url,body=body,idempotency_key=key)
    try:
        response=await client.post(url,auth=(APP_ID,secret),json=body,
            headers={"privy-app-id":APP_ID,"privy-idempotency-key":key,
                     "privy-authorization-signature":auth,"Content-Type":"application/json"})
        response.raise_for_status()
        if len(response.content)>16000:
            raise ValueError("Oversized TRON signature")
        payload=response.json()
        if payload.get("method")!="raw_sign" or payload["data"].get("encoding")!="hex":
            raise ValueError("Unrecognized TRON signature response")
        signed=verify_signed_tron_swap(manifest,quote,payload["data"]["signature"])
    except (httpx.HTTPError,ValueError,KeyError,TypeError):
        raise HTTPException(503,"Original TRX signature unavailable") from None
    if end.timestamp()<=time.time():
        raise HTTPException(409,"Original TRX quote expired during signing")
    # Persist the exact signed transaction identity BEFORE the only send.
    db=_db_url();_ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            _claim_signed_native_source_once_locked(cur,did=did,asset="TRX",
                key=intent_key,digest=order["payload_sha256"],signed_source_tx_hash=digest,
                permission_audit=audit)
    accepted=False
    try:
        result=await _tron_rpc(client,"/wallet/broadcasttransaction",signed)
        accepted=(result.get("result") is True
                  and (result.get("txid") is None or str(result["txid"]).lower()==digest))
    except (HTTPException,httpx.HTTPError,ValueError,KeyError,TypeError):
        pass
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            state=_record_signed_native_source_result_locked(cur,did=did,asset="TRX",
                key=intent_key,digest=order["payload_sha256"],signed_source_tx_hash=digest,
                network_accepted=accepted)
    return {"asset":"TRX","side":"SELL","intent_key":intent_key,"journal_state":state,
            "source_tx_hash_expected":digest,"network_submission_acknowledged":accepted,
            "real_trade_sent":accepted,"destination_settled":False,"finalized":False,
            "fee_assessed":False,"retry_allowed":False}
