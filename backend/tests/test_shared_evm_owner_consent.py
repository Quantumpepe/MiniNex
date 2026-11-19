"""Five EVM assets share true one-time owner EIP191 intent without live grant."""
import hashlib

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import HTTPException

from backend import shared_evm_owner_consent as m
from backend.storage import SCHEMA

DID="did:privy:owner12345"
SID=hashlib.sha256(b"session_12345678").hexdigest()
NOW=1800000000
WALLET="0x"+"ab"*20
OWNED={"address":WALLET,"wallet_id":"wallet_12345678","delegated":False}
OWNER="owner_123456"
EXEC="signer_123456"
SETTLEMENT={"BNB":"USDT","POL":"USDC","AVAX":"USDC",
            "LINK":"USDC","UNI":"USDT","ARB":"USDC"}
CHAIN={"BNB":56,"POL":137,"AVAX":43114,"LINK":1,"UNI":1,"ARB":42161}


class Cursor:
    def __init__(self,rows):
        self.rows=iter(rows);self.calls=[]
    def execute(self,sql,args):
        self.calls.append((sql,args))
    def fetchone(self):
        return next(self.rows)


def settings(asset):
    return {"budget_usd":9500,"stable":SETTLEMENT[asset],"revision":3}


def begin_rows(asset):
    return [settings(asset),None,None,{"status":"PENDING"}]


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI","ARB"])
def test_coin_consent_intent_binds_user_wallet_network_budget_and_nonce(asset):
    cur=Cursor(begin_rows(asset))
    report=m._begin_locked(
        cur,did=DID,asset=asset,session_sha256=SID,
        owned=OWNED,owner=OWNER,executor=EXEC,
        now=NOW,nonce="f"*64)
    challenge=report["challenge"]
    assert f"Asset: {asset}" in challenge
    assert f"Chain ID: {CHAIN[asset]}" in challenge
    assert f"Settlement: {SETTLEMENT[asset]}" in challenge
    assert "Coin budget USD: 9500" in challenge
    assert "Budget revision: 3" in challenge
    assert "Nonce: "+ "f"*64 in challenge
    assert SID in challenge
    assert WALLET in challenge
    assert "does NOT delegate" in challenge
    assert report["privy_signer_granted"] is False
    assert len(cur.calls)==4
    assert "FOR UPDATE" in cur.calls[0][0]
    assert "WHERE mininex_live_evm_consent_challenges.status='PENDING'" in cur.calls[-1][0]


def test_no_hidden_reenroll_on_signed_or_revoked_coin_intent():
    for state in ("SIGNED_INTENT","REVOKED"):
        cur=Cursor([settings("LINK"),None,{"status":state}])
        with pytest.raises(HTTPException) as e:
            m._begin_locked(cur,did=DID,asset="LINK",session_sha256=SID,
                owned=OWNED,owner=OWNER,executor=EXEC,
                now=NOW,nonce="a"*64)
        assert e.value.status_code==409
        assert len(cur.calls)==3


def test_existing_privy_grant_cannot_be_overwritten_by_new_consent():
    cur=Cursor([settings("UNI"),{"status":"GRANT_VERIFIED"}])
    with pytest.raises(HTTPException):
        m._begin_locked(cur,did=DID,asset="UNI",session_sha256=SID,
            owned=OWNED,owner=OWNER,executor=EXEC,
            now=NOW,nonce="b"*64)


def consent_record(asset,message,*,rev=3,session=SID):
    return {
        "status":"PENDING","session_sha256":session,
        "wallet_id":OWNED["wallet_id"],"wallet_address":WALLET,
        "chain_id":CHAIN[asset],"stable":SETTLEMENT[asset],
        "budget_revision":rev,"budget_usd":9500,
        "message_text":message,
        "message_sha256":hashlib.sha256(message.encode()).hexdigest(),
        "expires_at_unix":NOW+300,
    }


def real_owner_and_signature():
    account=Account.from_key("0x"+"11"*32)
    return account,account.address.lower()


def test_signed_owner_confirm_requires_exact_privy_eoa_and_one_time_use():
    account,wallet=real_owner_and_signature()
    owned={**OWNED,"address":wallet}
    challenge=m._canonical_message(
        did=DID,asset="BNB",stable="USDT",chain_id=56,
        wallet=wallet,wallet_id=OWNED["wallet_id"],budget_usd=9500,
        budget_revision=3,session_sha256=SID,nonce="a"*64,
        issued=NOW,expires=NOW+300,owner=OWNER,executor=EXEC)
    sig=Account.sign_message(
        encode_defunct(text=challenge),account.key).signature.hex()
    if not sig.startswith("0x"):sig="0x"+sig
    row=consent_record("BNB",challenge)
    row["wallet_address"]=wallet
    cur=Cursor([settings("BNB"),row,{"status":"SIGNED_INTENT"}])
    report=m._confirm_locked(
        cur,did=DID,asset="BNB",session_sha256=SID,
        owned=owned,signature=sig,now=NOW+5)
    assert report["owner_signature_verified"] is True
    assert report["additional_privy_grant_still_required"] is True
    assert report["live_trading_enabled"] is False
    assert "status='PENDING'" in cur.calls[-1][0]
    cur=Cursor([settings("BNB"),{**row,"status":"SIGNED_INTENT"}])
    with pytest.raises(HTTPException) as e:
        m._confirm_locked(cur,did=DID,asset="BNB",session_sha256=SID,
                          owned=owned,signature=sig,now=NOW+6)
    assert e.value.status_code==409


@pytest.mark.parametrize("mutate",[
    lambda row:row.update(wallet_address="0x"+"cd"*20),
    lambda row:row.update(chain_id=1),
    lambda row:row.update(budget_revision=2),
    lambda row:row.update(session_sha256="0"*64),
    lambda row:row.update(expires_at_unix=NOW-1),
    lambda row:row.update(message_sha256="0"*64),
])
def test_altered_session_budget_chain_or_expired_challenge_rejected(mutate):
    message=m._canonical_message(did=DID,asset="POL",stable="USDC",
        chain_id=137,wallet=WALLET,wallet_id=OWNED["wallet_id"],
        budget_usd=9500,budget_revision=3,session_sha256=SID,
        nonce="b"*64,issued=NOW,expires=NOW+300,
        owner=OWNER,executor=EXEC)
    row=consent_record("POL",message)
    mutate(row)
    cur=Cursor([settings("POL"),row])
    with pytest.raises(HTTPException) as e:
        m._confirm_locked(cur,did=DID,asset="POL",
            session_sha256=SID,owned=OWNED,
            signature="0x"+"00"*65,now=NOW)
    assert e.value.status_code==409
    assert len(cur.calls)==2


def test_owner_intent_cannot_be_misrepresented_as_privy_grant():
    import inspect
    source=inspect.getsource(m)
    assert "post_claimed_privy_evm_rpc" not in source
    assert "sign_privy_evm_swap_request" not in source
    assert "privy_remote_signer_removed" in source
    assert "mininex_live_evm_consent_challenges" in "\n".join(SCHEMA)
    from backend.app import app
    for suffix in ("challenge","confirm","revoke"):
        route=next(x for x in app.routes if
            x.path==f"/api/live/evm/consent/{{asset}}/{suffix}")
        assert route.methods=={"POST"}
        assert "verify_privy_bearer" in inspect.getsource(route.endpoint)
