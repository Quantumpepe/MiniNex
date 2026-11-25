"""Cryptographically bind original native Privy keys to their addresses."""
import hashlib

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from xrpl.core.addresscodec import encode_classic_address

from backend.native_privy_pubkey_binding import verify_native_public_key_owner
from backend.tests.test_native_btc_thor_outputs import _reference_address
from backend.tests.test_native_sol_thor_transfer_instructions import b58


def _hash160(data):
    return hashlib.new("ripemd160", hashlib.sha256(data).digest()).digest()


def _secp():
    key = ec.derive_private_key(4711, ec.SECP256K1())
    return key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.CompressedPoint,
    )


def test_real_original_btc_segwit_privy_pubkey_matches_address():
    public = _secp()
    owner = _reference_address(_hash160(public).hex())
    result = verify_native_public_key_owner(
        asset="BTC",owner=owner,public_key="0x"+public.hex())
    assert result["original_public_key_matches_wallet"] is True
    assert result["public_key_curve"] == "secp256k1"
    assert result["privy_native_wallet_api_supported"] is True
    assert len(result["public_key_sha256"]) == 64
    assert result["can_submit"] is False
    with pytest.raises(ValueError):
        verify_native_public_key_owner(
            asset="BTC",owner=_reference_address("22"*20),
            public_key=public.hex())


def test_sol_original_privy_ed25519_pubkey_exactly_matches_owner_account():
    public = ed25519.Ed25519PrivateKey.from_private_bytes(
        bytes(range(1,33))).public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    owner = b58(public)
    result = verify_native_public_key_owner(
        asset="SOL",owner=owner,public_key=public.hex())
    assert result["original_public_key_matches_wallet"] is True
    assert result["public_key_curve"] == "ed25519"
    assert result["privy_native_wallet_api_supported"] is True
    with pytest.raises(ValueError):
        verify_native_public_key_owner(
            asset="SOL",owner=b58(bytes([4])*32),public_key=public.hex())


@pytest.mark.parametrize("algorithm",["secp256k1","ed25519"])
def test_xrp_classic_pubkey_derivation_is_math_only_not_privy_xrpl_support(algorithm):
    if algorithm == "secp256k1":
        key = _secp()
    else:
        public = ed25519.Ed25519PrivateKey.from_private_bytes(
            bytes(range(1,33))).public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        key = b"\xed" + public
    owner = encode_classic_address(_hash160(key))
    result = verify_native_public_key_owner(
        asset="XRP",owner=owner,public_key=key.hex())
    assert result["original_public_key_matches_wallet"] is True
    assert result["public_key_curve"] == algorithm
    assert result["privy_native_wallet_api_supported"] is False
    assert result["privy_raw_signing_capability_verified"] is False
    assert result["execution_enabled"] is False


@pytest.mark.parametrize("asset,owner,key",[
    ("BTC","bc1q"+"q"*38,"02"+"aa"*32),
    ("BTC","not-a-btc-address","02"+"aa"*32),
    ("BTC","bc1q"+"q"*38,"ed"+"aa"*32),
    ("SOL","not-solana","aa"*32),
    ("SOL",b58(bytes([5])*32),"zz"*32),
    ("XRP","r"+ "a"*33,"02"+"aa"*32),
    ("DOGE","D"+"a"*33,"02"+"aa"*32),
])
def test_key_curve_or_address_spoof_fails_closed(asset,owner,key):
    with pytest.raises(ValueError):
        verify_native_public_key_owner(asset=asset,owner=owner,public_key=key)


def test_verified_pubkey_alone_never_confers_wallet_sign_or_spend():
    import inspect
    src = inspect.getsource(verify_native_public_key_owner)
    for phrase in ("sendrawtransaction", "privy_rpc", "signDigest",
                   "requests.post", "private_key"):
        assert phrase not in src


def test_tron_original_public_key_matches_native_address_only():
    from eth_keys import keys
    from backend.native_trx_user_signed_send import _tron_address_from_public_key
    key = keys.PrivateKey(bytes([1])*32).public_key
    owner = _tron_address_from_public_key(key.to_bytes())
    result = verify_native_public_key_owner(
        asset="TRX",owner=owner,public_key=key.to_compressed_bytes().hex())
    assert result["original_public_key_matches_wallet"] is True
    assert result["public_key_curve"] == "secp256k1"
    assert result["privy_remote_policy_verified"] is False
    assert result["can_submit"] is False
    other = keys.PrivateKey(bytes([2])*32).public_key
    with pytest.raises(ValueError):
        verify_native_public_key_owner(asset="TRX",owner=owner,
            public_key=other.to_compressed_bytes().hex())
