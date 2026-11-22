"""Manual XRP payments from the currently authenticated Privy XRPL wallet.

Construct a complete XRP mainnet Payment from validated ledger data, ask
Privy to sign its canonical hex serialization, verify the signed transaction
against the owner and exact requested amount, and broadcast only that blob.
No private keys and no automatic trading authorization.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
import hashlib
import logging
import os
import re

import httpx
from fastapi import HTTPException
from xrpl.core.binarycodec import decode, encode

from .native_coin_balances import _rpc_url, _valid_address
from .native_xrp_signing_blob import _owner_pubkey
from .privy_wallet_verification import APP_ID, fetch_privy_user
from .storage import _safe_did

DROPS = Decimal(1_000_000)
LOG = logging.getLogger("mininex.xrp_manual_send")
_INTEGER = re.compile(r"^(?:0|[1-9][0-9]*)$")
_HEX = re.compile(r"^[0-9A-Fa-f]+$")
_WALLET_ID = re.compile(r"^[A-Za-z0-9_-]{6,160}$")
_TXN_PREFIX = bytes.fromhex("54584e00")
_locks: dict[str, asyncio.Lock] = {}



def privy_signing_failure(response: httpx.Response) -> HTTPException:
    """Classify Privy wallet authorization/schema issues without exposing raw responses.

    The user JWT proves their identity to MiniNex. It is not a Privy wallet
    authorization signature and cannot authorize this server to spend XRP.
    """
    status = response.status_code
    code = None
    try:
        payload = response.json()
        if type(payload) is dict:
            nested = payload.get("error")
            if type(nested) is dict:
                code = nested.get("code")
            elif type(nested) is str:
                code = nested
            if not code:
                code = payload.get("code")
    except (ValueError, TypeError):
        pass
    if type(code) is not str or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", code):
        code = "unknown"
    LOG.warning("Privy XRP sign refused: http_status=%d error_code=%s", status, code)
    if status in (401, 403):
        return HTTPException(403,
            "Privy has not authorized this server to sign your XRP wallet. "
            "Wallet-owner approval or a verified delegated signer is required.")
    if status in (400, 422):
        return HTTPException(502,
            f"Privy rejected XRP signing format (HTTP {status}, code {code})")
    if status == 404:
        return HTTPException(502, "Privy XRPL signing is unavailable for this wallet (404)")
    if status == 429:
        return HTTPException(503, "Privy is temporarily rate-limiting XRP signing")
    return HTTPException(502,
        f"Privy XRP signing failed (HTTP {status}, code {code})")


def amount_to_drops(amount: object) -> str:
    """Never round down a requested send or accept float precision loss."""
    if type(amount) not in (str, int, float):
        raise HTTPException(422, "Enter a valid XRP amount")
    raw = str(amount)
    if len(raw) > 45:
        raise HTTPException(422, "Enter a valid XRP amount")
    try:
        value = Decimal(raw) * DROPS
    except (ValueError, InvalidOperation):
        raise HTTPException(422, "Enter a valid XRP amount") from None
    if (not value.is_finite() or value != value.to_integral_value()
            or value <= 0 or value >= Decimal(10**20)):
        raise HTTPException(422, "XRP amount must be positive with at most 6 decimals")
    return str(int(value))


def original_xrp_wallet(user: dict, did: str, source: str) -> str:
    if (type(user) is not dict or user.get("id") != did
            or not _valid_address("XRP", source)):
        raise HTTPException(403, "Original XRP wallet could not be verified")
    linked = user.get("linked_accounts")
    if type(linked) is not list:
        raise HTTPException(503, "Privy-linked XRP wallets unavailable")
    matches = [
        acc for acc in linked
        if type(acc) is dict and acc.get("type") == "wallet"
        and acc.get("chain_type") == "xrpl"
        and acc.get("address") == source
        and acc.get("wallet_client_type", acc.get("wallet_client")) == "privy"
        and type(acc.get("id")) is str
        and _WALLET_ID.fullmatch(acc["id"])
    ]
    if len(matches) != 1:
        raise HTTPException(403, "XRP send wallet is not the signed-in Privy wallet")
    return matches[0]["id"]


def prepare_mainnet_payment(
    source: str, destination: str, drops: str,
    server: dict, fees: dict, account: dict,
) -> dict:
    """Use XRPL validated state, not user-selected fees or sequence values."""
    try:
        info = server["result"]["info"]
        ledger = info["validated_ledger"]
        fee_info = fees["result"]["drops"]
        acc_result = account["result"]
        account_data = acc_result["account_data"]
        index = ledger["seq"]
        age = ledger["age"]
        network_id = info.get("network_id", 0)
        base = Decimal(str(ledger["reserve_base_xrp"])) * DROPS
        increment = Decimal(str(ledger["reserve_inc_xrp"])) * DROPS
        fee = fee_info["open_ledger_fee"]
        balance = account_data["Balance"]
        sequence = account_data["Sequence"]
        owners = account_data["OwnerCount"]
    except (KeyError, TypeError, InvalidOperation, ValueError):
        raise HTTPException(503, "Current XRP Ledger state unavailable") from None
    if (network_id != 0 or type(index) is not int or not 1 <= index < 2**32 - 40
            or type(age) not in (float, int) or not 0 <= age <= 90
            or server["result"].get("status") != "success"
            or fees["result"].get("status") != "success"
            or acc_result.get("status") != "success"
            or acc_result.get("validated") is not True
            or account_data.get("Account") != source
            or type(sequence) is not int or not 0 < sequence < 2**32
            or type(owners) is not int or owners < 0
            or any(type(v) is not str or not _INTEGER.fullmatch(v)
                   for v in (drops, fee, balance))
            or not base.is_finite() or not increment.is_finite()
            or base < 0 or increment < 0
            or base != base.to_integral_value()
            or increment != increment.to_integral_value()):
        raise HTTPException(503, "Unverified XRP mainnet account or fees")
    # Fee safety checks only prevent unexpected fee burning; never impose
    # artificial capital or strategy limits.
    if not 0 < int(fee) <= 100_000:
        raise HTTPException(503, "XRP network fee temporarily unavailable")
    minimum_reserve = int(base + increment * owners)
    if int(balance) < int(drops) + int(fee) + minimum_reserve:
        raise HTTPException(422, "Not enough spendable XRP after ledger reserve and fee")
    return {
        "TransactionType": "Payment",
        "Account": source,
        "Destination": destination,
        "Amount": drops,
        "Fee": fee,
        "Sequence": sequence,
        "LastLedgerSequence": index + 20,
        "Flags": 0x80000000,
    }


def verified_signed_blob(privy_response: dict, unsigned: dict) -> tuple[str, str]:
    """Never broadcast a signed transaction with changed owner or terms."""
    data = privy_response.get("data") if type(privy_response) is dict else None
    if type(data) is not dict:
        raise HTTPException(502, "Privy returned no signed XRP transaction")
    tx_blob = data.get("signed_transaction")
    if (type(tx_blob) is not str or len(tx_blob) % 2
            or not 100 <= len(tx_blob) <= 4096 or not _HEX.fullmatch(tx_blob)):
        raise HTTPException(502, "Privy returned no valid signed XRP blob")
    try:
        signed = decode(tx_blob)
    except (ValueError, TypeError, KeyError):
        raise HTTPException(502, "Privy signed XRP transaction is malformed") from None
    if (set(signed) != set(unsigned) | {"SigningPubKey", "TxnSignature"}
            or any(signed.get(k) != v for k, v in unsigned.items())
            or type(signed.get("SigningPubKey")) is not str
            or type(signed.get("TxnSignature")) is not str):
        raise HTTPException(502, "Privy signed a different XRP transaction")
    try:
        _, signed_for = _owner_pubkey(signed["SigningPubKey"])
    except ValueError:
        raise HTTPException(502, "Privy XRP signing key is invalid") from None
    if signed_for != unsigned["Account"]:
        raise HTTPException(502, "Privy XRP signature is not from this wallet")
    if not signed["TxnSignature"] or not _HEX.fullmatch(signed["TxnSignature"]):
        raise HTTPException(502, "Privy XRP signature is missing")
    txid = hashlib.sha512(_TXN_PREFIX + bytes.fromhex(tx_blob)).digest()[:32].hex().upper()
    return tx_blob.upper(), txid


async def _ledger_rpc(client: httpx.AsyncClient, method: str, params: list) -> dict:
    try:
        response = await client.post(
            _rpc_url("XRP"), json={"method": method, "params": params},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        response.raise_for_status()
        if len(response.content) > 50_000:
            raise ValueError("Large XRP response")
        data = response.json()
        if type(data) is not dict or type(data.get("result")) is not dict:
            raise ValueError("Invalid XRP response")
        return data
    except (httpx.HTTPError, ValueError, TypeError):
        raise HTTPException(503, "XRP Ledger could not be reached") from None


async def submit_manual_xrp(
    did: str, *, source: object, destination: object, amount: object,
) -> dict:
    did = _safe_did(did)
    if (not _valid_address("XRP", source)
            or not _valid_address("XRP", destination)
            or source == destination):
        raise HTTPException(422, "Provide a different valid XRP destination")
    drops = amount_to_drops(amount)
    secret = os.environ.get("MININEX_PRIVY_APP_SECRET", "").strip()
    if not secret:
        raise HTTPException(503, "Privy server credentials are missing")
    user = await fetch_privy_user(did)
    wallet_id = original_xrp_wallet(user, did, source)
    # Prevent two concurrent manual sends with the same account sequence
    # on this backend instance; do not silently retry a broadcast.
    lock = _locks.setdefault(did, asyncio.Lock())
    async with lock:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(12.0), follow_redirects=False,
        ) as client:
            server = await _ledger_rpc(client, "server_info", [{}])
            fees = await _ledger_rpc(client, "fee", [{}])
            index = server["result"].get("info", {}).get("validated_ledger", {}).get("seq")
            if type(index) is not int:
                raise HTTPException(503, "Validated XRP ledger unavailable")
            account = await _ledger_rpc(
                client, "account_info", [{"account": source, "ledger_index": index, "strict": True}],
            )
            unsigned = prepare_mainnet_payment(source, destination, drops, server, fees, account)
            try:
                encoded = encode(unsigned)
            except (ValueError, TypeError, KeyError):
                raise HTTPException(503, "Cannot encode XRP payment") from None
            # Privy's XRPL method accepts a HEX string and explicit encoding;
            # passing a JSON object causes "Expected string, received object".
            try:
                response = await client.post(
                    f"https://api.privy.io/v1/wallets/{wallet_id}/rpc",
                    auth=(APP_ID, secret),
                    headers={"privy-app-id": APP_ID, "Content-Type": "application/json"},
                    json={
                        "method": "xrpl_signTransaction",
                        "params": {"transaction": encoded, "encoding": "hex"},
                    },
                )
            except httpx.HTTPError:
                raise HTTPException(503, "Privy XRP signing service unavailable") from None
            if response.status_code != 200:
                raise privy_signing_failure(response)
            try:
                payload = response.json()
            except ValueError:
                raise HTTPException(502, "Privy XRP signing response malformed") from None
            blob, txid = verified_signed_blob(payload, unsigned)
            submit = await _ledger_rpc(client, "submit", [{"tx_blob": blob}])
            result = submit["result"]
            code = result.get("engine_result")
            if code != "tesSUCCESS":
                raise HTTPException(409, f"XRP network rejected submission: {str(code)[:48]}")
            # tesSUCCESS is only preliminary. Never claim final ledger validation
            # without a validated transaction receipt.
            return {
                "status": "submitted", "validated": False, "txid": txid,
                "from": source, "to": destination,
                "amount": str(Decimal(drops) / DROPS),
            }
