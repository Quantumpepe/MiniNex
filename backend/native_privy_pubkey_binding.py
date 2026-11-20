"""Cryptographic address binding for original Privy native public keys.

No key generation, signing, address reassignment, policy publishing or funds
movement. Privy GET /v1/wallets may contain "public_key" (a compressed/raw
curve public key). Linking an address to a user is not enough to prove the
signer controls it: verify the public key against the on-chain address.

XRP's curve math is supported as a *verification primitive only*. Privy's
documented wallet API currently does not expose an "xrpl" chain_type, so a
valid XRP signature/address is not evidence of an available native Privy
XRP wallet, delegated signer or production policy support.
"""
from __future__ import annotations

import hashlib
import re

from cryptography.hazmat.primitives.asymmetric import ec
from xrpl.core.addresscodec import encode_classic_address

from .native_btc_thor_outputs import _mainnet_p2wpkh_script
from .native_coin_balances import _decode_b58, _BASE58, _valid_address

_HEX = re.compile(r"^(?:0x)?[0-9a-fA-F]+$")


def _raw_key(value: object, length: int) -> bytes:
    if type(value) is not str or not _HEX.fullmatch(value):
        raise ValueError("Privy compressed public key unavailable")
    raw = value[2:] if value.startswith("0x") else value
    if len(raw) != length * 2:
        raise ValueError("Privy public key curve length invalid")
    return bytes.fromhex(raw)


def verify_native_public_key_owner(
    *, asset: str, owner: str, public_key: object,
) -> dict:
    """Return only non-authorizing cryptographic ownership verification."""
    if asset not in ("BTC", "SOL", "XRP") or type(owner) is not str:
        raise ValueError("Unrecognized original native wallet asset")
    if asset == "SOL":
        key = _raw_key(public_key, 32)
        decoded = _decode_b58(owner, _BASE58)
        if decoded != key:
            raise ValueError("Privy Solana public key differs from original wallet")
        curve = "ed25519"
    else:
        key = _raw_key(public_key, 33)
        if key[0] not in (2, 3, 0xed):
            raise ValueError("Privy native curve public key malformed")
        if key[0] in (2, 3):
            try:
                ec.EllipticCurvePublicKey.from_encoded_point(
                    ec.SECP256K1(), key,
                )
            except ValueError:
                raise ValueError("Invalid native secp256k1 public point") from None
        if asset == "BTC":
            if key[0] == 0xed:
                raise ValueError("Bitcoin SegWit requires secp256k1 public key")
            program = hashlib.new(
                "ripemd160", hashlib.sha256(key).digest(),
            ).digest()
            if _mainnet_p2wpkh_script(owner) != bytes((0, 20)) + program:
                raise ValueError("Bitcoin SegWit owner key does not match address")
            curve = "secp256k1"
        else:
            if not _valid_address("XRP", owner):
                raise ValueError("Malformed original XRP classic address")
            account_id = hashlib.new(
                "ripemd160", hashlib.sha256(key).digest(),
            ).digest()
            if encode_classic_address(account_id) != owner:
                raise ValueError("Original XRP account key differs from address")
            curve = "ed25519" if key[0] == 0xed else "secp256k1"
    return {
        "asset": asset,
        "original_wallet_address": owner,
        "public_key_curve": curve,
        "original_public_key_matches_wallet": True,
        "public_key_sha256": hashlib.sha256(key).hexdigest(),
        "privy_native_wallet_api_supported": asset in ("BTC", "SOL"),
        "privy_remote_policy_verified": False,
        "privy_raw_signing_capability_verified": False,
        "user_trade_consent_verified": False,
        "execution_enabled": False,
        "can_submit": False,
    }
