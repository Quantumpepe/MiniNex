"""Two-step user-authorized XRPL send: prepare -> Privy user raw-sign -> submit.

No backend custody/signing. The login's linked wallet is checked against
Privy wallet metadata on both steps. A short-lived authenticated MAC binds
exact terms, public key, sequence, fee and expiration to the signed hash.
"""
from __future__ import annotations

import asyncio
import base64
from decimal import Decimal
import hashlib
import hmac
import json
import os
import secrets
import time

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from fastapi import HTTPException
from xrpl.core.binarycodec import encode, encode_for_signing

from .native_xrp_manual_send import (
    DROPS, _ledger_rpc, amount_to_drops, original_xrp_wallet,
    prepare_mainnet_payment, verify_privy_xrp_remote_owner,
)
from .native_xrp_signing_blob import _owner_pubkey
from .privy_wallet_verification import fetch_privy_user
from .privy_live_signer import fetch_privy_wallet_metadata
from .storage import _safe_did

SECP_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_TXN_PREFIX = bytes.fromhex("54584e00")
_EXPIRY_SECONDS = 180
_INFLIGHT: set[str] = set()
_SEEN: dict[str, float] = {}
_LOCK = asyncio.Lock()


def _secret() -> bytes:
    value = os.environ.get("MININEX_PRIVY_APP_SECRET", "").strip()
    if len(value) < 16:
        raise HTTPException(503, "XRP transaction authorization unavailable")
    return hmac.new(value.encode(), b"mininex:xrp:manual:user-sign:v1", hashlib.sha256).digest()


def _seal(obj: dict) -> str:
    plain = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    if len(plain) > 3500:
        raise HTTPException(503, "XRP transaction too large")
    mac = hmac.new(_secret(), plain, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(plain + mac).decode().rstrip("=")


def _unseal(token: object, did: str) -> dict:
    if type(token) is not str or len(token) > 6000 or len(token) < 80:
        raise HTTPException(422, "Invalid or expired XRP authorization")
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        plain, mac = raw[:-32], raw[-32:]
        if len(plain) > 3500 or not hmac.compare_digest(mac, hmac.new(_secret(), plain, hashlib.sha256).digest()):
            raise ValueError()
        data = json.loads(plain)
        if (type(data) is not dict or data.get("ver") != 1
                or data.get("did") != did
                or type(data.get("created")) is not int
                or not 0 <= int(time.time()) - data["created"] <= _EXPIRY_SECONDS
                or type(data.get("nonce")) is not str or len(data["nonce"]) != 32):
            raise ValueError()
        return data
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(422, "XRP authorization expired or invalid") from None


async def _owned(did: str, source: str) -> tuple[str, str]:
    user = await fetch_privy_user(did)
    wallet_id = original_xrp_wallet(user, did, source)
    remote = await fetch_privy_wallet_metadata(wallet_id)
    verify_privy_xrp_remote_owner(remote, wallet_id=wallet_id, address=source, did=did)
    pubkey = remote.get("public_key")
    try:
        key, address = _owner_pubkey(pubkey)
    except ValueError:
        raise HTTPException(503, "Privy XRP public key unavailable") from None
    if address != source:
        raise HTTPException(403, "XRP signing key does not match wallet")
    if key[0] not in (2, 3):
        # The Privy XRPL raw-hash API provides secp256k1 prehash signing.
        # An Ed25519 account needs a distinct signing-bytes implementation.
        raise HTTPException(422, "This XRP wallet uses an unsupported signing curve")
    return wallet_id, pubkey.upper()


async def prepare_xrp(did: str, *, source: object, destination: object, amount: object) -> dict:
    from .native_coin_balances import _valid_address, _rpc_url
    did = _safe_did(did)
    if (not _valid_address("XRP", source) or not _valid_address("XRP", destination)
            or source == destination):
        raise HTTPException(422, "Choose a different valid XRP destination")
    drops = amount_to_drops(amount)
    wallet_id, pubkey = await _owned(did, source)
    async with httpx.AsyncClient(timeout=httpx.Timeout(12), follow_redirects=False) as client:
        server = await _ledger_rpc(client, "server_info", [{}])
        fees = await _ledger_rpc(client, "fee", [{}])
        idx = server.get("result", {}).get("info", {}).get("validated_ledger", {}).get("seq")
        if type(idx) is not int:
            raise HTTPException(503, "Validated XRP Ledger unavailable")
        account = await _ledger_rpc(client, "account_info",
                                    [{"account": source, "ledger_index": idx, "strict": True}])
    unsigned = prepare_mainnet_payment(source, destination, drops, server, fees, account)
    unsigned["SigningPubKey"] = pubkey
    try:
        signing_bytes = bytes.fromhex(encode_for_signing(unsigned))
    except (ValueError, TypeError, KeyError):
        raise HTTPException(503, "Could not prepare XRP signing input") from None
    if not signing_bytes.startswith(bytes.fromhex("53545800")):
        raise HTTPException(503, "XRP signing input invalid")
    digest = hashlib.sha512(signing_bytes).digest()[:32]
    token = _seal({"ver": 1, "created": int(time.time()), "nonce": secrets.token_hex(16),
                   "did": did, "wallet_id": wallet_id, "source": source,
                   "unsigned": unsigned})
    return {"authorization": token, "hash": "0x" + digest.hex(),
            "source": source, "destination": destination,
            "amount": str(Decimal(drops)/DROPS),
            "fee": str(Decimal(unsigned["Fee"])/DROPS),
            "expires_in_seconds": _EXPIRY_SECONDS,
            "status": "awaiting_wallet_signature"}


def _signed_from_compact(unsigned: dict, signature: object) -> tuple[str, str]:
    if (type(signature) is not str or not signature.startswith("0x")
            or len(signature) != 130):
        raise HTTPException(422, "Invalid XRP wallet signature format")
    try:
        raw = bytes.fromhex(signature[2:])
        r, s = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
        if not 0 < r < SECP_ORDER or not 0 < s < SECP_ORDER:
            raise ValueError()
        pub = bytes.fromhex(unsigned["SigningPubKey"])
        data = bytes.fromhex(encode_for_signing(unsigned))
        digest = hashlib.sha512(data).digest()[:32]
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256K1(), pub)
        key.verify(utils.encode_dss_signature(r, s), digest,
                   ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        signature_der = utils.encode_dss_signature(r, min(s, SECP_ORDER - s)).hex().upper()
        signed = {**unsigned, "TxnSignature": signature_der}
        wire = encode(signed)
        txid = hashlib.sha512(_TXN_PREFIX + bytes.fromhex(wire)).digest()[:32].hex().upper()
        return wire, txid
    except (ValueError, TypeError, KeyError, InvalidSignature):
        raise HTTPException(422, "XRP signature does not match this wallet and payment") from None


async def submit_user_signed_xrp(did: str, *, authorization: object, signature: object) -> dict:
    did = _safe_did(did)
    data = _unseal(authorization, did)
    unsigned = data.get("unsigned")
    if (type(unsigned) is not dict or
            set(unsigned) != {"TransactionType", "Account", "Destination", "Amount",
                              "Fee", "Sequence", "LastLedgerSequence", "Flags", "SigningPubKey"}):
        raise HTTPException(422, "Invalid XRP signing intent")
    wallet_id, public_key = await _owned(did, data.get("source"))
    if (wallet_id != data.get("wallet_id") or public_key != unsigned.get("SigningPubKey")
            or unsigned.get("Account") != data.get("source")
            or unsigned.get("TransactionType") != "Payment"
            or unsigned.get("Flags") != 0x80000000):
        raise HTTPException(403, "XRP wallet or payment has changed")
    wire, txid = _signed_from_compact(unsigned, signature)
    async with _LOCK:
        now = time.monotonic()
        for old, timestamp in list(_SEEN.items()):
            if now-timestamp > 600:
                _SEEN.pop(old, None)
        if txid in _SEEN or txid in _INFLIGHT:
            raise HTTPException(409, "This XRP transaction was already submitted")
        _INFLIGHT.add(txid)
    try:
        from .native_coin_balances import _rpc_url
        async with httpx.AsyncClient(timeout=httpx.Timeout(12), follow_redirects=False) as client:
            ledger = await _ledger_rpc(client, "ledger", [{"ledger_index": "validated"}])
            index = ledger.get("result", {}).get("ledger_index")
            if type(index) is str and index.isdigit():
                index = int(index)
            if type(index) is not int or index > unsigned["LastLedgerSequence"]:
                raise HTTPException(409, "XRP transaction expired; prepare another transfer")
            submit = await _ledger_rpc(client, "submit", [{"tx_blob": wire}])
            code = submit.get("result", {}).get("engine_result")
            if code != "tesSUCCESS":
                raise HTTPException(409, "XRP Ledger rejected the signed payment")
            # A preliminary tesSUCCESS is not final settlement.
            _SEEN[txid] = time.monotonic()
            return {"status": "submitted_pending_validation", "txid": txid,
                    "validated": False, "amount": str(Decimal(unsigned["Amount"])/DROPS)}
    finally:
        async with _LOCK:
            _INFLIGHT.discard(txid)
