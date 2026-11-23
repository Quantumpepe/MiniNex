"""Manual TRX wallet authorization and raw-data safety regression tests."""
import pytest
from fastapi import HTTPException

from backend.native_trx_manual_send import (
    trx_to_sun, tron_address_hex, owner_tron_wallet,
)
from backend.native_coin_balances import _checksum, _BASE58


def make_tron(byte):
    raw = bytes([0x41]) + bytes([byte]) * 20
    data = raw + _checksum(raw)
    number = int.from_bytes(data, "big")
    digits = ""
    while number:
        number, mod = divmod(number, 58)
        digits = _BASE58[mod] + digits
    return digits


OWNER = make_tron(1)
DEST = make_tron(2)
DID = "did:privy:trx_user"


def test_trx_amount_exact_sun():
    assert trx_to_sun("1") == 1000000
    assert trx_to_sun("0.000001") == 1
    assert trx_to_sun("1.200001") == 1200001
    assert tron_address_hex(OWNER) == ("41" + "01" * 20)
    assert tron_address_hex(DEST) == ("41" + "02" * 20)


@pytest.mark.parametrize("amount", ["-1", "0", "1.0000001", "NaN", "",
                                  None, {}, "10000000000000000000"])
def test_trx_bad_amount_rejected(amount):
    with pytest.raises(HTTPException) as error:
        trx_to_sun(amount)
    assert error.value.status_code == 422


@pytest.mark.parametrize("address", ["Txxx", "0x41" + "01" * 20, "", None])
def test_trx_requires_real_checksummed_address(address):
    with pytest.raises(HTTPException) as error:
        tron_address_hex(address)
    assert error.value.status_code == 422


def test_trx_owner_wallet_exactly_matches_authenticated_user():
    user = {"id": DID, "linked_accounts": [{
        "type": "wallet", "chain_type": "tron",
        "wallet_client_type": "privy", "id": "wallet_123456789",
        "address": OWNER,
    }]}
    assert owner_tron_wallet(user, DID, OWNER) == "wallet_123456789"
    with pytest.raises(HTTPException):
        owner_tron_wallet(user, DID, DEST)
    with pytest.raises(HTTPException):
        owner_tron_wallet(user, "did:privy:other", OWNER)
    user["linked_accounts"][0]["address"] = DEST
    with pytest.raises(HTTPException):
        owner_tron_wallet(user, DID, OWNER)
