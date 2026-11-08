"""Offline checks for Privy authentication. No Privy API credentials required."""
import os
import time
from datetime import datetime, timezone, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException

from backend.auth import verify_privy_bearer


@pytest.fixture
def keys():
    private = ec.generate_private_key(ec.SECP256R1())
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private, public_pem


def signed_token(private, **changes):
    now = int(time.time())
    claims = {
        "sub": "did:privy:test-user",
        "sid": "session-123",
        "iss": "privy.io",
        "aud": "cmw7sb7ty013o0gl55aqyzi3y",
        "iat": now,
        "exp": now+3600,
    }
    claims.update(changes)
    return jwt.encode(claims, private, algorithm="ES256")


def verify(token):
    return verify_privy_bearer("Bearer "+token)


def test_absent_or_invalid_header_fails_closed(monkeypatch):
    monkeypatch.delenv("MININEX_PRIVY_VERIFICATION_KEY",raising=False)
    for header in (None, "", "foo", "Bearer ", "Basic abc", "Bearer abc def"):
        with pytest.raises(HTTPException) as exc:
            verify_privy_bearer(header)
        assert exc.value.status_code == 401


def test_unconfigured_server_fails_closed(monkeypatch, keys):
    private,_ = keys
    monkeypatch.delenv("MININEX_PRIVY_VERIFICATION_KEY",raising=False)
    with pytest.raises(HTTPException) as exc:
        verify(signed_token(private))
    assert exc.value.status_code == 503


def test_good_token_returns_only_privy_identity(monkeypatch, keys):
    private,public = keys
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",public)
    user=verify(signed_token(private))
    assert user == {"user_id":"did:privy:test-user","session_id":"session-123"}


@pytest.mark.parametrize("changes", [
    {"aud":"other-application"},
    {"iss":"unknown-issuer"},
    {"exp":int(time.time())-600},
    {"sub":"wallet:fake"},
    {"sid":""},
])
def test_wrong_claims_rejected(monkeypatch,keys,changes):
    private,public=keys
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",public)
    with pytest.raises(HTTPException) as exc:
        verify(signed_token(private,**changes))
    assert exc.value.status_code==401


def test_signature_from_other_key_rejected(monkeypatch,keys):
    _,public=keys
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",public)
    attacker=ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(HTTPException) as exc:
        verify(signed_token(attacker))
    assert exc.value.status_code==401


def test_unsigned_token_rejected(monkeypatch,keys):
    _,public=keys
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",public)
    token=jwt.encode({"sub":"did:privy:fake","sid":"s","iss":"privy.io","aud":"cmw7sb7ty013o0gl55aqyzi3y",
                      "iat":int(time.time()),"exp":int(time.time())+3600},key="",algorithm="none")
    with pytest.raises(HTTPException) as exc:
        verify(token)
    assert exc.value.status_code==401
