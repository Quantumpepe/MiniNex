"""Detached signature verification never confers live trade authority."""
import base64
import copy
import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from backend.native_sol_signed_wire import verify_original_sol_detached_signature
from backend.native_sol_compiled_message import compile_original_unsigned_sol_message
from backend.tests.test_native_sol_compiled_message import params, b58


def valid():
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    p = params()
    owner = b58(raw)
    p["quote"]["source_wallet"] = owner
    p["source_account"]["owner"] = owner
    memo = p["quote"]["memo"]
    old = memo.split("/")[1].split(":")[0]
    p["quote"]["memo"] = memo.replace("/" + old + ":", "/" + owner + ":")
    unsigned = compile_original_unsigned_sol_message(**p)
    message = base64.b64decode(unsigned["message_base64"])
    signature = base64.b64encode(key.sign(message)).decode("ascii")
    return unsigned, signature, key


def test_exact_owner_signature_verifies_without_broadcast_authority():
    unsigned, signature, _ = valid()
    verified = verify_original_sol_detached_signature(
        unsigned, owner_signature_base64=signature)
    wire = base64.b64decode(verified["wire_base64"])
    assert wire[0] == 1
    assert wire[1:65] == base64.b64decode(signature)
    assert wire[65:] == base64.b64decode(unsigned["message_base64"])
    assert verified["wire_sha256"] == hashlib.sha256(wire).hexdigest()
    assert verified["detached_ed25519_signature_valid"] is True
    assert verified["privy_policy_independently_verified"] is False
    assert verified["broadcast_authorized"] is False
    assert verified["source_transaction_sent"] is False
    assert verified["can_submit"] is False


def test_wrong_signer_or_modified_message_rejected():
    unsigned, signature, key = valid()
    other = Ed25519PrivateKey.generate()
    wrong = base64.b64encode(other.sign(
        base64.b64decode(unsigned["message_base64"]))).decode()
    with pytest.raises(ValueError):
        verify_original_sol_detached_signature(
            unsigned, owner_signature_base64=wrong)
    tampered = copy.deepcopy(unsigned)
    tampered["message_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        verify_original_sol_detached_signature(
            tampered, owner_signature_base64=signature)
    changed = copy.deepcopy(unsigned)
    changed["source_wallet"] = b58(bytes([4]) * 32)
    changed["required_signers"] = [changed["source_wallet"]]
    with pytest.raises(ValueError):
        verify_original_sol_detached_signature(
            changed, owner_signature_base64=signature)


@pytest.mark.parametrize("field,value", [
    ("can_submit", True),
    ("wallet_signed", True),
    ("execution_enabled", True),
    ("source_transaction_sent", True),
    ("privy_signer_grant_verified", True),
    ("ready_to_submit", True),
    ("mode", "ARBITRARY_MESSAGE"),
])
def test_cannot_relabel_untrusted_message_as_executable(field, value):
    unsigned, signature, _ = valid()
    unsigned[field] = value
    with pytest.raises(ValueError):
        verify_original_sol_detached_signature(
            unsigned, owner_signature_base64=signature)


@pytest.mark.parametrize("signature", ["", "!!", "AA==", "A" * 150])
def test_invalid_signature_encoding_rejected(signature):
    unsigned, _, _ = valid()
    with pytest.raises(ValueError):
        verify_original_sol_detached_signature(
            unsigned, owner_signature_base64=signature)
