"""Owner quorum, not trade signer, must sign a full central-policy PATCH."""
import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from backend import shared_evm_wallet_policy_owner_signature as m
from backend.shared_evm_consolidated_wallet_policy import (
    compose_scoped_evm_wallet_policy,
)

WALLET="0x"+"a"*40
NOW=1800000000
OWNER="owner_12345678"
SIGNER="signer_12345678"
URL="https://api.privy.io/v1/policies/policy_12345678"
KEY="a"*64


def candidate():
    return compose_scoped_evm_wallet_policy(
        wallet=WALLET,owner_id=OWNER,executor_id=SIGNER,
        scopes=[dict(
            asset="LINK",stable="USDC",treasury="0x"+"b"*40,
            buy_source_atomic_cap="1000000",
            sell_source_atomic_cap="1000000000000000000",
            native_wrap_atomic_cap=None,
            owner_max_fee_atomic="1000000",
            expires_at=NOW+3600,
        )],now=NOW,
    )


def test_exact_patch_can_be_signed_with_registered_owner_key(monkeypatch):
    private=ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setattr(m,"_read_valid_p256_private_key",lambda:private)
    monkeypatch.setattr(
        m,"_configured_quorum_public_key",lambda:private.public_key(),
    )
    out=m.sign_exact_privy_owner_patch(
        url=URL,candidate=candidate(),
        idempotency_key=KEY,expiry_unix_ms=NOW*1000,
        owner_quorum_verified=True,
    )
    assert out["request_sent"] is False
    assert out["headers"]["privy-request-expiry"]==str(NOW*1000)
    assert out["headers"]["privy-idempotency-key"]==KEY
    assert out["headers"]["privy-app-id"]==m.APP_ID
    assert set(out["body"])=={"name","rules"}
    assert "owner_id" not in out["body"]
    signed_payload=m.owner_policy_patch_signature_payload(
        url=out["url"],body=out["body"],
        idempotency_key=KEY,expiry_unix_ms=NOW*1000,
    )
    parsed=json.loads(signed_payload)
    assert parsed["version"]==1
    assert parsed["method"]=="PATCH"
    assert parsed["headers"]["privy-request-expiry"]==str(NOW*1000)
    private.public_key().verify(
        base64.b64decode(out["headers"]["privy-authorization-signature"]),
        signed_payload,ec.ECDSA(hashes.SHA256()),
    )


def test_missing_policy_owner_signature_or_wrong_private_key_fails(monkeypatch):
    private=ec.generate_private_key(ec.SECP256R1())
    different=ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setattr(m,"_read_valid_p256_private_key",lambda:private)
    monkeypatch.setattr(
        m,"_configured_quorum_public_key",lambda:different.public_key(),
    )
    with pytest.raises(ValueError):
        m.sign_exact_privy_owner_patch(
            url=URL,candidate=candidate(),
            idempotency_key=KEY,expiry_unix_ms=NOW*1000,
            owner_quorum_verified=True,
        )
    with pytest.raises(ValueError):
        m.sign_exact_privy_owner_patch(
            url=URL,candidate=candidate(),
            idempotency_key=KEY,expiry_unix_ms=NOW*1000,
            owner_quorum_verified=False,
        )


@pytest.mark.parametrize("bad",[
    lambda p:p["rules"][0].update(method="*"),
    lambda p:p["rules"][0].update(action="ALLOW_ALL"),
    lambda p:p["rules"][0].update(conditions=[]),
    lambda p:p["rules"].append(dict(p["rules"][0])),
    lambda p:p.update(name="too-long-"*12),
    lambda p:p.update(chain_type="solana"),
])
def test_unrestricted_or_renamed_policy_request_rejected(bad):
    p=candidate()
    bad(p)
    with pytest.raises(ValueError):
        m.exact_owner_policy_patch_body(p)


@pytest.mark.parametrize("patch",[
    {"url":"https://evil.example/v1/policies/policy_12345678"},
    {"url":URL+"/../wallets"},
    {"idempotency_key":"wrong"},
    {"expiry_unix_ms":None},
    {"expiry_unix_ms":0},
    {"body":{"rules":[]}},
])
def test_noncanonical_or_unbound_privy_request_is_rejected(patch):
    data=dict(
        url=URL,body=m.exact_owner_policy_patch_body(candidate()),
        idempotency_key=KEY,expiry_unix_ms=NOW*1000,
    )
    data.update(patch)
    with pytest.raises(ValueError):
        m.owner_policy_patch_signature_payload(**data)
