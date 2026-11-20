"""Native XRP source signing bytes match Privy owner and original THOR Payment."""
import hashlib
import copy

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from xrpl.core.addresscodec import encode_classic_address
from xrpl.core.binarycodec import encode_for_signing

from backend.native_xrp_thor_payment_tx import build_original_unsigned_xrp_thor_payment
from backend.native_xrp_signing_blob import encode_original_xrp_privy_signing_blob
from backend.tests.test_native_xrp_thor_payment_tx import (
    quote, account, network, OWNER, NOW, AMOUNT,
)


def original(algorithm="secp256k1"):
    if algorithm == "secp256k1":
        key = ec.derive_private_key(31, ec.SECP256K1())
        pub = key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.CompressedPoint,
        )
    else:
        key = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(1,33)))
        pub = b"\xed" + key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )
    owner = encode_classic_address(hashlib.new(
        "ripemd160", hashlib.sha256(pub).digest(),
    ).digest())
    q = quote()
    q["source_wallet"] = owner
    q["memo"] = q["memo"].replace("/" + OWNER + ":", "/" + owner + ":")
    a = account()
    a["result"]["account_data"]["Account"] = owner
    unsigned = build_original_unsigned_xrp_thor_payment(
        q, amount=AMOUNT, now=NOW,
        account_info=a, network_terms=network(),
    )
    return unsigned, pub.hex()


@pytest.mark.parametrize("algorithm", ["secp256k1", "ed25519"])
def test_xrpl_stx_signing_payload_exact_original_owner(algorithm):
    unsigned, pub = original(algorithm)
    report = encode_original_xrp_privy_signing_blob(
        unsigned, original_owner_public_key_hex=pub, now=NOW,
    )
    assert report["mode"] == "ORIGINAL_XRP_PRIVY_SIGNING_BLOB_READ_ONLY"
    assert report["signer_algorithm"] == algorithm
    assert report["source_wallet"] == unsigned["source_wallet"]
    assert report["inbound_vault"] == unsigned["inbound_vault"]
    assert report["destination_wallet"] == unsigned["destination_wallet"]
    assert report["source_amount_drops"] == unsigned["source_amount_drops"]
    assert report["signing_blob_hex"].startswith("53545800")
    assert len(report["signing_blob_sha512half_hex"]) == 64
    assert hashlib.sha512(bytes.fromhex(
        report["signing_blob_hex"]
    )).digest()[:32].hex() == report["signing_blob_sha512half_hex"]
    payment = {**unsigned["unsigned_payment"],
               "SigningPubKey": pub.upper()}
    assert report["signing_blob_hex"] == encode_for_signing(payment)
    assert unsigned["unsigned_payment"]["Flags"] == 0x80000000
    for flag in ("privy_remote_owner_policy_verified",
                 "wallet_signed", "source_transaction_sent",
                 "execution_enabled", "can_submit"):
        assert report[flag] is False


@pytest.mark.parametrize("mutation", [
    lambda u: u["unsigned_payment"].update(Amount="1"),
    lambda u: u["unsigned_payment"].update(Fee="15"),
    lambda u: u["unsigned_payment"].update(Flags=0),
    lambda u: u["unsigned_payment"].update(Destination=OWNER),
    lambda u: u["unsigned_payment"].update(
        LastLedgerSequence=u["unsigned_payment"]["LastLedgerSequence"] + 1),
    lambda u: u.update(unsigned_payment_sha256="0"*64),
    lambda u: u.update(source_wallet=OWNER),
    lambda u: u.update(wallet_signed=True),
    lambda u: u.update(can_submit=True),
    lambda u: u.update(execution_enabled=True),
])
def test_xrpl_cannot_sign_different_original_owner_or_payment(mutation):
    unsigned, pub = original()
    mutation(unsigned)
    with pytest.raises(ValueError):
        encode_original_xrp_privy_signing_blob(
            unsigned, original_owner_public_key_hex=pub, now=NOW,
        )


def test_xrpl_pubkey_does_not_repurpose_different_privy_wallet():
    unsigned, pub = original()
    other = ec.derive_private_key(32, ec.SECP256K1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint,
    ).hex()
    with pytest.raises(ValueError):
        encode_original_xrp_privy_signing_blob(
            unsigned, original_owner_public_key_hex=other, now=NOW,
        )


def test_xrpl_original_provider_quote_expiry_revalidated_at_signing():
    unsigned, pub = original()
    with pytest.raises(ValueError):
        encode_original_xrp_privy_signing_blob(
            unsigned, original_owner_public_key_hex=pub, now=NOW+200,
        )


def test_xrpl_signing_payload_generator_has_no_wallet_permission_or_network_action():
    import inspect
    text = inspect.getsource(encode_original_xrp_privy_signing_blob)
    for forbidden in ("private_key", "sendrawtransaction", "sign_and_send",
                      "requests.post", "psycopg", "wallet.rpc"):
        assert forbidden not in text
