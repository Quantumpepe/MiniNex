"""Original-owner Privy signer removal must stop future central EVM orders."""
import inspect

import pytest
from fastapi import HTTPException

from backend.shared_evm_wallet_policy_revoke import (
    _verified_original_absence,_confirm_original_revocation_locked,
)
from backend.app import app

DID="did:privy:owner12345"
WID="wallet_12345678"
ADDR="0x"+"a"*40


def user(delegated=False):
    return {"id":DID,"linked_accounts":[{
        "type":"wallet","chain_type":"ethereum",
        "wallet_client_type":"privy","id":WID,
        "address":ADDR,"delegated":delegated,
    }]}


def remote(signers=None):
    return {"id":WID,"chain_type":"ethereum","address":ADDR,
        "entity":{"type":"user","id":DID},
        "archived_at":None,
        "additional_signers":[] if signers is None else signers,
    }


class Cur:
    def __init__(self,rows):
        self.rows=iter(rows)
        self.calls=[]
    def execute(self,sql,params):
        self.calls.append((sql,params))
    def fetchone(self):
        return next(self.rows)


def test_remote_wallet_absence_is_independently_checked():
    result=_verified_original_absence(
        did=DID,user=user(),remote=remote(),
    )
    assert result["wallet_id"]==WID
    assert result["delegated"] is False


@pytest.mark.parametrize("change",[
    lambda u,r:u["linked_accounts"][0].update(delegated=True),
    lambda u,r:r.update(additional_signers=[{
        "signer_id":"other_signer123456","override_policy_ids":[],
    }]),
    lambda u,r:r["entity"].update(id="did:privy:other12345"),
    lambda u,r:r.update(id="wallet_other12345"),
    lambda u,r:r.update(address="0x"+"b"*40),
    lambda u,r:r.update(archived_at="2026-11-22T18:00:00Z"),
])
def test_never_revoke_original_ledger_when_privy_still_has_signer(change):
    original=user();wallet=remote()
    change(original,wallet)
    with pytest.raises(HTTPException) as exc:
        _verified_original_absence(
            did=DID,user=original,remote=wallet,
        )
    assert exc.value.status_code==409


def test_original_signer_revocation_pauses_every_active_coin_session():
    cur=Cur([
        {"wallet_id":WID,"wallet_address":ADDR,
         "status":"GRANT_VERIFIED"},
        {"status":"REVOKED"},
    ])
    result=_confirm_original_revocation_locked(
        cur,did=DID,wallet={"wallet_id":WID,"address":ADDR},
    )
    assert result["status"]=="REVOKED"
    assert result["future_auto_orders_blocked"] is True
    assert result["real_transaction_cancelled"] is False
    assert len(cur.calls)==4
    assert "status='REVOKED'" in cur.calls[1][0]
    assert "mininex_live_evm_wallet_policy_intents" in cur.calls[2][0]
    assert "mininex_live_evm_auto_sessions" in cur.calls[3][0]
    assert "status='PAUSED'" in cur.calls[3][0]


def test_repeated_same_privy_removal_does_not_restore_any_signer():
    cur=Cur([{"wallet_id":WID,"wallet_address":ADDR,"status":"REVOKED"}])
    result=_confirm_original_revocation_locked(
        cur,did=DID,wallet={"wallet_id":WID,"address":ADDR},
    )
    assert result["status"]=="REVOKED"
    assert result["new_signer_granted"] is False
    assert len(cur.calls)==1


def test_revoke_endpoint_only_accepts_owner_bearer_and_remote_sdk_removal():
    route=next(r for r in app.routes if
        r.path=="/api/live/evm/wallet-policy/confirm-revocation")
    assert route.methods=={"POST"}
    code=inspect.getsource(route.endpoint)
    assert "verify_privy_bearer" in code
    assert "confirm_original_wallet_policy_revocation" in code
