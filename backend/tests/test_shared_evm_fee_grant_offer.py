"""Fee owner grant review is a read-only, exact remote Privy policy offer."""
from datetime import datetime,timezone,timedelta
from copy import deepcopy

import pytest
from fastapi import HTTPException

from backend import shared_evm_fee_grant_offer as offer
from backend.app import app

WALLET="0x"+"ab"*20
TREASURY="0x"+"ef"*20
DIGEST="d"*64


def evidence():
    expiry=datetime(2027,1,1,tzinfo=timezone.utc)
    record={
        "asset":"LINK","stable":"USDC","chain_id":1,
        "status":"REMOTE_UNATTACHED","revoked_at":None,
        "owner_signed_at":datetime(2026,11,17,tzinfo=timezone.utc),
        "wallet_id":"wallet_12345678","wallet_address":WALLET,
        "treasury_wallet":TREASURY,"owner_max_fee_atomic":5000000,
        "signer_id":"signer_12345678","policy_id":"policy_12345678",
        "policy_sha256":DIGEST,"remote_policy_sha256":DIGEST,
        "expires_at":expiry,
    }
    return record,{"id":"expected"},{"wallet_id":"wallet_12345678","address":WALLET},{
        "id":"policy_12345678"},{"remote_quorum_public_key_verified":True}


def verified_remote(remote,expected):
    return {"remote_exact_match":True,"policy_id":"policy_12345678",
            "policy_sha256":DIGEST}


def test_strict_fee_grant_offer_exposes_only_exact_scope(monkeypatch):
    monkeypatch.setattr(offer,"verify_exact_remote_evm_policy",verified_remote)
    r,expected,owned,remote,quorum=evidence()
    result=offer.verify_owner_auto_fee_grant_offer(
        record=r,asset="LINK",expected=expected,owned=owned,
        remote=remote,quorum=quorum,now=1790000000)
    assert result["policy_id"]=="policy_12345678"
    assert result["execution_signer_id"]=="signer_12345678"
    assert result["treasury_wallet"]==TREASURY
    assert result["owner_max_fee_atomic"]=="5000000"
    assert result["mode"]=="OWNER_EVM_FEE_SIGNER_GRANT_OFFER_ONLY"
    assert result["privy_grant_attached"] is False
    assert result["automatic_payment_sent"] is False


@pytest.mark.parametrize("change",[
    lambda r,e,o,p,q:r.update(status="GRANT_VERIFIED"),
    lambda r,e,o,p,q:r.update(revoked_at=datetime.now(timezone.utc)),
    lambda r,e,o,p,q:r.update(owner_signed_at=None),
    lambda r,e,o,p,q:r.update(remote_policy_sha256="a"*64),
    lambda r,e,o,p,q:r.update(expires_at=datetime(2026,1,1,tzinfo=timezone.utc)),
    lambda r,e,o,p,q:r.update(signer_id=""),
    lambda r,e,o,p,q:r.update(policy_id=""),
    lambda r,e,o,p,q:o.update(address="0x"+"cd"*20),
    lambda r,e,o,p,q:o.update(wallet_id="another_wallet"),
    lambda r,e,o,p,q:q.update(remote_quorum_public_key_verified=False),
])
def test_unapproved_or_changed_fee_offer_refused(monkeypatch,change):
    monkeypatch.setattr(offer,"verify_exact_remote_evm_policy",verified_remote)
    r,expected,owned,remote,quorum=evidence()
    change(r,expected,owned,remote,quorum)
    with pytest.raises(HTTPException) as caught:
        offer.verify_owner_auto_fee_grant_offer(
            record=r,asset="LINK",expected=expected,owned=owned,
            remote=remote,quorum=quorum,now=1790000000)
    assert caught.value.status_code==409


def test_policy_mismatch_is_never_offered(monkeypatch):
    monkeypatch.setattr(offer,"verify_exact_remote_evm_policy",
        lambda *_:{"remote_exact_match":False,
                  "policy_id":"wrong","policy_sha256":DIGEST})
    r,expected,owned,remote,quorum=evidence()
    with pytest.raises(HTTPException):
        offer.verify_owner_auto_fee_grant_offer(
            record=r,asset="LINK",expected=expected,owned=owned,
            remote=remote,quorum=quorum,now=1790000000)


def test_fee_grant_route_authenticated_and_non_signing():
    routes={r.path:(getattr(r,"methods",set()),r.endpoint)
            for r in app.routes}
    methods,endpoint=routes[
        "/api/live/evm/fee-auto-consent/{asset}/grant-review"]
    assert methods=={"GET"}
    assert "verify_privy_bearer" in endpoint.__code__.co_names
    assert "get_owner_auto_fee_grant_offer" in endpoint.__code__.co_names
    assert "sign_privy" not in endpoint.__code__.co_names
