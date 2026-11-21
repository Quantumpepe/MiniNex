"""Original signed SOL transaction readback: no mutation or broadcast."""
import base64
import copy

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from backend.native_sol_compiled_message import compile_original_unsigned_sol_message
from backend.native_sol_privy_signed_readback import (
    inspect_original_sol_signed_transaction,
)
from backend.tests.test_native_sol_compiled_message import params, b58


def original_signed():
    private = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )
    args = params()
    old = args["quote"]["source_wallet"]
    owner = b58(public)
    args["quote"]["source_wallet"] = owner
    args["quote"]["memo"] = args["quote"]["memo"].replace(
        "/" + old + ":", "/" + owner + ":",
    )
    args["source_account"]["owner"] = owner
    manifest = compile_original_unsigned_sol_message(**args)
    message = base64.b64decode(manifest["message_base64"],validate=True)
    signature = private.sign(message)
    wire = b"\x01" + signature + message
    return manifest,wire


def test_privy_signed_sol_readback_verifies_owner_memo_transfer_and_original_bytes():
    original,wire = original_signed()
    result = inspect_original_sol_signed_transaction(
        original,signed_transaction_base64=base64.b64encode(wire).decode("ascii"))
    assert result["mode"] == "PRIVY_SOL_ORIGINAL_SIGNED_TX_READBACK_ONLY"
    assert result["original_owner_signature_verified"] is True
    assert result["original_refund_memo_and_vault_verified"] is True
    assert result["original_message_unchanged"] is True
    assert result["source_wallet"] == original["source_wallet"]
    assert result["original_message_sha256"] == original["message_sha256"]
    assert result["source_amount_lamports"] == original["source_amount_lamports"]
    for blocked in (
        "privy_remote_policy_independently_verified",
        "original_order_send_claim_verified","broadcast_authorized",
        "source_transaction_sent","execution_enabled","can_submit",
    ):
        assert result[blocked] is False
    assert "signed_wire_base64" not in result


@pytest.mark.parametrize("mutate",[
    lambda b: b.__setitem__(0,2),
    lambda b: b.__setitem__(10,b[10]^1),
    lambda b: b.__setitem__(65,b[65]^1),
    lambda b: b.__setitem__(-1,b[-1]^1),
    lambda b: b.extend(b"\x00"),
    lambda b: b.pop(),
])
def test_wrong_original_signer_signature_message_or_trailing_bytes_rejected(mutate):
    original,wire = original_signed()
    tampered = bytearray(wire)
    mutate(tampered)
    with pytest.raises(ValueError):
        inspect_original_sol_signed_transaction(
            original,
            signed_transaction_base64=base64.b64encode(tampered).decode("ascii"))


def test_no_signer_response_or_unsigned_placeholder_is_not_signed():
    original,wire=original_signed()
    for wrong in (
        "not-base64-***",
        base64.b64encode(b"\x01"+bytes(64)+wire[65:]).decode("ascii"),
        base64.b64encode(wire[65:]).decode("ascii"),
    ):
        with pytest.raises(ValueError):
            inspect_original_sol_signed_transaction(
                original,signed_transaction_base64=wrong)


@pytest.mark.parametrize("field,value",[
    ("source_wallet","wrong"),
    ("message_sha256","0"*64),
    ("source_amount_lamports","1"),
    ("inbound_vault","other"),
    ("refund_memo","not original"),
    ("wallet_signed",True),
    ("can_submit",True),
    ("execution_enabled",True),
])
def test_original_vault_amount_refund_and_authority_not_substitutable(field,value):
    original,wire=original_signed()
    original=copy.deepcopy(original)
    original[field]=value
    with pytest.raises(ValueError):
        inspect_original_sol_signed_transaction(
            original,signed_transaction_base64=base64.b64encode(wire).decode("ascii"))


def test_privy_signed_tx_inspector_has_no_broadcast_or_policy_post():
    import inspect
    source=inspect.getsource(inspect_original_sol_signed_transaction)
    for forbidden in (
        "requests.post","httpx","sendTransaction","signTransaction",
        "private_key","psycopg","broadcast(",
    ):
        assert forbidden not in source
