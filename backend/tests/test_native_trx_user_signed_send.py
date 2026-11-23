"""Targeted offline checks of the user-approved native TRX send protocol."""
import hashlib
import time

import pytest
from eth_keys import keys
from fastapi import HTTPException

from backend.native_trx_user_signed_send import (
    _check_unsigned_transaction,
    _sealed_intent,
    _tron_address_from_public_key,
    _verify_intent,
    recover_tron_signature, solid_tron_receipt_confirmed,
)
from backend.native_trx_manual_send import tron_address_hex

KEY=keys.PrivateKey(bytes(range(1,33)))
OTHER=keys.PrivateKey(bytes(range(2,34)))
OWNER=_tron_address_from_public_key(KEY.public_key.to_bytes())
RECIPIENT=_tron_address_from_public_key(OTHER.public_key.to_bytes())


def test_tron_privy_compact_signature_recovers_exact_sender():
    digest="a7"*32
    compact=KEY.sign_msg_hash(bytes.fromhex(digest))
    signature="0x"+f"{compact.r:064x}{compact.s:064x}"
    tron=recover_tron_signature(digest,signature,OWNER)
    assert len(tron)==130
    assert tron[:128].lower()==signature[2:].lower()
    assert tron[-2:] in ("00","01")
    with pytest.raises(HTTPException) as exc:
        recover_tron_signature(digest,signature,RECIPIENT)
    assert exc.value.status_code==403
    with pytest.raises(HTTPException):
        recover_tron_signature("00"*32,signature,OWNER)


def _transaction(amount=1000000):
    raw={
        "contract":[{
            "parameter":{"value":{
                "owner_address":tron_address_hex(OWNER),
                "to_address":tron_address_hex(RECIPIENT),
                "amount":amount,
            }},
            "type":"TransferContract",
        }],
        "timestamp":int(time.time()*1000)-10000,
        "expiration":int(time.time()*1000)+90000,
    }
    # Validate txID over the exact raw_data_hex returned by the TRON node.
    blob="0a"+("11"*200)
    return {
        "txID":hashlib.sha256(bytes.fromhex(blob)).hexdigest(),
        "raw_data_hex":blob,
        "raw_data":raw,
    }


def test_unsigned_transaction_binds_amount_and_recipient():
    tx=_transaction()
    txid,verified=_check_unsigned_transaction(tx,OWNER,RECIPIENT,1000000)
    assert txid==tx["txID"].upper()
    assert verified["raw_data"]["contract"][0]["parameter"]["value"]["amount"]==1000000
    with pytest.raises(HTTPException):
        _check_unsigned_transaction(tx,OWNER,RECIPIENT,1000001)
    bad={**tx,"txID":"fe"*32}
    with pytest.raises(HTTPException):
        _check_unsigned_transaction(bad,OWNER,RECIPIENT,1000000)
    tx["raw_data"]["contract"][0]["type"]="AccountPermissionUpdateContract"
    with pytest.raises(HTTPException):
        _check_unsigned_transaction(tx,OWNER,RECIPIENT,1000000)


def test_tron_authorization_bound_to_authenticated_user(monkeypatch):
    monkeypatch.setenv("MININEX_PRIVY_APP_SECRET","local_test_secret_only_abcdefghijklmnopqrstuvwxyz")
    created=int(time.time())
    payload={"version":1,"did":"did:privy:test","created":created,
             "nonce":"11"*16,"source":OWNER}
    auth=_sealed_intent(payload)
    assert _verify_intent(auth,payload["did"])==payload
    with pytest.raises(HTTPException):
        _verify_intent(auth,"did:privy:someone_else")
    with pytest.raises(HTTPException):
        _verify_intent(auth[:-3]+"abc",payload["did"])
    with pytest.raises(HTTPException):
        _verify_intent(_sealed_intent({**payload,"created":created-200}),payload["did"])


def test_solidified_trx_confirmation_requires_owner_and_success():
    tx=_transaction()
    tx["ret"]=[{"contractRet":"SUCCESS"}]
    assert solid_tron_receipt_confirmed(tx["txID"],OWNER,tx)
    assert not solid_tron_receipt_confirmed(tx["txID"],RECIPIENT,tx)
    assert not solid_tron_receipt_confirmed("fa"*32,OWNER,tx)
    tx["ret"]=[{"contractRet":"FAILED"}]
    assert not solid_tron_receipt_confirmed(tx["txID"],OWNER,tx)
    tx["ret"]=[]
    assert solid_tron_receipt_confirmed(
        tx["txID"],OWNER,tx,{"id":tx["txID"],"blockNumber":123,
                            "receipt":{"result":"SUCCESS"}})
    assert not solid_tron_receipt_confirmed(
        tx["txID"],OWNER,tx,{"id":tx["txID"],"blockNumber":123,
                            "receipt":{"result":"FAILED"}})
