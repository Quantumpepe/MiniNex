"""Atomic final send claim requires owner consent, current policy and budget."""
from datetime import datetime,timedelta,timezone

import pytest
from fastapi import HTTPException

from backend.shared_evm_order_journal import _claim_authorized_evm_order_locked
from backend.shared_evm_swap_receipts import _pair

DID="did:privy:alice"
KEY="sig_abcdef01234567890123456789"
SHA="a"*64
WALLET="0x"+"ab"*20
WALLET_ID="wallet_12345678"


class Cursor:
    def __init__(self,rows):self.rows=iter(rows);self.calls=[]
    def execute(self,query,args):self.calls.append((query,args))
    def fetchone(self):return next(self.rows)


def identity(asset="AVAX"):
    pair=_pair(asset,"USDC","BUY")
    return {
        "asset":asset,"chain_id":pair["chain_id"],
        "network":pair["network"],"side":"BUY","stable":"USDC",
        "wallet_address":WALLET,"wallet_id":WALLET_ID,
        "request_digest":SHA,
    }


def rows(asset="AVAX"):
    return [
        {"status":"GRANT_VERIFIED","stable":"USDC",
         "chain_id":_pair(asset,"USDC","BUY")["chain_id"],
         "wallet_id":WALLET_ID,"wallet_address":WALLET,
         "policy_id":"policy_123456",
         "policy_sha256":SHA,"remote_policy_sha256":SHA,
         "consent_sha256":SHA,
         "budget_revision":7,"revoked_at":None,
         "expires_at":datetime.now(timezone.utc)+timedelta(hours=1)},
        {"status":"SIGNED_INTENT","wallet_id":WALLET_ID,
         "wallet_address":WALLET,
         "chain_id":_pair(asset,"USDC","BUY")["chain_id"],
         "stable":"USDC","budget_revision":7,
         "message_sha256":SHA,
         "confirmed_at":datetime.now(timezone.utc)},
        {"stable":"USDC","revision":7},
        {"state":"RESERVED","side":"BUY","stable":"USDC",
         "budget_revision":7,"quote_sha256":SHA},
        {"status":"ACTIVE","chain_id":_pair(asset,"USDC","BUY")["chain_id"],
         "stable":"USDC","wallet_address":WALLET,
         "budget_revision":7,
         "started_by_owner_at":datetime.now(timezone.utc)},
        {"privy_did":DID},
        {"pending":0},
        {"pending":0},
        {"pending":0},
        {"pending":0},
        {"state":"SENDING"},
    ]


def claim(cur,asset="AVAX"):
    return _claim_authorized_evm_order_locked(
        cur,did=DID,key=KEY,identity=identity(asset),
        quote_sha256=SHA,budget_revision=7)


def test_single_atomic_owner_wallet_signed_reservation_claim():
    cur=Cursor(rows())
    claim(cur)
    assert len(cur.calls)==12
    assert all("FOR UPDATE" in q for q,args in cur.calls[:4])
    assert "mininex_live_evm_auto_broadcasts" in cur.calls[-2][0]
    assert "state='SENDING'" in cur.calls[-1][0]


@pytest.mark.parametrize("tamper",[
    lambda r:r[0].update(status="REVOKED"),
    lambda r:r[0].update(wallet_address="0x"+"cd"*20),
    lambda r:r[0].update(budget_revision=8),
    lambda r:r[0].update(revoked_at=datetime.now(timezone.utc)),
    lambda r:r[1].update(status="PENDING"),
    lambda r:r[1].update(message_sha256="b"*64),
    lambda r:r[2].update(revision=8),
    lambda r:r[3].update(state="SENDING"),
    lambda r:r[3].update(quote_sha256="b"*64),
])
def test_no_live_send_claim_after_wallet_revoke_or_budget_change(tamper):
    data=rows()
    tamper(data)
    cur=Cursor(data)
    with pytest.raises(HTTPException):
        claim(cur)
    assert not any("INSERT INTO mininex_live_evm_auto_broadcasts" in q
                   for q,args in cur.calls)


@pytest.mark.parametrize("change",[
    lambda r:r[4].update(status="PAUSED"),
    lambda r:r[4].update(wallet_address="0x"+"cd"*20),
    lambda r:r[4].update(budget_revision=8),
    lambda r:r[4].update(started_by_owner_at=None),
    lambda r:r[4].update(chain_id=1),
])
def test_owner_not_started_or_paused_session_blocks_real_send(change):
    data=rows()
    change(data)
    cur=Cursor(data)
    with pytest.raises(HTTPException) as err:
        claim(cur)
    assert err.value.status_code==409
    assert len(cur.calls)==5
    assert not any("INSERT INTO mininex_live_evm_auto_broadcasts" in sql
                   for sql,args in cur.calls)


def test_other_network_coin_cannot_reuse_avalanche_approval():
    cur=Cursor(rows())
    other=identity("POL")
    with pytest.raises(HTTPException):
        _claim_authorized_evm_order_locked(
            cur,did=DID,key=KEY,identity=other,
            quote_sha256=SHA,budget_revision=7)
    assert len(cur.calls)==1


def test_authorized_claim_is_internal_no_api_or_privy_signature():
    import inspect
    from backend import shared_evm_order_journal as mod
    source=inspect.getsource(mod._claim_authorized_evm_order_locked)
    assert "sign_privy" not in source
    assert "post_claimed_privy_evm_rpc" not in source
    from backend.app import app
    assert all("claim-live-evm-order" not in r.path for r in app.routes)
