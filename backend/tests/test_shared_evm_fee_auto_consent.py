"""Owner must sign exact net-profit Treasury intent before any Privy fee grant."""
import hashlib
import json
from datetime import datetime,timezone
from decimal import Decimal

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import HTTPException

from backend import shared_evm_fee_auto_consent as fee

DID="did:privy:alice_12345678"
TREASURY="0x"+"bc"*20
OWNER="policy_owner_12345"
EXECUTOR="exec_signer_12345"
SESSION="a"*64
NOW=1800000


class Cur:
    def __init__(self,rows):
        self.rows=iter(rows)
        self.sql=[]
    def execute(self,q,args):
        self.sql.append((q,args))
    def fetchone(self):
        return next(self.rows)


def begin(monkeypatch):
    user=Account.create()
    owned={"address":user.address.lower(),"wallet_id":"wallet_12345678"}
    monkeypatch.setattr(fee,"_locked_coin_settings",
                        lambda cur,**kw:{"stable":"USDC","revision":1})
    pending=Cur([{"status":"PENDING_INTENT"}])
    result=fee._begin_fee_intent_locked(
        pending,did=DID,session_sha256=SESSION,asset="LINK",
        owned=owned,treasury=TREASURY,owner_id=OWNER,signer_id=EXECUTOR,
        max_atomic="3500000",expires_at=NOW+86400*365,
        now=NOW,nonce="n"*64)
    values=pending.sql[0][1]
    policy=json.loads(values[-3])
    record={
        "asset":"LINK","stable":"USDC","chain_id":1,
        "wallet_id":owned["wallet_id"],"wallet_address":owned["address"],
        "treasury_wallet":TREASURY,"owner_id":OWNER,"signer_id":EXECUTOR,
        "owner_max_fee_atomic":Decimal("3500000"),
        "expires_at":datetime.fromtimestamp(NOW+86400*365,timezone.utc),
        "challenge_text":result["message_to_sign"],
        "challenge_sha256":hashlib.sha256(
            result["message_to_sign"].encode()).hexdigest(),
        "challenge_expires_at":datetime.fromtimestamp(NOW+300,timezone.utc),
        "policy_json":policy,
        "policy_sha256":result["policy_sha256"],
        "status":"PENDING_INTENT",
    }
    return user,owned,record,result


def test_only_exact_owner_signed_fee_intent_becomes_consent(monkeypatch):
    user,owned,record,original=begin(monkeypatch)
    monkeypatch.setattr(fee,"owner_fee_treasury",lambda **kw:TREASURY)
    sig=Account.sign_message(
        encode_defunct(text=original["message_to_sign"]),
        private_key=user.key).signature.hex()
    cur=Cur([record,{"status":"CONSENT_SIGNED"}])
    outcome=fee._confirm_fee_intent_locked(
        cur,did=DID,asset="LINK",session_sha256=SESSION,
        owned=owned,signature="0x"+sig,now=NOW+30)
    assert outcome["owner_fee_intent_verified"] is True
    assert outcome["privy_owner_grant_still_required"] is True
    assert outcome["automatic_fee_transfer_enabled"] is False
    assert "UPDATE mininex_evm_fee_auto_permissions" in cur.sql[-1][0]


@pytest.mark.parametrize("change",[
    lambda r:r.update(status="GRANT_VERIFIED"),
    lambda r:r.update(treasury_wallet="0x"+"aa"*20),
    lambda r:r.update(policy_sha256="f"*64),
    lambda r:r.update(wallet_id="wallet_replaced"),
    lambda r:r.update(stable="USDT"),
    lambda r:r.update(challenge_sha256="f"*64),
    lambda r:r.update(challenge_expires_at=datetime.fromtimestamp(NOW-1,timezone.utc)),
])
def test_fee_grant_enrollment_cannot_skip_exact_current_owner_intent(
    monkeypatch,change,
):
    user,owned,record,original=begin(monkeypatch)
    monkeypatch.setattr(fee,"owner_fee_treasury",lambda **kw:TREASURY)
    sig=Account.sign_message(
        encode_defunct(text=original["message_to_sign"]),
        private_key=user.key).signature.hex()
    change(record)
    cur=Cur([record])
    with pytest.raises((HTTPException,ValueError)):
        fee._confirm_fee_intent_locked(
            cur,did=DID,asset="LINK",session_sha256=SESSION,
            owned=owned,signature="0x"+sig,now=NOW+10)
    assert not any("UPDATE mininex_evm_fee_auto_permissions" in q for q,args in cur.sql)


def test_unsupported_wallet_signature_does_not_delegate_privy(monkeypatch):
    user,owned,record,result=begin(monkeypatch)
    monkeypatch.setattr(fee,"owner_fee_treasury",lambda **kw:TREASURY)
    wrong=Account.create()
    sig=Account.sign_message(
        encode_defunct(text=result["message_to_sign"]),private_key=wrong.key
    ).signature.hex()
    cur=Cur([record])
    with pytest.raises(HTTPException) as exc:
        fee._confirm_fee_intent_locked(
            cur,did=DID,asset="LINK",session_sha256=SESSION,
            owned=owned,signature="0x"+sig,now=NOW+10)
    assert exc.value.status_code==403


def test_fee_automatic_policy_is_separate_from_trading_policy():
    import inspect
    src=inspect.getsource(fee)
    assert "publish_user_evm_privy_policy" not in src
    assert "sign_privy_rpc_payload" not in src
    assert "post_claimed_privy_evm_rpc" not in src
