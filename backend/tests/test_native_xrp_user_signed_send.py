"""Focused offline verification of user-approved XRPL send cryptography."""
import hashlib
from decimal import Decimal

import pytest
from fastapi import HTTPException
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from xrpl.core.addresscodec import encode_classic_address
from xrpl.core.binarycodec import decode, encode_for_signing

from backend.native_xrp_user_signed_send import _seal, _unseal, _signed_from_compact, prepare_xrp
from backend.tests.test_native_xrp_manual_send import VAULT

DID="did:privy:test_xrp_signer"


def fixture_key_and_payment():
    private=ec.derive_private_key(13, ec.SECP256K1())
    pub=private.public_key().public_bytes(
        __import__("cryptography.hazmat.primitives.serialization",fromlist=["Encoding"]).Encoding.X962,
        __import__("cryptography.hazmat.primitives.serialization",fromlist=["PublicFormat"]).PublicFormat.CompressedPoint,
    )
    digest=hashlib.new("ripemd160",hashlib.sha256(pub).digest()).digest()
    address=encode_classic_address(digest)
    payment={"TransactionType":"Payment","Account":address,"Destination":VAULT,
             "Amount":"1000000","Fee":"12","Sequence":4,
             "LastLedgerSequence":100001,"Flags":0x80000000,
             "SigningPubKey":pub.hex().upper()}
    signing=bytes.fromhex(encode_for_signing(payment))
    assert signing.startswith(bytes.fromhex("53545800"))
    digest=hashlib.sha512(signing).digest()[:32]
    der=private.sign(digest,ec.ECDSA(utils.Prehashed(hashes.SHA256())))
    r,s=utils.decode_dss_signature(der)
    compact="0x"+r.to_bytes(32,"big").hex()+s.to_bytes(32,"big").hex()
    return payment,compact


def test_exact_approved_hash_signature_generates_canonical_xrp_payment():
    payment,compact=fixture_key_and_payment()
    wire,txid=_signed_from_compact(payment,compact)
    tx=decode(wire)
    assert tx["Account"]==payment["Account"]
    assert tx["Destination"]==payment["Destination"]
    assert tx["Amount"]=="1000000"
    assert tx["Fee"]=="12"
    assert tx["SigningPubKey"]==payment["SigningPubKey"]
    assert len(tx["TxnSignature"])>=130
    assert len(txid)==64


def test_modified_amount_rejected_before_broadcast():
    payment,compact=fixture_key_and_payment()
    changed={**payment,"Amount":"1000001"}
    with pytest.raises(HTTPException) as exc:
        _signed_from_compact(changed,compact)
    assert exc.value.status_code==422


def test_signature_bad_length_rejected():
    payment,_=fixture_key_and_payment()
    with pytest.raises(HTTPException):
        _signed_from_compact(payment,"0x123")


def test_signed_authorization_binds_privy_did_and_expires(monkeypatch):
    monkeypatch.setenv("MININEX_PRIVY_APP_SECRET","test_secret_only_for_offline_checks")
    monkeypatch.setattr("backend.native_xrp_user_signed_send.time.time",lambda:10000)
    token=_seal({"ver":1,"did":DID,"created":10000,"nonce":"0123456789abcdef0123456789abcdef"})
    assert _unseal(token,DID)["did"]==DID
    with pytest.raises(HTTPException):
        _unseal(token,"did:privy:other")
    monkeypatch.setattr("backend.native_xrp_user_signed_send.time.time",lambda:10200)
    with pytest.raises(HTTPException):
        _unseal(token,DID)
    with pytest.raises(HTTPException):
        _unseal(token[:-2]+"AA",DID)
