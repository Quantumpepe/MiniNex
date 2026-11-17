"""Exact automatic fee Privy policy publication is once-only and not a grant."""
import hashlib
import json
from datetime import datetime,timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException

from backend import shared_evm_fee_policy_publish as pub
from backend.shared_evm_fee_privy_policy_draft import build_owner_evm_fee_policy_draft

DID="did:privy:owner_12345678"
WALLET="0x"+"ab"*20
TREASURY="0x"+"cd"*20
OWNER="owner_12345678"
SIGNER="signer_12345678"
NOW=1800000


class Cur:
    def __init__(self,items):
        self.rows=iter(items)
        self.sql=[]
    def execute(self,q,a):
        self.sql.append((q,a))
    def fetchone(self):
        return next(self.rows)


def record(status="CONSENT_SIGNED"):
    draft=build_owner_evm_fee_policy_draft(
        asset="LINK",stable="USDC",wallet=WALLET,treasury=TREASURY,
        owner_id=OWNER,executor_id=SIGNER,
        owner_max_fee_atomic="3500000",
        expires_at=NOW+86400*365,now=NOW)
    digest=hashlib.sha256(json.dumps(
        draft,sort_keys=True,separators=(",",":")
    ).encode("utf-8")).hexdigest()
    return dict(
        status=status,asset="LINK",stable="USDC",chain_id=1,
        wallet_id="wallet_12345678",wallet_address=WALLET,
        treasury_wallet=TREASURY,owner_id=OWNER,signer_id=SIGNER,
        owner_max_fee_atomic=Decimal("3500000"),
        expires_at=datetime.fromtimestamp(NOW+86400*365,timezone.utc),
        owner_signed_at=datetime.fromtimestamp(NOW-10,timezone.utc),
        revoked_at=None,policy_attempted_at=None,policy_id=None,
        policy_json=draft,policy_sha256=digest,
    )


def setup(monkeypatch):
    monkeypatch.setattr(pub,"_require_policy_roles",lambda:(OWNER,SIGNER))
    monkeypatch.setattr(pub,"owner_fee_treasury",lambda **kw:TREASURY)


def test_fee_policy_exact_once_only_after_separately_signed_intent(monkeypatch):
    setup(monkeypatch)
    original=record()
    cur=Cur([original,{"status":"REMOTE_UNCERTAIN"}])
    claim=pub._claim_fee_policy_locked(
        cur,did=DID,asset="LINK",
        owned={"wallet_id":original["wallet_id"],"address":WALLET},
        now=NOW)
    assert claim["candidate"]==original["policy_json"]
    assert len(claim["idempotency_key"])==64
    assert "SET status='REMOTE_UNCERTAIN'" in cur.sql[-1][0]
    assert "policy_attempted_at IS NULL" in cur.sql[-1][0]


@pytest.mark.parametrize("change",[
    lambda r:r.update(owner_signed_at=None),
    lambda r:r.update(status="PENDING_INTENT"),
    lambda r:r.update(status="REMOTE_UNCERTAIN"),
    lambda r:r.update(policy_json={"rules":[{"action":"ALLOW"}]}),
    lambda r:r.update(treasury_wallet="0x"+"ef"*20),
    lambda r:r.update(policy_id="already_published_123"),
    lambda r:r.update(policy_attempted_at=datetime.fromtimestamp(NOW,timezone.utc)),
    lambda r:r.update(expires_at=datetime.fromtimestamp(NOW-1,timezone.utc)),
])
def test_fee_policy_cannot_be_published_unapproved_or_twice(monkeypatch,change):
    setup(monkeypatch)
    original=record()
    change(original)
    cur=Cur([original])
    with pytest.raises((HTTPException,ValueError)):
        pub._claim_fee_policy_locked(
            cur,did=DID,asset="LINK",
            owned={"wallet_id":"wallet_12345678","address":WALLET},
            now=NOW)
    assert not any("UPDATE mininex_evm_fee_auto_permissions" in q
                   for q,a in cur.sql)


def test_policy_remote_get_proof_does_not_attach_signer(monkeypatch):
    setup(monkeypatch)
    original=record(status="REMOTE_UNCERTAIN")
    original["idempotency_key"]="f"*64
    claim={"candidate":original["policy_json"],
           "policy_sha256":original["policy_sha256"],
           "idempotency_key":"f"*64}
    remote={"remote_exact_match":True,"policy_sha256":original["policy_sha256"],
            "policy_id":"policy_12345678"}
    cur=Cur([original,{"status":"REMOTE_UNATTACHED"}])
    result=pub._finish_fee_policy_locked(
        cur,did=DID,asset="LINK",claim=claim,remote=remote)
    assert result["state"]=="REMOTE_UNATTACHED"
    assert result["privy_wallet_owner_add_signer_still_required"] is True
    assert result["automatic_fee_transfer_enabled"] is False
    assert "SET status='REMOTE_UNATTACHED'" in cur.sql[-1][0]


def test_owner_fee_api_does_not_reuse_trading_policy():
    from backend.app import app
    routes={r.path for r in app.routes}
    assert "/api/live/evm/fee-auto-consent/{asset}/challenge" in routes
    assert "/api/live/evm/fee-auto-consent/{asset}/confirm" in routes
    assert "/api/live/evm/fee-auto-consent/{asset}/publish-policy" in routes
    assert "/api/live/evm/fee-auto-consent/{asset}/grant-confirm" in routes
