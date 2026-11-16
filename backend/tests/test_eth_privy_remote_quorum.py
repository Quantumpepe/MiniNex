"""Privy read-only GET of the registered P-256 1-of-1 backend quorum."""
from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from backend.eth_privy_remote_quorum import (
    check_privy_backend_quorum, inspect_remote_privy_backend_quorum,
)

QUORUM = "quorum_v2_test_123456"


def configured(monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    monkeypatch.setenv(
        "MININEX_PRIVY_ETH_AUTH_P256_PKCS8",
        base64.b64encode(private).decode(),
    )
    monkeypatch.setenv(
        "MININEX_PRIVY_ETH_SIGNER_PUBLIC_KEY_SPKI",
        base64.b64encode(public).decode(),
    )
    monkeypatch.setenv("MININEX_PRIVY_ETH_SIGNER_ID", QUORUM)
    monkeypatch.setenv("MININEX_PRIVY_APP_SECRET", "mock-app-secret")
    return base64.b64encode(public).decode()


def payload(public):
    return {
        "id": QUORUM,
        "authorization_threshold": 1,
        "authorization_keys": [{"public_key": public}],
    }


@pytest.mark.parametrize("mutation", [
    {"id": "different_quorum"},
    {"authorization_threshold": 2},
    {"authorization_threshold": "1"},
    {"authorization_keys": []},
    {"authorization_keys": [{"public_key": "invalid"}]},
    {"authorization_keys": [{"public_key": "invalid"}, {"public_key": "invalid"}]},
])
def test_reject_modified_privy_quorum(monkeypatch, mutation):
    public = configured(monkeypatch)
    candidate = {**payload(public), **mutation}
    assert inspect_remote_privy_backend_quorum(candidate, expected_id=QUORUM) is False


def test_wrong_remote_quorum_public_key_fails_closed(monkeypatch):
    configured(monkeypatch)
    other = ec.generate_private_key(ec.SECP256R1())
    spki = other.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    assert not inspect_remote_privy_backend_quorum(
        payload(base64.b64encode(spki).decode()), expected_id=QUORUM,
    )


def test_read_only_remote_quorum_get_exact_match_without_wallet_delegation(monkeypatch):
    public = configured(monkeypatch)
    sent = []
    def handler(request):
        sent.append(request)
        assert request.method == "GET"
        assert request.url.path == "/v1/key_quorums/" + QUORUM
        assert request.headers["privy-app-id"]
        return httpx.Response(200, json=payload(public))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await check_privy_backend_quorum(client)
    result = asyncio.run(run())
    assert len(sent) == 1
    assert result["remote_quorum_get_succeeded"] is True
    assert result["remote_quorum_public_key_verified"] is True
    assert result["owner_wallet_delegation_verified"] is False
    assert result["live_execution_enabled"] is False
    assert public not in json.dumps(result)
    assert "mock-app-secret" not in json.dumps(result)


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_privy_errors_never_are_positive_or_raise_secret(monkeypatch, status):
    public = configured(monkeypatch)
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(status, json={"detail": public}),
            ),
        ) as client:
            return await check_privy_backend_quorum(client)
    result = asyncio.run(run())
    assert result["remote_quorum_public_key_verified"] is False
    assert public not in str(result)


def test_absent_private_key_never_queries_privy(monkeypatch):
    configured(monkeypatch)
    monkeypatch.delenv("MININEX_PRIVY_ETH_AUTH_P256_PKCS8")
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: pytest.fail("must not GET")),
        ) as client:
            return await check_privy_backend_quorum(client)
    assert asyncio.run(run())["remote_quorum_public_key_verified"] is False


@pytest.mark.parametrize("extraneous_members", [
    {"user_ids": ["did:privy:unapproved-user"]},
    {"key_quorum_ids": ["other_quorum_123"]},
    {"user_ids": ["did:privy:unapproved-user"], "key_quorum_ids": []},
    {"user_ids": [], "key_quorum_ids": ["other_quorum_123"]},
    {"user_ids": "not-a-list"},
    {"key_quorum_ids": "not-a-list"},
])
def test_1_of_1_rejects_other_member_classes(monkeypatch, extraneous_members):
    public = configured(monkeypatch)
    candidate = {**payload(public), **extraneous_members}
    assert not inspect_remote_privy_backend_quorum(candidate, expected_id=QUORUM)


def test_1_of_1_accepts_explicit_empty_member_classes(monkeypatch):
    public = configured(monkeypatch)
    candidate = {**payload(public), "user_ids": [], "key_quorum_ids": []}
    assert inspect_remote_privy_backend_quorum(candidate, expected_id=QUORUM)


def test_1_of_1_accepts_exact_numeric_float_threshold(monkeypatch):
    public = configured(monkeypatch)
    candidate = {**payload(public), "authorization_threshold": 1.0}
    assert inspect_remote_privy_backend_quorum(candidate, expected_id=QUORUM)


@pytest.mark.parametrize("value", [True, False, "1", "1.0", 1.00001, 0.0, 2.0])
def test_1_of_1_does_not_accept_other_threshold_types_or_values(monkeypatch, value):
    public = configured(monkeypatch)
    candidate = {**payload(public), "authorization_threshold": value}
    assert not inspect_remote_privy_backend_quorum(candidate, expected_id=QUORUM)
