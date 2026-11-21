"""Internal original-native one-shot signed source dispatcher.

Unlike preview components, this code can send real BTC/SOL mainnet sources.
The original XRP transport exists but current documented Privy XRPL remote
signer is unavailable, so an XRP source cannot pass the supported-signer gate.

No FastAPI endpoint, startup task, scheduler, or automatic trade calls this
entrypoint. The future strategy worker must independently produce the CURRENT
owner-signed consent, exact remote Privy policy and authorized quorum audit.
Neither a Readiness result nor an arbitrary policy ID is such authorization.
The original journal commits SENDING and the signed transaction hash BEFORE
the first irreversible chain RPC call. Unknown results are never retried.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import time

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .native_live_broadcast_transport import (
    broadcast_original_native_signed_once,signed_native_transaction_hash,
)
from .native_live_order_journal import (
    _claim_signed_native_source_once_locked,
    _record_signed_native_source_result_locked,
)
from .native_privy_owner_signer import get_original_native_privy_signer_status
from .native_btc_privy_signed_readback import inspect_original_btc_signed_transaction
from .native_sol_privy_signed_readback import inspect_original_sol_signed_transaction
from .native_xrp_privy_signed_readback import inspect_original_xrp_signed_payment
from .native_btc_thor_psbt import compile_original_unsigned_btc_thor_psbt
from .native_btc_thor_outputs import _mainnet_p2wpkh_script
from .storage import _db_url, _ensure_schema, _safe_did

_INTENT=re.compile(r"^native_[0-9a-f]{64}$")
_SHA=re.compile(r"^[0-9a-f]{64}$")
_ASSET_PRECISION={"BTC":8,"SOL":9,"XRP":6}
_ORIGINAL_ETH_BUY_ASSETS=frozenset(("BTC","SOL","XRP","TRX","DOGE"))


def _original_quote_digest(quote:object)->str:
    if type(quote) is not dict:
        raise ValueError("Original private THORChain quote unavailable")
    fields={k:(v.isoformat() if isinstance(v,datetime) else v)
            for k,v in sorted(quote.items())}
    return hashlib.sha256(json.dumps(
        fields,sort_keys=True,separators=(",",":"),ensure_ascii=True,
    ).encode("utf-8")).hexdigest()


def inspect_original_order_source_for_dispatch(
    order:object,*,quote:object,manifest:object,
    signed_wire_base64:object,now:int,
    original_utxos:object=None,fee_rate_sats_per_vbyte:object=None,
    original_owner_public_key_hex:object=None,
)->dict:
    """Pure validation of real one-shot signed source against durable order."""
    if (type(order) is not dict or type(quote) is not dict
            or type(manifest) is not dict or type(now) is not int
            or order.get("asset") not in _ASSET_PRECISION
            or order.get("side")!="SELL"
            or order.get("status")!="PREPARED"
            or type(order.get("intent_key")) is not str
            or not _INTENT.fullmatch(order["intent_key"])
            or type(order.get("payload_sha256")) is not str
            or not _SHA.fullmatch(order["payload_sha256"])
            or type(order.get("immutable_quote_sha256")) is not str
            or order["immutable_quote_sha256"]!=_original_quote_digest(quote)
            or quote.get("mode")!="THORCHAIN_DESTINATION_BOUND_READ_ONLY"
            or quote.get("from_network")!={
                "BTC":"bitcoin-mainnet","SOL":"solana-mainnet","XRP":"xrpl-mainnet",
            }[order["asset"]]
            or quote.get("to_network")!="ethereum-mainnet"
            or quote.get("source_asset")!=order["asset"]
            or quote.get("destination_asset")!=order.get("stable")
            or quote.get("source_wallet")!=order.get("source_wallet")
            or quote.get("destination_wallet")!=order.get("destination_wallet")
            or manifest.get("asset")!=order["asset"]
            or manifest.get("side")!="SELL"
            or manifest.get("source_wallet")!=order["source_wallet"]
            or manifest.get("destination_wallet")!=order["destination_wallet"]
            or manifest.get("inbound_vault")!=quote.get("inbound_vault")
            or quote.get("memo") is None):
        raise ValueError("Original signed source does not match durable quote")
    expiry=quote.get("quote_expires_at")
    if (not isinstance(expiry,datetime) or expiry.tzinfo is None
            or not now<expiry.timestamp()
            or str(order.get("quote_expires_at"))!=str(expiry)):
        raise ValueError("Original quoted source expired")
    try:
        amount=Decimal(str(order["input_amount"]))
        quoted=Decimal(quote["amount"])
        units=quoted*Decimal(10**_ASSET_PRECISION[order["asset"]])
    except (KeyError,InvalidOperation,TypeError,ValueError):
        raise ValueError("Original signed source quantity unavailable") from None
    if (not units.is_finite() or units<=0 or units!=int(units)
            or quoted!=amount):
        raise ValueError("Original signed source amount changed")
    asset=order["asset"]
    if asset=="BTC":
        if type(original_utxos) is not list or not original_utxos:
            raise ValueError("Current original Bitcoin UTXOs unavailable")
        if type(fee_rate_sats_per_vbyte) is not int:
            raise ValueError("Original Bitcoin network fee rate unavailable")
        reconstructed=compile_original_unsigned_btc_thor_psbt(
            quote,amount=quote["amount"],now=now,
            owner_script_hex=_mainnet_p2wpkh_script(order["source_wallet"]).hex(),
            vault_script_hex=_mainnet_p2wpkh_script(quote["inbound_vault"]).hex(),
            input_utxos=original_utxos,
            fee_rate_sats_per_vbyte=fee_rate_sats_per_vbyte,
        )
        if reconstructed["psbt_sha256"]!=manifest.get("psbt_sha256"):
            raise ValueError("Bitcoin signed source is not the quoted UTXO plan")
        proof=inspect_original_btc_signed_transaction(
            manifest,original_utxos=original_utxos,
            signed_wire_base64=signed_wire_base64,
        )
        atomic=manifest.get("source_amount_sats")
    elif asset=="SOL":
        if manifest.get("refund_memo")!=quote["memo"]:
            raise ValueError("Original Solana quote memo changed")
        proof=inspect_original_sol_signed_transaction(
            manifest,signed_transaction_base64=signed_wire_base64,
        )
        atomic=manifest.get("source_amount_lamports")
    else:
        # The XRPL Payment hash and signature can be independently checked,
        # but Privy has not documented a native 'xrpl' sign-capable wallet.
        if not original_owner_public_key_hex:
            raise ValueError("Original XRP signer key unavailable")
        proof=inspect_original_xrp_signed_payment(
            manifest,original_owner_public_key_hex=original_owner_public_key_hex,
            signed_payment_base64=signed_wire_base64,now=now,
        )
        atomic=manifest.get("source_amount_drops")
    if str(atomic)!=str(int(units)):
        raise ValueError("Original signed native amount differs from journal")
    expected_hash=signed_native_transaction_hash(
        asset,proof,signed_wire_base64,
    )
    return {
        "mode":"ORIGINAL_NATIVE_EXECUTABLE_SOURCE_SIGNED_LOCALLY",
        "asset":asset,"side":"SELL",
        "source_wallet":order["source_wallet"],
        "original_intent_key":order["intent_key"],
        "original_payload_sha256":order["payload_sha256"],
        "signed_source_tx_hash":expected_hash,
        "signed_wire_sha256":proof["signed_wire_sha256"],
        "source_crypto_verified":True,
        "source_chain_confirmed":False,
        "crosschain_swap_filled":False,
        "source_transaction_sent":False,
    }


def _get_original_prepared_source(did:str,asset:str,intent_key:str)->dict:
    if (asset not in _ORIGINAL_ETH_BUY_ASSETS or type(intent_key) is not str
            or not _INTENT.fullmatch(intent_key)):
        raise ValueError("Invalid owner native intent reference")
    db=_db_url()
    _ensure_schema(db)
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT privy_did,asset,chain,side,stable,intent_key,status,
                          source_wallet,destination_wallet,input_amount,
                          payload_sha256,immutable_quote_sha256,
                          owner_consent_sha256,privy_grant_sha256,
                          budget_revision,quote_expires_at
                   FROM mininex_live_native_order_intents
                   WHERE privy_did=%s AND asset=%s AND intent_key=%s""",
                (did,asset,intent_key),
            )
            row=cur.fetchone()
    if (type(row) is not dict or row.get("privy_did")!=did
            or row.get("asset")!=asset or row.get("status")!="PREPARED"
            or (asset in ("TRX","DOGE") and row.get("side")!="BUY")):
        raise HTTPException(409,"No authorized original native source to submit")
    return row


async def dispatch_signed_original_native_once(
    did:str,*,asset:str,intent_key:str,quote:dict,manifest:dict,
    signed_wire_base64:str,permission_audit:dict,
    original_utxos:object=None,fee_rate_sats_per_vbyte:object=None,
    original_owner_public_key_hex:object=None,
)->dict:
    """Actual one-time SELL submission; no caller outside trusted workers.

    No Retry or 'send again' branch under ANY circumstance. Public routes
    must never expose this function or construct permission_audit from user
    JSON or a UI read-only capability display.
    """
    did=_safe_did(did)
    if asset not in _ASSET_PRECISION:
        raise HTTPException(422,"Unsupported native source asset")
    order=_get_original_prepared_source(did,asset,intent_key)
    now=int(time.time())
    source=inspect_original_order_source_for_dispatch(
        order,quote=quote,manifest=manifest,
        signed_wire_base64=signed_wire_base64,now=now,
        original_utxos=original_utxos,
        fee_rate_sats_per_vbyte=fee_rate_sats_per_vbyte,
        original_owner_public_key_hex=original_owner_public_key_hex,
    )
    # Remote owner check is additional to, not a replacement for, the
    # future real signed-policy+budget audit required by the journal claim.
    remote=await get_original_native_privy_signer_status(did,asset=asset)
    if (remote.get("original_wallet_remote_owner_verified") is not True
            or remote.get("original_public_key_matches_wallet") is not True
            or remote.get("privy_native_wallet_api_supported") is not True
            or remote.get("execution_enabled") is not False
            or remote.get("can_submit") is not False):
        raise HTTPException(409,"Original native Privy signer not verified")
    tx_hash=source["signed_source_tx_hash"]
    db=_db_url()
    _ensure_schema(db)
    # Durable pre-send claim commits before even creating the HTTP client.
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            _claim_signed_native_source_once_locked(
                cur,did=did,asset=asset,key=intent_key,
                digest=source["original_payload_sha256"],
                signed_source_tx_hash=tx_hash,
                permission_audit=permission_audit,
            )
    accepted=False
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(12),follow_redirects=False,
        ) as client:
            result=await broadcast_original_native_signed_once(
                client,asset=asset,signed_base64=signed_wire_base64,
                expected_source_hash=tx_hash,
            )
            accepted=result["network_accepted"] is True
    except (httpx.HTTPError,ValueError,KeyError,TypeError):
        # Any ambiguous result remains unresolved. Do not retry the send.
        accepted=False
    with psycopg.connect(db,connect_timeout=6) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            state=_record_signed_native_source_result_locked(
                cur,did=did,asset=asset,key=intent_key,
                digest=source["original_payload_sha256"],
                signed_source_tx_hash=tx_hash,
                network_accepted=accepted,
            )
    return {
        "mode":"ORIGINAL_NATIVE_EXECUTED_ONCE_PENDING_FINALITY",
        "asset":asset,"side":"SELL","intent_key":intent_key,
        "source_tx_hash_expected":tx_hash,
        "journal_state":state,
        "network_submission_acknowledged":accepted,
        "finalized":False,"destination_settled":False,
        "fee_assessed":False,"retry_allowed":False,
    }
