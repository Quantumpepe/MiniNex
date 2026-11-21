"""XRPL locally signed Payment readback prevents destination/fee/signature edits."""
from __future__ import annotations

import base64
import copy

import pytest
from xrpl.core.binarycodec import encode, decode

from backend.native_xrp_privy_signed_readback import (
    inspect_original_xrp_signed_payment,
)
from backend.native_xrp_signed_wire import verify_original_xrp_detached_signature
from backend.tests.test_native_xrp_signed_wire import signed
from backend.tests.test_native_xrp_thor_payment_tx import NOW


def original(algorithm="secp256k1"):
    unsigned, pub, detached = signed(algorithm)
    checked = verify_original_xrp_detached_signature(
        unsigned, original_owner_public_key_hex=pub,
        detached_signature_hex=detached, now=NOW,
    )
    return unsigned, pub, base64.b64decode(checked["signed_wire_base64"])


@pytest.mark.parametrize("algorithm", ["secp256k1","ed25519"])
def test_original_signed_xrp_payment_retains_all_quoted_fields(algorithm):
    unsigned,public,wire=original(algorithm)
    result=inspect_original_xrp_signed_payment(
        unsigned, original_owner_public_key_hex=public,
        signed_payment_base64=base64.b64encode(wire).decode("ascii"),
        now=NOW,
    )
    assert result["mode"] == "ORIGINAL_XRP_SIGNED_PAYMENT_READBACK_ONLY"
    assert result["original_owner_signature_verified"] is True
    assert result["original_payment_fields_unchanged"] is True
    assert result["source_wallet"] == unsigned["source_wallet"]
    assert result["original_unsigned_payment_sha256"] == unsigned[
        "unsigned_payment_sha256"]
    assert len(result["signed_transaction_hash"]) == 64
    for key in (
        "privy_xrpl_wallet_signer_supported_and_verified",
        "original_order_send_claim_verified","broadcast_authorized",
        "source_transaction_sent","execution_enabled","can_submit",
    ):
        assert result[key] is False
    assert "signed_payment_base64" not in result


@pytest.mark.parametrize("change", [
    lambda t: t.update(Amount="1"),
    lambda t: t.update(Fee="1000"),
    lambda t: t.update(Flags=0),
    lambda t: t.update(Sequence=t["Sequence"]+1),
    lambda t: t.update(LastLedgerSequence=t["LastLedgerSequence"]+1),
    lambda t: t.update(Destination=t["Account"]),
    lambda t: t.update(TxnSignature="00"*64),
    lambda t: t.update(SigningPubKey="02"+"ab"*32),
])
def test_canonically_reencoded_xrpl_payment_edits_rejected(change):
    unsigned,pub,wire=original()
    payload=decode(wire.hex().upper())
    change(payload)
    tampered=bytes.fromhex(encode(payload))
    with pytest.raises(ValueError):
        inspect_original_xrp_signed_payment(
            unsigned,original_owner_public_key_hex=pub,
            signed_payment_base64=base64.b64encode(tampered).decode("ascii"),
            now=NOW,
        )


@pytest.mark.parametrize("mutation", [
    lambda r: r.__setitem__(0,r[0]^1),
    lambda r: r.__setitem__(-1,r[-1]^1),
    lambda r: r.extend(b"\x00"),
    lambda r: r.pop(),
])
def test_xrpl_untrusted_wire_cannot_bypass_codec_canonicality(mutation):
    unsigned,pub,wire=original("ed25519")
    changed=bytearray(wire)
    mutation(changed)
    with pytest.raises(ValueError):
        inspect_original_xrp_signed_payment(
            unsigned,original_owner_public_key_hex=pub,
            signed_payment_base64=base64.b64encode(changed).decode("ascii"),
            now=NOW,
        )


def test_xrp_owner_and_quote_are_revalidated_on_local_signed_readback():
    unsigned,pub,wire=original()
    with pytest.raises(ValueError):
        inspect_original_xrp_signed_payment(
            unsigned,original_owner_public_key_hex=pub,
            signed_payment_base64=base64.b64encode(wire).decode("ascii"),
            now=NOW+200,
        )
    tampered=copy.deepcopy(unsigned)
    tampered["unsigned_payment"]["Amount"]="1"
    with pytest.raises(ValueError):
        inspect_original_xrp_signed_payment(
            tampered,original_owner_public_key_hex=pub,
            signed_payment_base64=base64.b64encode(wire).decode("ascii"),
            now=NOW,
        )


def test_xrp_signed_payment_readback_does_not_submit_or_sign():
    import inspect as stdlib_inspect
    code=stdlib_inspect.getsource(inspect_original_xrp_signed_payment)
    for forbidden in (
        "PrivyClient","requests.post","httpx",
        "signDigest","sendTransaction","submit(", "private_key",
    ):
        assert forbidden not in code
