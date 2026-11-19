"""Only the owner receives exact remote EVM trade signer terms."""
from datetime import datetime,timezone

import pytest
from fastapi import HTTPException

from backend import shared_evm_trade_grant_offer as offer
from backend.app import app

ADDRESS="0x"+"ab"*20
SHA="e"*64

def verified():
    row={
        "asset":"AVAX","chain_id":43114,"stable":"USDC",
        "wallet_address":ADDRESS,"wallet_id":"wallet_12345678",
        "signer_id":"signer_12345678","policy_id":"policy_12345678",
        "policy_sha256":SHA,"remote_policy_sha256":SHA,
        "status":"CONSENT_VERIFIED","revoked_at":None,
        "consent_verified_at":datetime(2026,11,17,tzinfo=timezone.utc),
        "budget_revision":3,
    }
    return row,{"wallet_id":"wallet_12345678","address":ADDRESS},{
        "version":"1.0"},{"id":"policy_12345678"},{
            "remote_quorum_public_key_verified":True,
        }


def test_only_exact_confirmed_trading_policy_can_be_offered(monkeypatch):
    monkeypatch.setattr(offer,"verify_exact_remote_evm_policy",
        lambda policy,expected:{
            "remote_exact_match":True,"policy_id":"policy_12345678",
            "policy_sha256":SHA,
        })
    row,owned,expected,remote,quorum=verified()
    result=offer.validate_owner_trade_grant_offer(
        asset="AVAX",record=row,owned=owned,
        expected=expected,remote_policy=remote,quorum=quorum)
    assert result["policy_id"]=="policy_12345678"
    assert result["execution_signer_id"]=="signer_12345678"
    assert result["wallet_address"]==ADDRESS
    assert result["budget_revision"]==3
    assert result["privy_signer_attached"] is False
    assert result["trade_sent"] is False


@pytest.mark.parametrize("mutate",[
    lambda r,o,e,p,q:r.update(status="DRAFT"),
    lambda r,o,e,p,q:r.update(consent_verified_at=None),
    lambda r,o,e,p,q:r.update(revoked_at=datetime.now(timezone.utc)),
    lambda r,o,e,p,q:r.update(remote_policy_sha256="f"*64),
    lambda r,o,e,p,q:r.update(signer_id=""),
    lambda r,o,e,p,q:r.update(policy_id=""),
    lambda r,o,e,p,q:r.update(wallet_id="wallet_different"),
    lambda r,o,e,p,q:o.update(address="0x"+"cd"*20),
    lambda r,o,e,p,q:q.update(remote_quorum_public_key_verified=False),
])
def test_trading_grant_offer_must_fail_on_unverified_scope(monkeypatch,mutate):
    monkeypatch.setattr(offer,"verify_exact_remote_evm_policy",
        lambda policy,expected:{
            "remote_exact_match":True,"policy_id":"policy_12345678",
            "policy_sha256":SHA,
        })
    row,owned,expected,remote,quorum=verified()
    mutate(row,owned,expected,remote,quorum)
    with pytest.raises(HTTPException) as e:
        offer.validate_owner_trade_grant_offer(
            asset="AVAX",record=row,owned=owned,
            expected=expected,remote_policy=remote,quorum=quorum)
    assert e.value.status_code==409


def test_only_authenticated_get_can_get_owner_trading_signer():
    route=next(r for r in app.routes
        if r.path=="/api/live/evm/policy/{asset}/grant-review")
    assert route.methods=={"GET"}
    assert "verify_privy_bearer" in route.endpoint.__code__.co_names
    assert "get_owner_trade_grant_offer" in route.endpoint.__code__.co_names
