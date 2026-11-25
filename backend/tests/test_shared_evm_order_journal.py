"""Durable per-asset EVM order journal, no permission or hidden broadcasts."""
import pytest
from fastapi import HTTPException

from backend.shared_evm_order_journal import (
    immutable_evm_send_identity,_claim_locked,_record_result_locked,
)
from backend.shared_evm_swap_receipts import _pair
from backend.shared_evm_execution import EvmPair,unsigned_exact_input_single
from backend.storage import SCHEMA

DID="did:privy:client"
WALLET="0x"+"ab"*20
KEY="sig_abcdef01234567890123456789"
WALLET_ID="wallet_1234567"
DIGEST="a"*64


class Cursor:
    def __init__(self, rows):
        self.rows=iter(rows)
        self.sql=[]
    def execute(self,query,args):
        self.sql.append((query,args))
    def fetchone(self):
        return next(self.rows)


def identity(asset,side="BUY",stable=None):
    stable=stable or ("USDT" if asset=="BNB" else "USDC")
    pair=_pair(asset,stable,side)
    tx=unsigned_exact_input_single(
        pair=EvmPair(asset,pair["chain_id"],pair["network"],
                     pair["source_token"],pair["output_token"],
                     pair["router"],500),
        wallet=WALLET,amount_in=10**6,min_out=12345)
    return immutable_evm_send_identity(
        asset=asset,wallet_id=WALLET_ID,wallet=WALLET,tx=tx,
        side=side,stable=stable,input_atomic="1000000",
        minimum_output_atomic="12345")


@pytest.mark.parametrize("asset,chain",[
    ("BNB",56),("POL",137),("AVAX",43114),("LINK",1),("UNI",1),
])
@pytest.mark.parametrize("side",["BUY","SELL"])
def test_five_evm_assets_have_independent_bound_durable_send_identity(
    asset,chain,side
):
    i=identity(asset,side)
    assert i["chain_id"]==chain
    assert i["wallet_address"]==WALLET
    assert len(i["request_digest"])==64
    assert i["signed_or_sent"] is False
    cur=Cursor([{"privy_did":DID},{"pending":0}] +
               ([{"pending":0},{"pending":0}] if chain==1 else
                [{"pending":0},{"pending":0},{"pending":0},{"pending":0}]))
    _claim_locked(cur,did=DID,key=KEY,identity=i)
    assert len(cur.sql)==(5 if chain==1 else 7)
    assert "FOR UPDATE" in cur.sql[0][0]
    assert "chain_id=%s" in cur.sql[1][0]
    assert cur.sql[1][1]==(DID,chain,DID,chain,DID,chain,DID,chain,DID,chain)
    assert "mininex_avax_staged_steps" in cur.sql[1][0]
    assert "mininex_live_native_order_intents" in cur.sql[1][0]
    assert "mininex_evm_auto_fee_sends" in cur.sql[1][0]
    assert "'SENDING'" in cur.sql[-1][0]
    assert i["request_digest"] in cur.sql[-1][1]


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI"])
def test_unresolved_network_order_prevents_duplicate_wallet_send(asset):
    cur=Cursor([{"privy_did":DID},{"pending":1}])
    with pytest.raises(HTTPException) as error:
        _claim_locked(cur,did=DID,key=KEY,identity=identity(asset))
    assert error.value.status_code==409
    assert len(cur.sql)==2


@pytest.mark.parametrize("asset",["LINK","UNI"])
@pytest.mark.parametrize("busy_step",["eth_swap","eth_wrap"])
def test_shared_ethereum_nonce_blocks_erc20_when_eth_operation_pending(asset,busy_step):
    rows=[{"privy_did":DID},{"pending":0}]
    if busy_step=="eth_swap":
        rows.append({"pending":1})
    else:
        rows.extend(({"pending":0},{"pending":1}))
    cur=Cursor(rows)
    with pytest.raises(HTTPException) as err:
        _claim_locked(cur,did=DID,key=KEY,identity=identity(asset))
    assert err.value.status_code==409
    assert not any("INSERT INTO mininex_live_evm_auto_broadcasts" in q for q,a in cur.sql)


@pytest.mark.parametrize("digest,tx_hash,expected",[
    (DIGEST,"0x"+"ab"*32,"SUBMITTED"),
    (DIGEST,None,"RECONCILIATION_REQUIRED"),
])
def test_immutable_send_outcome_never_retries(digest,tx_hash,expected):
    cur=Cursor([{"request_digest":digest,"status":"SENDING"}])
    state=_record_result_locked(
        cur,did=DID,asset="AVAX",key=KEY,
        digest=digest,tx_hash=tx_hash)
    assert state==expected
    assert "'SENDING'" in cur.sql[1][0]
    assert cur.sql[1][1][0]==expected


@pytest.mark.parametrize("row",[
    {"request_digest":DIGEST,"status":"SUBMITTED"},
    {"request_digest":"b"*64,"status":"SENDING"},
    None,
])
def test_claimed_or_conflicting_order_cannot_be_replayed(row):
    cur=Cursor([row])
    with pytest.raises(HTTPException):
        _record_result_locked(
            cur,did=DID,asset="LINK",key=KEY,
            digest=DIGEST,tx_hash="0x"+"ab"*32)
    assert len(cur.sql)==1


def test_eth_legacy_journal_is_not_silently_replaced():
    with pytest.raises(ValueError):
        identity("ETH")
    assert any("CREATE TABLE IF NOT EXISTS mininex_live_evm_auto_broadcasts" in s
               for s in SCHEMA)
    assert any("CREATE TABLE IF NOT EXISTS mininex_live_eth_auto_broadcasts" in s
               for s in SCHEMA)


def test_new_journal_not_accessible_from_public_api():
    from backend.app import app
    assert all("evm-auto-broadcast" not in r.path for r in app.routes)


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
def test_unresolved_native_conversion_blocks_same_chain_swap(asset):
    cur=Cursor([{"privy_did":DID},{"pending":0},{"pending":1}])
    with pytest.raises(HTTPException):
        _claim_locked(cur,did=DID,key=KEY,identity=identity(asset))
    assert len(cur.sql)==3
    assert "mininex_live_evm_native_auto_steps" in cur.sql[-1][0]
    assert not any("INSERT INTO mininex_live_evm_auto_broadcasts" in q
                   for q,args in cur.sql)


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
def test_pending_native_sell_deposit_blocks_same_chain_swap(asset):
    cur=Cursor([{"privy_did":DID},{"pending":0},
                {"pending":0},{"pending":1}])
    with pytest.raises(HTTPException) as err:
        _claim_locked(cur,did=DID,key=KEY,identity=identity(asset))
    assert err.value.status_code==409
    assert "mininex_live_evm_native_sell_intents" in cur.sql[-1][0]
    assert not any("INSERT INTO mininex_live_evm_auto_broadcasts" in sql
                   for sql,args in cur.sql)


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
def test_another_signal_cannot_preempt_earlier_finalized_native_sell(asset):
    cur=Cursor([{"privy_did":DID},{"pending":0},
                {"pending":0},{"pending":0},{"pending":1}])
    with pytest.raises(HTTPException) as err:
        _claim_locked(cur,did=DID,key=KEY,identity=identity(asset))
    assert err.value.status_code==409
    assert "AND n.intent_key<>%s" in cur.sql[-1][0]
    assert KEY==cur.sql[-1][1][-1]
    assert not any("INSERT INTO mininex_live_evm_auto_broadcasts" in q
                   for q,args in cur.sql)


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
def test_other_swap_cannot_spend_unsettled_native_buy_position(asset):
    cur=Cursor([{"privy_did":DID},{"pending":0},{"pending":0},
                {"pending":0},{"pending":0},{"pending":1}])
    with pytest.raises(HTTPException):
        _claim_locked(cur,did=DID,key=KEY,identity=identity(asset))
    assert "mininex_live_evm_completed_native_cycles" in cur.sql[-1][0]
    assert not any("INSERT INTO mininex_live_evm_auto_broadcasts" in sql
                   for sql,args in cur.sql)



def test_verified_one_signature_grant_reaches_durable_send_without_eip191(monkeypatch):
    from datetime import datetime, timezone, timedelta
    from backend.shared_evm_order_journal import _claim_authorized_evm_order_locked
    from backend.shared_evm_remote_grant_audit import _grant_scope_digest

    owner=dict(
        asset="LINK", stable="USDC", chain_id=1, wallet_id=WALLET_ID,
        wallet_address=WALLET, owner_id="owner_123456",
        signer_id="signer_123456", policy_id="policy_123456",
        policy_sha256="a"*64, remote_policy_sha256="a"*64,
        budget_revision=5, authorization_method="PRIVY_SCOPED_GRANT",
        status="GRANT_VERIFIED", revoked_at=None,
        expires_at=datetime.now(timezone.utc)+timedelta(hours=2),
    )
    owner["consent_sha256"]=_grant_scope_digest(
        did=DID,record=owner,budget_usd=1500)
    settings={"stable":"USDC","revision":5,"budget_usd":1500}
    reserve={"state":"RESERVED","side":"BUY","stable":"USDC",
             "budget_revision":5,"quote_sha256":"b"*64}
    session={"status":"ACTIVE","chain_id":1,"stable":"USDC",
             "wallet_address":WALLET,"budget_revision":5,
             "started_by_owner_at":datetime.now(timezone.utc)}
    cur=Cursor([
        owner,settings,reserve,session,
        {"privy_did":DID},{"pending":0},{"pending":0},{"pending":0},
        {"state":"SENDING"},
    ])
    _claim_authorized_evm_order_locked(
        cur,did=DID,key=KEY,identity=identity("LINK"),
        quote_sha256="b"*64,budget_revision=5)
    assert not any("mininex_live_evm_consent_challenges" in sql
                   for sql,_ in cur.sql)
    assert any("INSERT INTO mininex_live_evm_auto_broadcasts" in sql
               for sql,_ in cur.sql)
    assert "budget_usd" in cur.sql[1][0]


def test_one_signature_send_rejects_changed_capital_before_claim():
    from datetime import datetime, timezone, timedelta
    from backend.shared_evm_order_journal import _claim_authorized_evm_order_locked
    from backend.shared_evm_remote_grant_audit import _grant_scope_digest

    owner=dict(
        asset="LINK",stable="USDC",chain_id=1,wallet_id=WALLET_ID,
        wallet_address=WALLET,owner_id="owner_123456",
        signer_id="signer_123456",policy_id="policy_123456",
        policy_sha256="a"*64,remote_policy_sha256="a"*64,
        budget_revision=5,authorization_method="PRIVY_SCOPED_GRANT",
        status="GRANT_VERIFIED",revoked_at=None,
        expires_at=datetime.now(timezone.utc)+timedelta(hours=2),
    )
    owner["consent_sha256"]=_grant_scope_digest(
        did=DID,record=owner,budget_usd=1500)
    cur=Cursor([owner,{"stable":"USDC","revision":5,"budget_usd":1501}])
    with pytest.raises(HTTPException) as exc:
        _claim_authorized_evm_order_locked(
            cur,did=DID,key=KEY,identity=identity("LINK"),
            quote_sha256="b"*64,budget_revision=5)
    assert exc.value.status_code==409
    assert not any("INSERT INTO mininex_live_evm_auto_broadcasts" in sql
                   for sql,_ in cur.sql)


def test_erc20_ethereum_claim_checks_original_native_buy_nonce():
    for asset in ("LINK","UNI"):
        cur=Cursor([{"privy_did":DID},{"pending":1}])
        with pytest.raises(HTTPException):
            _claim_locked(cur,did=DID,key=KEY,identity=identity(asset))
        sql,args=cur.sql[1]
        assert "mininex_live_native_order_intents" in sql
        assert "chain='ethereum-mainnet'" in sql
        assert args[-2:]==(DID,1)
