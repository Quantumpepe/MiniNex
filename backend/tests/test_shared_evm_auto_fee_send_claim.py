"""Automatic 5% wallet fee sender: exact privileged original debt once."""
from datetime import datetime,timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException

from backend import shared_evm_auto_fee_send_claim as claim
from backend.shared_evm_fee_transfer_proof import owner_fee_transfer_draft
from backend.shared_evm_swap_receipts import _pair

DID="did:privy:owner_12345678"
KEY="sig_0123456789abc"
WALLET="0x"+"ab"*20
TREASURY="0x"+"cd"*20
POLICY="policy_12345678"
SHA="a"*64
NOW=1800000


class Cur:
    def __init__(self,rows):
        self.rows=iter(rows);self.sql=[]
    def execute(self,q,args):
        self.sql.append((q,args))
    def fetchone(self):
        return next(self.rows)


def draft(asset):
    stable="USDT" if asset=="BNB" else "USDC"
    return owner_fee_transfer_draft(
        asset=asset,stable=stable,wallet=WALLET,treasury=TREASURY,
        fee_due_usd="5",stable_usd="1")


def meta(asset):
    stable="USDT" if asset=="BNB" else "USDC"
    chain=_pair(asset,stable,"BUY")["chain_id"]
    g=dict(
        status="GRANT_VERIFIED",
        stable=stable,chain_id=chain,wallet_id="wallet_12345678",
        wallet_address=WALLET,treasury_wallet=TREASURY,
        owner_max_fee_atomic=Decimal("10000000"),
        expires_at=datetime.fromtimestamp(NOW+86400,timezone.utc),
        policy_id=POLICY,policy_sha256=SHA,remote_policy_sha256=SHA,
        revoked_at=None,owner_signed_at=datetime.now(timezone.utc),
        grant_verified_at=datetime.now(timezone.utc),
    )
    remote=dict(
        asset=asset,chain_id=chain,stable=stable,
        wallet_id="wallet_12345678",owner_wallet=WALLET,
        treasury_wallet=TREASURY,
        policy_id=POLICY,policy_sha256=SHA,
        owner_fee_grant_remotely_verified=True,
        signer_quorum_remotely_verified=True,
    )
    return g,remote


def cur_for(asset,grant=None,trade_pending=0,existing_fee=None):
    g,_=meta(asset)
    if grant is not None:g.update(grant)
    vals=[
        {"privy_did":DID},g,{"pending":trade_pending},
        {"pending":0},{"pending":0},{"pending":0},existing_fee,
    ]
    return Cur(vals)


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI"])
def test_exact_stablecoin_fee_wallet_rpc_bound_to_policy_and_owner(asset):
    original=draft(asset)
    result=claim.immutable_fee_rpc_identity(
        asset=asset,key=KEY,wallet_id="wallet_12345678",
        draft=original,policy_sha256=SHA)
    assert result["stable"]==original["token_symbol"]
    assert result["amount_atomic"]==original["amount_atomic"]
    assert result["body"]["params"]["transaction"]["to"]==original["token_address"]
    assert result["body"]["params"]["transaction"]["data"]==original[
        "unsigned_transaction"]["data"]
    assert result["body"]["caip2"]==f"eip155:{original['chain_id']}"
    assert result["request_digest"]!=result["idempotency_key"]
    assert result["owner_wallet"]==WALLET


@pytest.mark.parametrize("tamper",[
    lambda f:f["unsigned_transaction"].update(to=TREASURY),
    lambda f:f["unsigned_transaction"].update(value="0x1"),
    lambda f:f["unsigned_transaction"].update(data="0x1234"),
    lambda f:f.update(owner_wallet=TREASURY),
    lambda f:f.update(token_address=TREASURY),
])
def test_fee_rpc_never_allows_alternate_token_recipient_or_amount(tamper):
    f=draft("LINK");tamper(f)
    with pytest.raises((ValueError,KeyError)):
        claim.immutable_fee_rpc_identity(
            asset="LINK",key=KEY,wallet_id="wallet_12345678",
            draft=f,policy_sha256=SHA)


@pytest.mark.parametrize("asset",["BNB","LINK"])
def test_atomic_owner_fee_claim_uses_original_debt_and_permission(
    monkeypatch,asset,
):
    def prepare(cur,**kw):
        assert kw["did"]==DID and kw["asset"]==asset
        return draft(asset)
    monkeypatch.setattr(claim,"_prepare_locked",prepare)
    g,remote=meta(asset)
    cur=cur_for(asset)
    result=claim._claim_single_auto_fee_locked(
        cur,did=DID,asset=asset,key=KEY,remote=remote,
        stable_usd=Decimal("1"),treasury=TREASURY,now=NOW)
    assert result["execution_id"]==KEY
    assert any("FOR UPDATE" in q for q,_ in cur.sql)
    assert any("mininex_evm_auto_fee_sends" in q and "INSERT INTO" in q
               for q,_ in cur.sql)


@pytest.mark.parametrize("change",[
    {"status":"REMOTE_UNATTACHED"},
    {"revoked_at":datetime.now(timezone.utc)},
    {"policy_sha256":"f"*64},
    {"owner_max_fee_atomic":Decimal("100")},
    {"wallet_id":"other_wallet"},
    {"expires_at":datetime.fromtimestamp(NOW-1,timezone.utc)},
])
def test_fee_auto_claim_requires_actual_privy_grant_and_user_amount(
    monkeypatch,change,
):
    monkeypatch.setattr(claim,"_prepare_locked",lambda cur,**kw:draft("LINK"))
    _,remote=meta("LINK")
    cur=cur_for("LINK",grant=change)
    with pytest.raises((ValueError,HTTPException)):
        claim._claim_single_auto_fee_locked(
            cur,did=DID,asset="LINK",key=KEY,remote=remote,
            stable_usd=Decimal("1"),treasury=TREASURY,now=NOW)
    assert not any("INSERT INTO mininex_evm_auto_fee_sends" in q
                   for q,_ in cur.sql)


def test_inflight_swap_or_second_fee_is_not_allowed_to_race_wallet_nonce():
    _,remote=meta("LINK")
    cur=cur_for("LINK",trade_pending=1)
    with pytest.raises(HTTPException):
        claim._claim_single_auto_fee_locked(
            cur,did=DID,asset="LINK",key=KEY,remote=remote,
            stable_usd=Decimal("1"),treasury=TREASURY,now=NOW)
    assert not any("INSERT INTO mininex_evm_auto_fee_sends" in q
                   for q,_ in cur.sql)


def test_unknown_privy_fee_result_is_immutable_reconciliation_not_resend():
    cur=Cur([
        {"status":"SENDING","request_digest":"a"*64},
        {"status":"RECONCILIATION_REQUIRED"},
    ])
    result=claim._record_auto_fee_send_locked(
        cur,did=DID,asset="LINK",key=KEY,
        digest="a"*64,tx_hash=None)
    assert result=="RECONCILIATION_REQUIRED"
    assert cur.sql[-1][1][1] is None


def test_mined_fee_hash_remains_only_submitted_until_canonical_finality():
    cur=Cur([
        {"status":"SENDING","request_digest":"a"*64},
        {"status":"SUBMITTED"},
    ])
    state=claim._record_auto_fee_send_locked(
        cur,did=DID,asset="LINK",key=KEY,
        digest="a"*64,tx_hash="0x"+"ed"*32)
    assert state=="SUBMITTED"
    assert not any("PAID_VERIFIED" in q for q,_ in cur.sql)
