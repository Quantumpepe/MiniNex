"""No live database needed: private CLI test invariants."""
from inspect import getsource

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException

from backend import diagnostics


def test_public_p256_key_from_privy_style_env(monkeypatch):
    private = ec.generate_private_key(ec.SECP256R1())
    pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY", pem.replace("\n", "\\n"))
    diagnostics.test_public_key()


def test_missing_verification_key_fails_closed(monkeypatch):
    monkeypatch.delenv("MININEX_PRIVY_VERIFICATION_KEY", raising=False)
    with pytest.raises(RuntimeError):
        diagnostics.test_public_key()


def test_missing_and_invalid_bearer_rejected():
    diagnostics.test_unauthorized_rejected()


def test_diagnostic_always_cleans_only_its_own_account():
    src = getsource(diagnostics.test_temporary_db_account)
    assert "uuid4().hex" in src
    assert "WHERE privy_did=%s" in src
    assert "DELETE FROM mininex_accounts" in src
    assert "DELETE FROM mininex_asset_hwm" in src
    assert "test_temporary_db_account" in getsource(diagnostics.main)
