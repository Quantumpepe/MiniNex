"""Native BTC/SOL/XRP one-shot journal; no live signing or RPC in tests."""
from datetime import datetime,timedelta,timezone
import pytest
from fastapi import HTTPException

from backend import native_live_order_journal as journal
from backend.manual_swap_live_quotes import THOR_ASSETS
from backend.native_coin_balances import _checksum,_RIPPLE
from backend.storage import SCHEMA

DID="did:privy:native_original_owner_123"
ETH="0x"+"a"*40
BTC="bc1q"+"q"*38
SOL_CHARS="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
B58="123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def b58(raw,alphabet):
    n=int.from_bytes(raw,"big")
    out=""
    while n:
        n,r=divmod(n,58)
        out=alphabet[r]+out
    return alphabet[0]*(len(raw)-len(raw.lstrip(b"\x00")))+out
SOL=b58(bytes([7])*32,SOL_CHARS)
xrp_payload=b"\x00"+bytes([3])*20
XRP=b58(xrp_payload+_checksum(xrp_payload),_RIPPLE)
FUTURE=datetime.now(timezone.utc)+timedelta(minutes=3)


def identity(asset="BTC",side="SELL",amount="0.001",signal="signal001",
             stable="USDC",change=None,expires_at=None):
    native={"BTC":BTC,"SOL":SOL,"XRP":XRP}[asset]
    source=ETH if side=="BUY" else native
    destination=native if side=="BUY" else ETH
    chain="ethereum-mainnet" if side=="BUY" else journal._NATIVE_NETWORK[asset]
    target=journal._NATIVE_NETWORK[asset] if side=="BUY" else "ethereum-mainnet"
    vault={
        "BTC":"bc1q"+"p"*38,
        "SOL":b58(bytes([8])*32,SOL_CHARS),
        "XRP":b58(bytes([0])+bytes([8])*20+
            _checksum(bytes([0])+bytes([8])*20),_RIPPLE),
    }[asset] if side=="SELL" else "0x"+"c"*40
    quote={
        "mode":"THORCHAIN_DESTINATION_BOUND_READ_ONLY",
        "from_network":chain,"to_network":target,
        "source_asset":stable if side=="BUY" else asset,
        "destination_asset":asset if side=="BUY" else stable,
        "amount":amount,"source_wallet":source,
        "destination_wallet":destination,"inbound_vault":vault,
        "inbound_router":"0x"+"b"*40 if side=="BUY" else None,
        "needs_erc20_router":side=="BUY",
        "quote_expires_at":expires_at or FUTURE,
        "memo":"=:"+THOR_ASSETS[asset if side=="BUY" else stable][0]+
               ":"+destination+"/"+source+":0/1/0",
        "expected_output_units_1e8":"1000000",
        "venue_fee_units_1e8":"1000",
        "original_vault_verified":True,
        "required_chain_memo_verified":True,
        "refund_path_verified":True,
        "exact_venue_fees_verified":True,
        "transaction_encoding_verified":False,
        "privy_native_signer_verified":False,
        "source_transaction_sent":False,
        "execution_enabled":False,"can_submit":False,
    }
    if change:quote.update(change)
    return journal.immutable_native_order_identity(
        did=DID,asset=asset,side=side,stable=stable,
        source_wallet=source,destination_wallet=destination,
        amount=amount,quote=quote,quote_expires_at=expires_at or FUTURE,
        strategy_run_id="native_strategy_001",signal_event_id=signal,
        budget_revision=3,owner_consent_sha256="a"*64,
        privy_grant_sha256="b"*64,
    )


@pytest.mark.parametrize("asset,side,amount",[
    ("BTC","SELL","0.00000001"),("BTC","BUY","10.000001"),
    ("SOL","SELL","0.000000001"),("SOL","BUY","10"),
    ("XRP","SELL","0.000001"),("XRP","BUY","50"),
])
def test_three_coins_keep_exact_native_source_and_owner_signer_context(
    asset,side,amount,
):
    row=identity(asset,side,amount)
    assert row["asset"]==asset
    assert row["side"]==side
    assert row["chain"]==("ethereum-mainnet" if side=="BUY" else
                           journal._NATIVE_NETWORK[asset])
    assert len(row["intent_key"])==71
    assert row["quote_expires_at"]>datetime.now(timezone.utc)
    assert len(row["payload_sha256"])==64
    assert row["source_wallet"]!=(row["destination_wallet"])


def test_retrying_same_strategy_signal_has_fixed_key_but_new_terms_conflict():
    old=identity()
    new=identity(amount="0.002")
    assert old["intent_key"]==new["intent_key"]
    assert old["payload_sha256"]!=new["payload_sha256"]


@pytest.mark.parametrize("change",[
    {"mode":"NATIVE_MULTICHAIN_REAL_MARKET_QUOTE_ONLY"},
    {"original_vault_verified":False},
    {"required_chain_memo_verified":False},
    {"refund_path_verified":False},
    {"exact_venue_fees_verified":False},
    {"can_submit":True},
    {"source_asset":"ETH"},
    {"destination_wallet":"0x"+"b"*40},
    {"to_network":"bitcoin-mainnet"},
])
def test_public_read_only_quote_never_grants_real_send(change):
    with pytest.raises(ValueError):
        identity(change=change)


@pytest.mark.parametrize("asset,side,amount,stable",[
    ("BTC","SELL","0.000000001","USDC"),
    ("SOL","SELL","0.0000000001","USDC"),
    ("XRP","SELL","0.0000001","USDC"),
    ("XRP","BUY","1","USDT"),
    ("SOL","BUY","0.0000001","USDC"),
])
def test_wrong_precision_or_unsupported_stable_cannot_claim(
    asset,side,amount,stable,
):
    with pytest.raises(ValueError):
        identity(asset,side,amount,stable=stable)


def test_expired_quote_rejected_even_when_old_wallet_metadata_exists():
    with pytest.raises(ValueError):
        identity(expires_at=datetime.now(timezone.utc)-timedelta(seconds=1))


class Cursor:
    def __init__(self,rows):
        self.rows=iter(rows)
        self.queries=[]
    def execute(self,sql,params):
        self.queries.append((sql,params))
    def fetchone(self):
        return next(self.rows)


def test_first_persist_and_stable_replay_are_idempotent():
    original=identity()
    cur=Cursor([{"privy_did":DID},None,{"status":"PREPARED"}])
    assert journal._persist_locked(cur,original)=="PREPARED"
    assert "INSERT INTO mininex_live_native_order_intents" in cur.queries[-1][0]
    again=Cursor([{"privy_did":DID},
                  {"payload_sha256":original["payload_sha256"],
                   "status":"PREPARED"}])
    assert journal._persist_locked(again,original)=="PREPARED"
    assert not any("INSERT INTO" in s for s,args in again.queries)


def test_same_signal_with_changed_price_or_amount_fails_closed():
    old=identity()
    newer=identity(amount="0.002")
    cur=Cursor([{"privy_did":DID},
                {"payload_sha256":old["payload_sha256"],
                 "status":"PREPARED"}])
    with pytest.raises(HTTPException) as error:
        journal._persist_locked(cur,newer)
    assert error.value.status_code==409
    assert not any("INSERT INTO" in s for s,args in cur.queries)


def audit(row):
    return {"did":DID,"asset":row["asset"],
            "payload_sha256":row["payload_sha256"],
            "owner_consent_sha256":row["owner_consent_sha256"],
            "privy_grant_sha256":row["privy_grant_sha256"],
            "budget_revision":row["budget_revision"],
            "source_wallet":row["source_wallet"],
            "owner_wallet_current":True,
            "exact_remote_policy_verified":True,
            "backend_quorum_current":True,
            "owner_signed_budget_current":True}


def test_atomic_one_shot_claim_requires_privy_and_stable_owner_intent():
    row=identity()
    original={"status":"PREPARED","chain":row["chain"],
              "payload_sha256":row["payload_sha256"],
              "quote_expires_at":FUTURE,
              "source_wallet":row["source_wallet"],
              "owner_consent_sha256":row["owner_consent_sha256"],
              "privy_grant_sha256":row["privy_grant_sha256"],
              "budget_revision":row["budget_revision"]}
    cur=Cursor([{"privy_did":DID},original,{"pending":0},
                {"status":"SENDING"}])
    result=journal._claim_once_locked(
        cur,did=DID,asset="BTC",key=row["intent_key"],
        digest=row["payload_sha256"],permission_audit=audit(row))
    assert result=="SENDING"
    assert "FOR UPDATE" in cur.queries[0][0]
    assert "quote_expires_at>now()" in cur.queries[-1][0]
    assert any("RECONCILIATION_REQUIRED" in s for s,args in cur.queries)


@pytest.mark.parametrize("bad",[
    {"backend_quorum_current":False},
    {"owner_wallet_current":False},
    {"exact_remote_policy_verified":False},
    {"owner_signed_budget_current":False},
    {"asset":"SOL"},{"payload_sha256":"0"*64},
])
def test_native_claim_without_remote_verified_policy_never_checks_database(bad):
    row=identity()
    report=audit(row);report.update(bad)
    cur=Cursor([])
    with pytest.raises(HTTPException):
        journal._claim_once_locked(
            cur,did=DID,asset=row["asset"],
            key=row["intent_key"],digest=row["payload_sha256"],
            permission_audit=report)
    assert cur.queries==[]


def test_existing_unresolved_wallet_send_serializes_next_source_nonce():
    row=identity()
    cur=Cursor([
        {"privy_did":DID},
        {"chain":row["chain"],"status":"PREPARED",
         "payload_sha256":row["payload_sha256"],
         "source_wallet":row["source_wallet"],
         "owner_consent_sha256":row["owner_consent_sha256"],
         "privy_grant_sha256":row["privy_grant_sha256"],
         "budget_revision":row["budget_revision"]},
        {"pending":1},
    ])
    with pytest.raises(HTTPException):
        journal._claim_once_locked(cur,did=DID,asset=row["asset"],
            key=row["intent_key"],digest=row["payload_sha256"],
            permission_audit=audit(row))
    assert not any("SET status='SENDING'" in s for s,args in cur.queries)


@pytest.mark.parametrize("asset,side,tx_hash",[
    ("BTC","SELL","f"*64),
    ("SOL","SELL",b58(bytes([1])*64,SOL_CHARS)),
    ("XRP","SELL","A"*64),
    ("BTC","BUY","0x"+"a"*64),
])
def test_one_submitted_native_source_tx_is_written_only_after_sending(
    asset,side,tx_hash,
):
    row=identity(asset,side)
    cur=Cursor([
        {"status":"SENDING","side":side,
         "payload_sha256":row["payload_sha256"],"source_tx_hash":None},
        {"status":"SUBMITTED"},
    ])
    assert journal._record_send_result_locked(
        cur,did=DID,asset=asset,key=row["intent_key"],
        digest=row["payload_sha256"],tx_hash=tx_hash)=="SUBMITTED"
    assert cur.queries[-1][1][0]=="SUBMITTED"


def test_unknown_privy_submission_retains_non_retriable_reconciliation():
    row=identity()
    cur=Cursor([
        {"status":"SENDING","side":"SELL",
         "payload_sha256":row["payload_sha256"],"source_tx_hash":None},
        {"status":"RECONCILIATION_REQUIRED"},
    ])
    assert journal._record_send_result_locked(
        cur,did=DID,asset="BTC",key=row["intent_key"],
        digest=row["payload_sha256"],tx_hash=None
    )=="RECONCILIATION_REQUIRED"
    replay=Cursor([{"status":"RECONCILIATION_REQUIRED","side":"SELL",
                    "payload_sha256":row["payload_sha256"],
                    "source_tx_hash":None}])
    with pytest.raises(HTTPException):
        journal._record_send_result_locked(
            replay,did=DID,asset="BTC",key=row["intent_key"],
            digest=row["payload_sha256"],tx_hash=None)


def test_bad_cross_network_transaction_hash_cannot_be_submitted():
    row=identity("SOL")
    cur=Cursor([{"status":"SENDING","side":"SELL",
                 "payload_sha256":row["payload_sha256"],"source_tx_hash":None}])
    with pytest.raises(ValueError):
        journal._record_send_result_locked(
            cur,did=DID,asset="SOL",key=row["intent_key"],
            digest=row["payload_sha256"],tx_hash="0x"+"a"*64)
    assert not any("UPDATE " in s for s,args in cur.queries)


def test_schema_adds_wallet_journal_without_trading_caps_or_replaying_sends():
    q="\n".join(SCHEMA)
    assert "CREATE TABLE IF NOT EXISTS mininex_live_native_order_intents" in q
    assert "PRIMARY KEY(privy_did,asset,intent_key)" in q
    assert "privy_grant_sha256" in q and "owner_consent_sha256" in q
    assert "RECONCILIATION_REQUIRED" in q
    assert "quote_expires_at" in q
    assert "CREATE UNIQUE INDEX IF NOT EXISTS mininex_native_original_source_hash_unique" in q

@pytest.mark.parametrize("bad",[
    {"owner_consent_sha256":"c"*64},
    {"privy_grant_sha256":"c"*64},
    {"source_wallet":"0x"+"c"*40},
    {"budget_revision":4},
])
def test_native_grant_cannot_override_original_ledger_owner_terms(bad):
    row=identity()
    report=audit(row)
    report.update(bad)
    stored={
        "status":"PREPARED","chain":row["chain"],
        "payload_sha256":row["payload_sha256"],
        "quote_expires_at":FUTURE,
        "source_wallet":row["source_wallet"],
        "owner_consent_sha256":row["owner_consent_sha256"],
        "privy_grant_sha256":row["privy_grant_sha256"],
        "budget_revision":row["budget_revision"],
    }
    cur=Cursor([{"privy_did":DID},stored])
    with pytest.raises(HTTPException) as error:
        journal._claim_once_locked(
            cur,did=DID,asset=row["asset"],key=row["intent_key"],
            digest=row["payload_sha256"],permission_audit=report)
    assert error.value.status_code==409
    assert len(cur.queries)==2
    assert not any("SET status='SENDING'" in query for query,args in cur.queries)


@pytest.mark.parametrize("change",[
    {"source_wallet":"bc1q"+"r"*38},
    {"inbound_vault":"FAKE"},
    {"inbound_router":"0x"+"z"*40},
    {"needs_erc20_router":False},
    {"quote_expires_at":datetime.now(timezone.utc)},
    {"memo":"=:BTC.BTC:FAKE"},
    {"memo":"=:BTC.BTC:"+BTC+"/0x"+"f"*40+":0/1/0"},
    {"transaction_encoding_verified":True},
    {"source_transaction_sent":True},
    {"execution_enabled":True},
])
def test_original_quote_cannot_forge_wallet_expiry_or_privy_authority(change):
    with pytest.raises(ValueError):
        identity(asset="BTC",side="BUY",amount="50",change=change)


def test_original_private_quote_can_hash_utc_datetime_without_fabricating_send():
    result=identity(asset="SOL",side="SELL",amount="0.1")
    assert result["chain"]=="solana-mainnet"
    assert result["source_wallet"]==SOL
    assert len(result["immutable_quote_sha256"])==64
    assert len(result["payload_sha256"])==64
