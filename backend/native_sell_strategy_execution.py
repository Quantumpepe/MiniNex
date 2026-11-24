"""Connect original BTC/SOL source preparation, owner grant and one-shot SELL.

The production grant auditor still rejects missing remote permission; this
coordinator cannot create one. No automatic XRP/TRX signing is inferred.
Private quote/source bytes never appear in the worker result.
"""
import asyncio
import hashlib
import json
import re

from fastapi import HTTPException
from .storage import _safe_did
from .native_btc_original_preflight import prepare_original_btc_unsigned_psbt
from .native_sol_owner_preflight import prepare_original_sol_unsigned_owner_transfer
from .native_live_order_journal import immutable_native_order_identity,persist_internal_native_order
from .native_live_signed_dispatch import _original_quote_digest
from .native_privy_btc_signer import sign_and_dispatch_original_btc_sell_once
from .native_privy_sol_signer import sign_and_dispatch_original_sol_sell_once

_SHA=re.compile(r"^[0-9a-f]{64}$")
_FLAGS=("owner_wallet_current","exact_remote_policy_verified","backend_quorum_current",
        "owner_signed_budget_current","user_consent_current")


async def coordinate_original_native_sell_once(did,*,asset,stable,amount,quote,
        strategy_run_id,signal_event_id,budget_revision,grant_auditor,client):
    did=_safe_did(did)
    if asset not in ("BTC","SOL") or not callable(grant_auditor):
        raise ValueError("Original native SELL executor unavailable")
    if (quote.get("source_asset")!=asset or quote.get("destination_asset")!=stable
            or quote.get("amount")!=amount):
        raise ValueError("Original SELL quote changed")
    prepare=(prepare_original_btc_unsigned_psbt if asset=="BTC" else
             prepare_original_sol_unsigned_owner_transfer)
    preflight=await prepare(did,stable=stable,amount=amount,
        _private_quote=quote,_internal_bundle=True)
    if preflight.get("_private_quote")!=quote:
        raise ValueError("Original private native quote changed")
    manifest=preflight["unsigned_psbt" if asset=="BTC" else "original_compiled_message"]
    manifest_digest=manifest["psbt_sha256" if asset=="BTC" else "message_sha256"]
    if (not _SHA.fullmatch(manifest_digest)
            or manifest["source_wallet"]!=quote["source_wallet"]
            or manifest["destination_wallet"]!=quote["destination_wallet"]):
        raise ValueError("Original native SELL source changed")
    intent="native_"+hashlib.sha256(json.dumps(
        [did,asset,strategy_run_id,signal_event_id],separators=(",",":")).encode()).hexdigest()
    quote_digest=_original_quote_digest(quote)
    try:
        grant=await grant_auditor(did,asset=asset,stable=stable,amount=amount,
            budget_revision=budget_revision,signal_event_id=signal_event_id,side="SELL",
            source_wallet=quote["source_wallet"],destination_wallet=quote["destination_wallet"],
            original_manifest_sha256=manifest_digest,original_quote_sha256=quote_digest,
            intent_key=intent)
    except HTTPException as exc:
        if isinstance(exc.detail,dict) and exc.detail.get("code")=="NATIVE_PRIVY_GRANT_NOT_VERIFIED":
            return {"asset":asset,"state":"WAIT","reason":"NATIVE_PRIVY_GRANT_NOT_VERIFIED",
                    "real_trade_sent":False}
        raise
    expected={"did":did,"asset":asset,"side":"SELL","stable":stable,
              "source_amount":amount,"budget_revision":budget_revision,"intent_key":intent,
              "source_wallet":quote["source_wallet"],"destination_wallet":quote["destination_wallet"],
              "original_manifest_sha256":manifest_digest,"original_quote_sha256":quote_digest}
    if (type(grant) is not dict or grant.get("audit_mode")!="CURRENT_NATIVE_OWNER_SIGNED_GRANT"
            or any(grant.get(k)!=v for k,v in expected.items())
            or any(grant.get(k) is not True for k in _FLAGS)
            or any(type(grant.get(k)) is not str or not _SHA.fullmatch(grant[k])
                   for k in ("owner_consent_sha256","privy_grant_sha256","policy_sha256"))):
        raise HTTPException(409,"Original native SELL permission absent or changed")
    if asset=="BTC" and grant.get("original_psbt_sha256")!=manifest_digest:
        raise HTTPException(409,"Original BTC signed scope changed")
    if asset=="SOL" and (grant.get("original_message_sha256")!=manifest_digest
            or grant.get("source_message_sha256")!=manifest_digest):
        raise HTTPException(409,"Original SOL signed scope changed")
    identity=immutable_native_order_identity(did=did,asset=asset,side="SELL",stable=stable,
        source_wallet=quote["source_wallet"],destination_wallet=quote["destination_wallet"],
        amount=amount,quote=quote,quote_expires_at=quote["quote_expires_at"],
        strategy_run_id=strategy_run_id,signal_event_id=signal_event_id,
        budget_revision=budget_revision,owner_consent_sha256=grant["owner_consent_sha256"],
        privy_grant_sha256=grant["privy_grant_sha256"])
    if identity["intent_key"]!=intent or identity["immutable_quote_sha256"]!=quote_digest:
        raise ValueError("Original durable SELL identity changed")
    saved=await asyncio.to_thread(persist_internal_native_order,identity)
    if saved.get("state")!="PREPARED":
        raise HTTPException(409,"Original native SELL already claimed")
    audit={**grant,"payload_sha256":identity["payload_sha256"]}
    if asset=="BTC":
        return await sign_and_dispatch_original_btc_sell_once(did,intent_key=intent,quote=quote,
            psbt=manifest,original_utxos=preflight["_original_utxos"],
            fee_rate_sats_per_vbyte=preflight["fee_rate_sats_per_vbyte"],
            permission_audit=audit,client=client)
    return await sign_and_dispatch_original_sol_sell_once(did,intent_key=intent,quote=quote,
        manifest=manifest,permission_audit=audit,client=client)
