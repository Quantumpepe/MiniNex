"""Real native mainnet source broadcast transport, internal worker only.

These are the REAL one-shot Bitcoin, Solana and XRPL submission operations,
not simulated/paper calls and not source readiness previews. The caller MUST
have committed the original PREPARED -> SENDING journal claim, independently
authenticated the user and current Privy policy, and cryptographically
validated each original signed transaction before calling this module.

No public API route or background worker calls this module automatically.
Network acknowledgement is not finality. Any uncertain send is NEVER retried:
the caller must persist RECONCILIATION_REQUIRED and resolve the expected
transaction hash by querying the source chain rather than re-broadcasting.
"""
from __future__ import annotations

import base64
import re

import httpx

from .blockchain_preview import BTC_API
from .native_coin_balances import _BASE58, _rpc_url

_TXID=re.compile(r"^[0-9a-f]{64}$")
_XRP_HASH=re.compile(r"^[A-F0-9]{64}$")


def _b58(raw:bytes)->str:
    if type(raw) is not bytes or not raw:
        raise ValueError("Invalid original Solana signer bytes")
    n=int.from_bytes(raw,"big")
    result=""
    while n:
        n,remainder=divmod(n,58)
        result=_BASE58[remainder]+result
    return _BASE58[0]*(len(raw)-len(raw.lstrip(b"\x00")))+result


def signed_native_transaction_hash(asset:str,proof:dict,signed_base64:str)->str:
    """Proof-bound deterministic source ID, known BEFORE any RPC call."""
    if (asset not in ("BTC","SOL","XRP") or type(proof) is not dict
            or type(signed_base64) is not str
            or len(signed_base64)>1000000
            or proof.get("asset")!=asset
            or proof.get("side")!="SELL"):
        raise ValueError("Native signed source not ready for network submission")
    try:
        signed=base64.b64decode(signed_base64,validate=True)
    except (ValueError,base64.binascii.Error):
        raise ValueError("Native signed wire encoding unavailable") from None
    if not signed:
        raise ValueError("Empty original signed transaction")
    import hashlib
    if proof.get("signed_wire_sha256")!=hashlib.sha256(signed).hexdigest():
        raise ValueError("Signed transaction changed since owner verification")
    if asset=="BTC":
        h=proof.get("original_unsigned_txid")
        if (proof.get("mode")!="PRIVY_BTC_ORIGINAL_SIGNED_TX_READBACK_ONLY"
                or proof.get("original_owner_signatures_verified") is not True
                or proof.get("original_unsigned_outputs_unchanged") is not True
                or type(h) is not str or not _TXID.fullmatch(h)):
            raise ValueError("Original Bitcoin witness not verified")
    elif asset=="SOL":
        if (proof.get("mode")!="PRIVY_SOL_ORIGINAL_SIGNED_TX_READBACK_ONLY"
                or proof.get("original_owner_signature_verified") is not True
                or proof.get("original_message_unchanged") is not True
                or len(signed)<65 or signed[:1]!=b"\x01"):
            raise ValueError("Original Solana signature not verified")
        h=_b58(signed[1:65])
        if not 80<=len(h)<=90:
            raise ValueError("Invalid original Solana transaction signature")
    else:
        h=proof.get("signed_transaction_hash")
        if (proof.get("mode")!="ORIGINAL_XRP_SIGNED_PAYMENT_READBACK_ONLY"
                or proof.get("original_owner_signature_verified") is not True
                or proof.get("original_payment_fields_unchanged") is not True
                or type(h) is not str or not _XRP_HASH.fullmatch(h)):
            raise ValueError("Original signed XRP Payment invalid")
    return h


async def broadcast_original_native_signed_once(
    client:httpx.AsyncClient,*,asset:str,signed_base64:str,
    expected_source_hash:str,
)->dict:
    """One mainnet RPC submission. ONLY call after the committed SENDING claim.

    Never retry even for a timeout or rejected response: the outcome may be
    unknown until the source network's finalized ledger confirms it.
    """
    if (asset not in ("BTC","SOL","XRP") or type(signed_base64) is not str
            or len(signed_base64)>1000000
            or type(expected_source_hash) is not str):
        raise ValueError("Native signed transport arguments unavailable")
    try:
        wire=base64.b64decode(signed_base64,validate=True)
    except (ValueError,base64.binascii.Error):
        raise ValueError("Native signed transport encoding invalid") from None
    if not wire:
        raise ValueError("Empty signed native wire")
    if asset=="BTC":
        if not _TXID.fullmatch(expected_source_hash):
            raise ValueError("Original BTC transaction hash invalid")
        response=await client.post(
            BTC_API.rstrip("/")+"/tx",content=wire.hex(),
            headers={"Content-Type":"text/plain","Accept":"text/plain"})
        response.raise_for_status()
        if len(response.content)>256:
            raise ValueError("Bitcoin broadcast acknowledgement malformed")
        observed=response.text.strip().lower()
        if observed!=expected_source_hash:
            raise ValueError("Bitcoin broadcast returned another transaction ID")
    elif asset=="SOL":
        # Solana RPC submission returns the transaction's first signature;
        # the call does not prove execution or chain confirmation.
        if len(expected_source_hash)<75 or len(expected_source_hash)>90:
            raise ValueError("Original SOL signature id invalid")
        if wire[:1]!=b"\x01" or _b58(wire[1:65])!=expected_source_hash:
            raise ValueError("Solana signer changed before network submission")
        response=await client.post(
            _rpc_url("SOL"),json={
                "jsonrpc":"2.0","id":1,"method":"sendTransaction",
                "params":[signed_base64,{
                    "encoding":"base64","skipPreflight":False,
                    "preflightCommitment":"finalized","maxRetries":0,
                }],
            },headers={"Content-Type":"application/json",
                       "Accept":"application/json"})
        response.raise_for_status()
        if len(response.content)>16000:
            raise ValueError("Solana mainnet acknowledgement oversized")
        body=response.json()
        if (type(body) is not dict or body.get("jsonrpc")!="2.0"
                or body.get("id")!=1 or "error" in body
                or body.get("result")!=expected_source_hash):
            raise ValueError("Solana mainnet did not acknowledge original source")
    else:
        if not _XRP_HASH.fullmatch(expected_source_hash):
            raise ValueError("Original XRPL transaction hash invalid")
        response=await client.post(_rpc_url("XRP"),json={
            "method":"submit","params":[{"tx_blob":wire.hex().upper(),
                                          "fail_hard":True}],
        },headers={"Content-Type":"application/json",
                   "Accept":"application/json"})
        response.raise_for_status()
        if len(response.content)>30000:
            raise ValueError("XRP mainnet acknowledgement oversized")
        envelope=response.json()
        payload=envelope.get("result") if type(envelope) is dict else None
        if (type(payload) is not dict or payload.get("status")!="success"
                or payload.get("accepted") is not True
                or payload.get("engine_result") not in ("tesSUCCESS","terQUEUED")
                or (payload.get("tx_blob") is not None
                    and payload["tx_blob"].upper()!=wire.hex().upper())
                or (type(payload.get("tx_json")) is dict
                    and payload["tx_json"].get("hash") is not None
                    and payload["tx_json"]["hash"].upper()!=expected_source_hash)):
            raise ValueError("XRPL mainnet did not accept original signed Payment")
    return {
        "mode":"ORIGINAL_NATIVE_MAINNET_BROADCAST_ACK",
        "asset":asset,"side":"SELL",
        "source_tx_hash":expected_source_hash,
        "network_accepted":True,
        "source_chain_confirmed":False,
        "crosschain_swap_settled":False,
        "destination_fill_verified":False,
        "refund_verified":False,
        "fee_assessed":False,
    }
