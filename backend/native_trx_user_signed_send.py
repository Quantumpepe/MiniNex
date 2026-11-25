"""Owner-authorized native TRX transfer: prepare -> Privy signRawHash -> broadcast.

The embedded Privy wallet signs exactly one TRON transaction hash after an
explicit user action. MiniNex never stores keys or signs on the user's behalf.
The returned 64-byte ECDSA signature is recovered to the verified TRON sender
and converted to the TRON 65-byte wire format before a single submission.
"""
from __future__ import annotations

import asyncio
import base64
from decimal import Decimal
import hashlib
import hmac
import json
import os
import re
import secrets
import time

import httpx
from eth_keys import keys
from eth_utils import keccak
from fastapi import HTTPException

from .native_coin_balances import _BASE58, _checksum, _valid_address
from .native_trx_manual_send import owner_tron_wallet, tron_address_hex, trx_to_sun
from .native_xrp_manual_send import _same_privy_user_id
from .privy_wallet_verification import fetch_privy_user
from .privy_live_signer import fetch_privy_wallet_metadata
from .storage import _safe_did

_HEX64=re.compile(r"^[a-fA-F0-9]{64}$")
_HEX=re.compile(r"^[a-fA-F0-9]+$")
_TRON_NODE="https://api.trongrid.io"
_CONTEXT=b"MiniNex manual TRX owner-approved transfer v1"
_INTENT_SECONDS=150
_PENDING:set[str]=set()
_SUBMITTED:dict[str,float]={}
_LOCK=asyncio.Lock()


def _authorization_key()->bytes:
    app_secret=os.environ.get("MININEX_PRIVY_APP_SECRET","").strip()
    if len(app_secret)<16:
        raise HTTPException(503,"Privy server credentials unavailable")
    return hmac.new(app_secret.encode(),_CONTEXT,hashlib.sha256).digest()


def _sealed_intent(data:dict)->str:
    raw=json.dumps(data,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()
    if len(raw)>10000:
        raise HTTPException(503,"TRX transaction too large")
    auth=hmac.new(_authorization_key(),raw,hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw+auth).decode().rstrip("=")


def _verify_intent(token:object,did:str)->dict:
    if type(token) is not str or not 80<=len(token)<=14000:
        raise HTTPException(422,"TRX approval has expired")
    try:
        wire=base64.b64decode(token+"="*(-len(token)%4),altchars=b"-_",validate=True)
        if not 32<len(wire)<=10032:
            raise ValueError("length")
        raw,mac=wire[:-32],wire[-32:]
        digest=hmac.new(_authorization_key(),raw,hashlib.sha256).digest()
        if not hmac.compare_digest(digest,mac):
            raise ValueError("signature")
        data=json.loads(raw)
        now=int(time.time())
        if (type(data) is not dict or data.get("version")!=1
                or data.get("did")!=did or type(data.get("created")) is not int
                or not 0<=now-data["created"]<=_INTENT_SECONDS
                or type(data.get("nonce")) is not str or len(data["nonce"])!=32):
            raise ValueError("intent")
        return data
    except (ValueError,TypeError,IndexError,KeyError):
        raise HTTPException(422,"TRX approval has expired or is invalid") from None


def _tron_address_from_public_key(public_key:bytes)->str:
    if len(public_key)!=64:
        raise ValueError("Unexpected secp256k1 key")
    raw=b"\x41"+keccak(public_key)[-20:]
    packed=raw+_checksum(raw)
    value=int.from_bytes(packed,"big")
    out=""
    while value:
        value,rem=divmod(value,58)
        out=_BASE58[rem]+out
    for char in packed:
        if char!=0:
            break
        out=_BASE58[0]+out
    return out


def recover_tron_signature(hash_hex:object,signature:object,source:str)->str:
    """Only return signed wire after cryptographically recovering OWNER."""
    if (type(hash_hex) is not str or not _HEX64.fullmatch(hash_hex)
            or type(signature) is not str or not signature.startswith("0x")
            or len(signature)!=130 or not _HEX.fullmatch(signature[2:])
            or not _valid_address("TRX",source)):
        raise HTTPException(422,"Invalid TRX wallet signature")
    digest=bytes.fromhex(hash_hex)
    raw=bytes.fromhex(signature[2:])
    r=int.from_bytes(raw[:32],"big")
    s=int.from_bytes(raw[32:],"big")
    for recovery in (0,1):
        try:
            sig=keys.Signature(vrs=(recovery,r,s))
            pub=sig.recover_public_key_from_msg_hash(digest)
            if _tron_address_from_public_key(pub.to_bytes())==source:
                return (raw+bytes([recovery])).hex().upper()
        except (ValueError,TypeError):
            continue
    raise HTTPException(403,"TRX wallet signature does not match your wallet")


async def _owned_wallet(did:str,source:object)->str:
    if not _valid_address("TRX",source):
        raise HTTPException(422,"Invalid TRX sending wallet")
    user=await fetch_privy_user(did)
    wallet_id=owner_tron_wallet(user,did,source)
    remote=await fetch_privy_wallet_metadata(wallet_id)
    entity=remote.get("entity") if type(remote) is dict else None
    if (type(remote) is not dict or remote.get("id")!=wallet_id
            or remote.get("chain_type")!="tron" or remote.get("address")!=source
            or remote.get("archived_at") is not None
            or (entity is not None and
                (type(entity) is not dict or entity.get("type")!="user"
                 or not _same_privy_user_id(entity.get("id"),did)))):
        raise HTTPException(403,"TRX wallet owner could not be verified")
    return wallet_id


def _check_unsigned_transaction(tx:object,source:str,destination:str,sun:int,*,reference_ms:int|None=None)->tuple[str,dict]:
    if type(tx) is not dict or type(tx.get("raw_data")) is not dict:
        raise HTTPException(503,"TRON node did not prepare a transaction")
    txid=tx.get("txID")
    raw_data_hex=tx.get("raw_data_hex")
    if (type(txid) is not str or not _HEX64.fullmatch(txid)
            or type(raw_data_hex) is not str or not 64<=len(raw_data_hex)<=10000
            or len(raw_data_hex)%2 or not _HEX.fullmatch(raw_data_hex)
            or hashlib.sha256(bytes.fromhex(raw_data_hex)).hexdigest().lower()!=txid.lower()):
        raise HTTPException(503,"TRON transaction hash does not match raw data")
    raw=tx["raw_data"]
    contracts=raw.get("contract")
    if type(contracts) is not list or len(contracts)!=1 or type(contracts[0]) is not dict:
        raise HTTPException(503,"TRON transfer contract was not verified")
    entry=contracts[0]
    if (entry.get("type")!="TransferContract" or type(entry.get("parameter")) is not dict
            or type(entry["parameter"].get("value")) is not dict):
        raise HTTPException(503,"Unexpected TRON transaction type")
    value=entry["parameter"]["value"]
    owner=tron_address_hex(source)
    to=tron_address_hex(destination)
    if (str(value.get("owner_address","")).upper()!=owner
            or str(value.get("to_address","")).upper()!=to
            or value.get("amount")!=sun):
        raise HTTPException(503,"TRON transaction terms differ from your request")
    now_ms=int(time.time()*1000) if reference_ms is None else reference_ms
    if (type(now_ms) is not int or now_ms<=0 or type(raw.get("timestamp")) is not int or type(raw.get("expiration")) is not int
            or not now_ms-180000<=raw["timestamp"]<=now_ms+30000
            or not now_ms+5000<=raw["expiration"]<=now_ms+3600000):
        raise HTTPException(503,"TRON transaction timestamp or expiry unavailable")
    # Persist the exact RPC transaction object that produced this digest.
    unsigned={k:v for k,v in tx.items() if k in ("txID","raw_data","raw_data_hex","visible")}
    return txid.upper(),unsigned


async def _tron_rpc(client:httpx.AsyncClient,path:str,payload:dict)->dict:
    try:
        response=await client.post(
            _TRON_NODE+path,json=payload,
            headers={"Accept":"application/json","Content-Type":"application/json"},
        )
        response.raise_for_status()
        if len(response.content)>120000:
            raise ValueError("oversized response")
        result=response.json()
        if type(result) is not dict:
            raise ValueError("invalid response")
        return result
    except (httpx.HTTPError,ValueError,TypeError):
        raise HTTPException(503,"TRON network temporarily unavailable") from None


async def prepare_owner_trx(did:str,*,source:object,destination:object,amount:object)->dict:
    did=_safe_did(did)
    if (not _valid_address("TRX",source) or not _valid_address("TRX",destination)
            or source==destination):
        raise HTTPException(422,"Choose a different valid TRX recipient")
    sun=trx_to_sun(amount)
    wallet_id=await _owned_wallet(did,source)
    async with httpx.AsyncClient(timeout=httpx.Timeout(12),follow_redirects=False) as client:
        tx=await _tron_rpc(client,"/wallet/createtransaction",{
            "owner_address":tron_address_hex(source),
            "to_address":tron_address_hex(destination),
            "amount":sun,
            "visible":False,
        })
    txid,unsigned=_check_unsigned_transaction(tx,source,destination,sun)
    token=_sealed_intent({
        "version":1,"did":did,"wallet_id":wallet_id,
        "source":source,"destination":destination,"sun":sun,
        "created":int(time.time()),"nonce":secrets.token_hex(16),
        "txid":txid,"unsigned":unsigned,
    })
    return {
        "status":"awaiting_wallet_signature","authorization":token,
        "hash":"0x"+txid.lower(),"source":source,
        "destination":destination,"amount":str(Decimal(sun)/Decimal(1000000)),
        "expires_in_seconds":max(0,min(_INTENT_SECONDS,int((unsigned["raw_data"]["expiration"]-int(time.time()*1000))/1000)-5)),
    }


async def submit_owner_signed_trx(
    did:str,*,authorization:object,signature:object,
)->dict:
    did=_safe_did(did)
    intent=_verify_intent(authorization,did)
    source=intent.get("source")
    destination=intent.get("destination")
    sun=intent.get("sun")
    if (not _valid_address("TRX",source) or not _valid_address("TRX",destination)
            or type(sun) is not int or sun<=0 or source==destination):
        raise HTTPException(422,"Invalid TRX authorization")
    wallet_id=await _owned_wallet(did,source)
    if wallet_id!=intent.get("wallet_id"):
        raise HTTPException(403,"TRX wallet has changed since approval")
    txid,unsigned=_check_unsigned_transaction(intent.get("unsigned"),source,destination,sun)
    if txid!=intent.get("txid"):
        raise HTTPException(422,"TRX transaction terms changed")
    signature_hex=recover_tron_signature(txid,signature,source)
    signed={**unsigned,"signature":[signature_hex]}
    async with _LOCK:
        now=time.monotonic()
        for old,at in list(_SUBMITTED.items()):
            if now-at>900:
                _SUBMITTED.pop(old,None)
        if txid in _PENDING or txid in _SUBMITTED:
            raise HTTPException(409,"TRX transfer already submitted; check wallet balance")
        _PENDING.add(txid)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12),follow_redirects=False) as client:
            result=await _tron_rpc(client,"/wallet/broadcasttransaction",signed)
        if result.get("result") is not True:
            raise HTTPException(409,"TRON network did not accept this transaction")
        returned=result.get("txid")
        if returned is not None and (type(returned) is not str or returned.upper()!=txid):
            raise HTTPException(502,"TRON broadcast transaction ID mismatch")
        _SUBMITTED[txid]=time.monotonic()
        return {"status":"submitted_pending_validation","validated":False,
                "txid":txid,"amount":str(Decimal(sun)/Decimal(1000000))}
    finally:
        async with _LOCK:
            _PENDING.discard(txid)


def solid_tron_receipt_confirmed(
    txid:str,source:str,transaction:object,info:object=None,
)->bool:
    """Prove sender + finalized success; never infer it from broadcast status."""
    if (not _HEX64.fullmatch(txid) or not _valid_address("TRX",source)
            or type(transaction) is not dict
            or str(transaction.get("txID","")).lower()!=txid.lower()):
        return False
    try:
        entry=transaction["raw_data"]["contract"][0]
        if (entry["type"]!="TransferContract"
                or str(entry["parameter"]["value"]["owner_address"]).upper()!=tron_address_hex(source)):
            return False
    except (KeyError,TypeError,IndexError):
        return False
    # walletsolidity/gettransactionbyid returns only solidified transactions.
    # A successful TransferContract ret is sufficient without an info record.
    returns=transaction.get("ret")
    if (type(returns) is list and len(returns)==1
            and type(returns[0]) is dict):
        return returns[0].get("contractRet")=="SUCCESS"
    if (type(info) is dict and str(info.get("id","")).lower()==txid.lower()
            and type(info.get("blockNumber")) is int and info["blockNumber"]>0
            and type(info.get("receipt")) is dict):
        return info["receipt"].get("result")=="SUCCESS"
    return False


async def confirmed_owner_trx(did:str,txid:str)->dict:
    did=_safe_did(did)
    if type(txid) is not str or not _HEX64.fullmatch(txid):
        raise HTTPException(422,"Invalid TRX transaction ID")
    user=await fetch_privy_user(did)
    from .native_coin_balances import verified_native_addresses
    source=verified_native_addresses(user,did).get("TRX")
    if not source:
        raise HTTPException(403,"TRX wallet was not verified")
    async with httpx.AsyncClient(timeout=httpx.Timeout(12),follow_redirects=False) as client:
        # The solidified transaction plus contractRet is the primary proof.
        transaction=await _tron_rpc(client,"/walletsolidity/gettransactionbyid",{"value":txid})
        if solid_tron_receipt_confirmed(txid,source,transaction):
            return {"confirmed":True}
        info=await _tron_rpc(client,"/walletsolidity/gettransactioninfobyid",{"value":txid})
    return {"confirmed":solid_tron_receipt_confirmed(txid,source,transaction,info)}
