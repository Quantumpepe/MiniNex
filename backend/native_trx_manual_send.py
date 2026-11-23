"""Authenticated manual TRX transfer using the user's EXISTING Privy wallet.

No wallet creation, server secret export, synthetic transaction IDs or
privileged owner changes. Wallet action authorization remains Privy's job.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
import os
import httpx
from fastapi import HTTPException

from .native_coin_balances import _valid_address, _decode_b58, _BASE58, _checksum
from .privy_wallet_verification import APP_ID, fetch_privy_user
from .storage import _safe_did

_WALLET_ID = re.compile(r"^[A-Za-z0-9_-]{6,160}$")
_TX_ID = re.compile(r"^[0-9a-fA-F]{64}$")
_INTEGER = re.compile(r"^(?:0|[1-9][0-9]*)$")


def trx_to_sun(value: object) -> int:
    if type(value) not in (str, int, float):
        raise HTTPException(422, "Invalid TRX amount")
    raw = str(value)
    if len(raw) > 42:
        raise HTTPException(422, "Invalid TRX amount")
    try:
        sun = Decimal(raw) * 1000000
    except (InvalidOperation, ValueError):
        raise HTTPException(422, "Invalid TRX amount") from None
    if not sun.is_finite() or sun != sun.to_integral_value() or sun <= 0 or sun >= 2**63:
        raise HTTPException(422, "TRX amount must be positive with at most 6 decimals")
    return int(sun)


def tron_address_hex(value: object) -> str:
    if not _valid_address("TRX", value):
        raise HTTPException(422, "Invalid TRON address")
    decoded = _decode_b58(value, _BASE58)
    if (decoded is None or len(decoded) != 25 or decoded[0] != 0x41
            or decoded[-4:] != _checksum(decoded[:21])):
        raise HTTPException(422, "Invalid TRON address checksum")
    return decoded[:21].hex().upper()


def owner_tron_wallet(user: object, did: str, from_addr: str) -> str:
    if type(user) is not dict or user.get("id") != did:
        raise HTTPException(403, "TRX wallet user verification failed")
    linked = user.get("linked_accounts")
    if type(linked) is not list:
        raise HTTPException(503, "Privy TRX wallet links unavailable")
    wallets = [
        w for w in linked if type(w) is dict
        and w.get("type") == "wallet" and w.get("chain_type") == "tron"
        and w.get("wallet_client_type", w.get("wallet_client")) == "privy"
        and w.get("address") == from_addr and type(w.get("id")) is str
        and _WALLET_ID.fullmatch(w["id"])
    ]
    if len(wallets) != 1:
        raise HTTPException(403, "TRX sender is not the authenticated Privy wallet")
    return wallets[0]["id"]


async def send_manual_trx(
    did: str, *, source: object, destination: object, amount: object,
) -> dict:
    did = _safe_did(did)
    if not _valid_address("TRX", source) or not _valid_address("TRX", destination):
        raise HTTPException(422, "Provide valid TRON sender and recipient")
    if source == destination:
        raise HTTPException(422, "TRX recipient must differ from sender")
    sun = trx_to_sun(amount)
    user = await fetch_privy_user(did)
    wallet_id = owner_tron_wallet(user, did, source)
    from .privy_live_signer import fetch_privy_wallet_metadata
    from .native_xrp_manual_send import verify_privy_xrp_remote_owner, _same_privy_user_id
    remote = await fetch_privy_wallet_metadata(wallet_id)
    entity = remote.get("entity")
    if (remote.get("id") != wallet_id or remote.get("chain_type") != "tron"
            or remote.get("address") != source
            or remote.get("archived_at") is not None
            or (entity is not None and
                (type(entity) is not dict or entity.get("type") != "user"
                 or not _same_privy_user_id(entity.get("id"), did)))):
        raise HTTPException(403, "TRX wallet ownership could not be verified")
    secret = os.environ.get("MININEX_PRIVY_APP_SECRET", "").strip()
    if not secret:
        raise HTTPException(503, "Privy server credentials unavailable")
    body = {
        "method": "tron_sendTransaction",
        "params": {"raw_data": {"contract": [{
            "type": "TransferContract",
            "owner_address": tron_address_hex(source),
            "to_address": tron_address_hex(destination),
            "amount": sun,
        }]}},
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(12.0), follow_redirects=False,
        ) as client:
            response = await client.post(
                f"https://api.privy.io/v1/wallets/{wallet_id}/rpc",
                auth=(APP_ID, secret),
                headers={"privy-app-id": APP_ID, "Content-Type": "application/json"},
                json=body,
            )
    except httpx.HTTPError:
        raise HTTPException(503, "Privy TRX signing service unavailable") from None
    if response.status_code in (401, 403):
        raise HTTPException(403, "Privy has not authorized TRX signing for this wallet")
    if response.status_code != 200:
        raise HTTPException(502, f"Privy rejected TRX send (HTTP {response.status_code})")
    try:
        response_data = response.json()
        data = response_data["data"]
        txid = data.get("transaction_id")
        if type(txid) is not str or not _TX_ID.fullmatch(txid):
            raise ValueError("Invalid Privy TRX transaction ID")
        if type(data.get("hash")) is not str or not _TX_ID.fullmatch(data["hash"]):
            raise ValueError("Invalid Privy TRX hash")
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(502, "TRX submission outcome uncertain; check history") from None
    return {"status": "submitted", "txid": txid, "validated": False,
            "from": source, "to": destination, "amount": str(Decimal(sun) / 1000000)}
