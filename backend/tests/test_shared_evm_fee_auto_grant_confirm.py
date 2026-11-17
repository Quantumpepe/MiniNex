"""Only exact user-authenticated Privy fee additional-signer grant counts."""
from datetime import datetime,timezone

import pytest
from fastapi import HTTPException

from backend import shared_evm_fee_auto_grant_confirm as grant

DID="did:privy:owner_12345678"
WALLET="0x"+"ab"*20
TREASURY="0x"+"cd"*20
EXPECTED={"owner_id":"owner_12345678","rules":[{"action":"ALLOW"}]}


def row():
    return {
        "asset":"LINK","stable":"USDC","wallet_id":"wallet_12345678",
        "wallet_address":WALLET,"treasury_wallet":TREASURY,
        "signer_id":"signer_12345678","policy_id":"policy_12345678",
        "policy_sha256":"a"*64,"remote_policy_sha256":"a"*64,
        "status":"REMOTE_UNATTACHED",
        "revoked_at":None,
        "owner_signed_at":datetime.now(timezone.utc),
    }


def setup(monkeypatch):
    monkeypatch.setattr(grant,"_owned_eth_wallet",
                        lambda user,did:{
                            "delegated":True,"wallet_id":"wallet_12345678",
                            "address":WALLET})
    monkeypatch.setattr(grant,"verify_exact_remote_evm_policy",
                        lambda policy,expected:{
                            "remote_exact_match":True,
                            "policy_id":"policy_12345678",
                            "policy_sha256":"a"*64})
    monkeypatch.setattr(grant,"_scoped_signer",lambda *args,**kw:True)


def prove(monkeypatch,record=None,metadata=None,quorum=None):
    setup(monkeypatch)
    return grant._verify_owner_fee_grant(
        did=DID,record=record or row(),expected=EXPECTED,user={},
        wallet_metadata=metadata or {"id":"wallet_12345678"},
        remote_policy={},
        quorum=quorum or {"remote_quorum_public_key_verified":True})


def test_real_owner_grant_exact_wallet_signer_and_quorum_required(monkeypatch):
    result=prove(monkeypatch)
    assert result["verified"] is True
    assert result["policy_id"]=="policy_12345678"
    assert result["treasury_wallet"]==TREASURY


@pytest.mark.parametrize("mutate",[
    lambda r:r.update(status="CONSENT_SIGNED"),
    lambda r:r.update(policy_sha256="b"*64),
    lambda r:r.update(policy_id="another_12345678"),
    lambda r:r.update(wallet_id="another_wallet"),
    lambda r:r.update(remote_policy_sha256="f"*64),
])
def test_other_wallet_trading_policy_or_unsigned_permission_is_insufficient(
    monkeypatch,mutate,
):
    r=row();mutate(r)
    with pytest.raises(HTTPException):
        prove(monkeypatch,record=r)


def test_missing_remote_backend_signer_quorum_denies_fee_grant(monkeypatch):
    setup(monkeypatch)
    with pytest.raises(HTTPException):
        grant._verify_owner_fee_grant(
            did=DID,record=row(),expected=EXPECTED,user={},
            wallet_metadata={"id":"wallet_12345678"},
            remote_policy={},
            quorum={"remote_quorum_public_key_verified":False})


def test_fee_auto_grant_does_not_attach_signer_or_send_funds():
    import inspect
    src=inspect.getsource(grant)
    assert "addSigners" not in src or "user must" in src
    assert "post_claimed_privy_evm_rpc" not in src
    assert "sign_privy_rpc_payload" not in src
