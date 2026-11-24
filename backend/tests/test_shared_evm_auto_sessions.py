"""An EVM coin starts only after explicit owner action and fresh Privy grant."""
import asyncio
from datetime import datetime,timedelta,timezone
import inspect

import pytest
from fastapi import HTTPException
from backend import shared_evm_auto_sessions as m
from backend.storage import SCHEMA

DID="did:privy:owner"
WALLET="0x"+"ab"*20
SHA="a"*64
NOW=datetime.now(timezone.utc)
CHAIN={"BNB":56,"POL":137,"AVAX":43114,"LINK":1,"UNI":1,"ARB":42161}


class Cursor:
    def __init__(self,rows):self.rows=iter(rows);self.queries=[]
    def execute(self,sql,args):self.queries.append((sql,args))
    def fetchone(self):return next(self.rows)


def rows(asset):
    stable="USDT" if asset=="BNB" else "USDC"
    return [
        {"asset":asset,"chain_id":CHAIN[asset],"stable":stable,
         "wallet_id":"wallet_1234567","wallet_address":WALLET,
         "owner_id":"owner_1234567","signer_id":"signer_1234567",
         "policy_sha256":SHA,"remote_policy_sha256":SHA,
         "policy_id":"policy_1234567","consent_sha256":SHA,
         "consent_verified_at":NOW-timedelta(minutes=5),
         "grant_verified_at":NOW-timedelta(minutes=2),
         "revoked_at":None,"status":"GRANT_VERIFIED",
         "budget_revision":7,"expires_at":NOW+timedelta(hours=1)},
        {"status":"SIGNED_INTENT","wallet_id":"wallet_1234567",
         "wallet_address":WALLET,"chain_id":CHAIN[asset],
         "stable":stable,"budget_revision":7,"message_sha256":SHA,
         "confirmed_at":NOW-timedelta(minutes=6)},
        {"stable":stable,"budget_usd":150000,"revision":7},
        {"status":"ACTIVE"},
    ]


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI","ARB"])
def test_each_coin_starts_only_from_owner_approved_privy_wallet(asset):
    cur=Cursor(rows(asset))
    result=m._start_locked(cur,did=DID,asset=asset)
    assert result["asset"]==asset
    assert result["status"]=="ACTIVE"
    assert result["started_by_owner"] is True
    assert result["worker_attached"] is False
    assert result["new_transactions_sent"]==0
    assert len(cur.queries)==4
    assert "mininex_live_evm_auto_sessions" in cur.queries[-1][0]


@pytest.mark.parametrize("change",[
    lambda r:r[0].update(status="REVOKED"),
    lambda r:r[0].update(revoked_at=NOW),
    lambda r:r[1].update(status="PENDING"),
    lambda r:r[1].update(message_sha256="b"*64),
    lambda r:r[2].update(revision=6),
    lambda r:r[2].update(budget_usd=0),
])
def test_no_owner_start_without_signed_current_budget_consent(change):
    data=rows("AVAX")
    change(data)
    cur=Cursor(data)
    with pytest.raises(HTTPException):
        m._start_locked(cur,did=DID,asset="AVAX")
    assert not any("INSERT INTO mininex_live_evm_auto_sessions" in q
                   for q,args in cur.queries)


def test_remote_privy_readback_missing_quorum_rejects_activation():
    with pytest.raises(HTTPException):
        m._remote_ready({
            "mode":"EVM_PRIVY_REMOTE_GRANT_INDEPENDENT_AUDIT",
            "asset":"LINK","owner_wallet_current":True,
            "exact_remote_policy_rules_verified":True,
            "privy_additional_signer_scoped":True,
            "backend_quorum_current":False,
            "signed_coin_budget_intent_in_ledger":True}, "LINK")


def test_one_coin_pause_not_other_coin_and_no_unknown_send_retry():
    cur=Cursor([{"status":"PAUSED"}])
    out=m._stop_locked(cur,did=DID,asset="UNI")
    assert out["status"]=="PAUSED"
    assert out["already_submitted_transactions_not_retried_or_cancelled"]
    assert len(cur.queries)==1
    sql,args=cur.queries[0]
    assert "WHERE privy_did=%s AND asset=%s AND (status='ACTIVE' OR status='EXIT_PENDING')" in sql
    assert args==(DID,"UNI")


def test_user_endpoints_required_and_no_implicit_auto_start():
    from backend.app import app
    for suffix,method in [
        ("","GET"),("/start","POST"),("/pause","POST")]:
        route=next(x for x in app.routes if
            x.path==f"/api/live/evm/auto-session/{{asset}}{suffix}")
        assert route.methods=={method}
        assert "verify_privy_bearer" in inspect.getsource(route.endpoint)
    src=inspect.getsource(m)
    assert "post_claimed_privy_evm_rpc" not in src
    assert "sign_privy" not in src
    assert "mininex_live_evm_auto_sessions" in "\n".join(SCHEMA)


@pytest.mark.parametrize("asset",["DOGE","BTC","ADA"])
def test_unscheduled_assets_never_inherit_other_coin_live_grant(asset):
    with pytest.raises(HTTPException) as e:
        m._require_asset(asset)
    assert e.value.status_code==404



def test_session_status_counts_only_original_chain_submissions(monkeypatch):
    """An ACTIVE label alone never creates an imaginary on-chain trade."""
    class ReadCursor:
        def __init__(self):self.calls=[];self.rows=iter([
            {"status":"ACTIVE","chain_id":1,"stable":"USDC","budget_revision":7},
            {"sent":3,"finalized":2},
            {"last_poll_at":NOW},
        ])
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def execute(self,sql,args=None):self.calls.append((sql,args))
        def fetchone(self):return next(self.rows)
    cur=ReadCursor()
    class FakeDB:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kwargs):return cur
    monkeypatch.setattr(m,"_db_url",lambda:"postgres://unused")
    monkeypatch.setattr(m,"_ensure_schema",lambda _:None)
    monkeypatch.setattr(m.psycopg,"connect",lambda *a,**kw:FakeDB())
    state=m.get_owner_evm_auto(DID,"LINK")
    assert state["status"]=="ACTIVE"
    assert state["worker_attached"] is True
    assert state["original_blockchain_swaps_sent"]==3
    assert state["original_blockchain_swaps_finalized"]==2
    assert state["new_transactions_sent"]==0
    assert "tx_hash IS NOT NULL" in cur.calls[1][0]
    assert "mininex_ethereum_settlement_submissions" in cur.calls[1][0]
    assert "mininex_ethereum_settlement_finality" in cur.calls[1][0]
    assert "mininex_ethereum_settlement_manual_attempts" not in cur.calls[1][0]
    assert cur.calls[1][1]==(DID,"LINK")



def test_worker_heartbeat_comes_from_live_loop_not_owner_session_label():
    from backend.shared_evm_live_worker import _persist_evm_worker_heartbeat
    import inspect
    source=inspect.getsource(_persist_evm_worker_heartbeat)
    assert "mininex_live_worker_heartbeats" in source
    assert "ON CONFLICT" in source
    assert "shared_evm_auto" in source
    status_source=inspect.getsource(m.get_owner_evm_auto)
    assert "INTERVAL '90 seconds'" in status_source
    assert "last_poll_at" in status_source


def test_bridge_exit_endpoint_is_owner_authenticated_and_no_start_during_exit():
    from backend.app import app
    route=next(x for x in app.routes if x.path=="/api/live/evm/auto-session/{asset}/exit")
    assert route.methods=={"POST"}
    assert "verify_privy_bearer" in inspect.getsource(route.endpoint)
    assert "WHERE mininex_live_evm_auto_sessions.status<>'EXIT_PENDING'" in inspect.getsource(m._start_locked)
    with pytest.raises(HTTPException):
        m.request_owner_bridge_exit(DID,"BTC")


def test_avax_exit_requests_sell_without_marking_position_closed(monkeypatch):
    cur=Cursor([{"privy_did":DID},{"chain_id":43114,"stable":"USDC","status":"EXIT_PENDING"}])
    class DB:
        def __enter__(self):return self
        def __exit__(self,*a):return False
        def cursor(self,**kw):
            class CM:
                def __enter__(self):return cur
                def __exit__(self,*a):return False
            return CM()
    monkeypatch.setattr(m,"_db_url",lambda:"postgres://unused")
    monkeypatch.setattr(m,"_ensure_schema",lambda _:None)
    monkeypatch.setattr(m.psycopg,"connect",lambda *a,**kw:DB())
    result=m.request_owner_bridge_exit(DID,"AVAX")
    assert result["status"]=="EXIT_PENDING"
    assert result["new_transactions_sent"]==0
    assert any("SET status=\x27EXIT_PENDING\x27" in sql and args==(DID,"AVAX") for sql,args in cur.queries)


def test_exit_completion_waits_for_original_unresolved_bridge(monkeypatch):
    from backend import ethereum_settlement_journal as j
    cur=Cursor([{"privy_did":DID},
                {"status":"EXIT_PENDING","started_by_owner_at":NOW,"wallet_address":WALLET}])
    class DB:
        def __enter__(self):return self
        def __exit__(self,*a):return False
        def cursor(self,**kw):
            class CM:
                def __enter__(self):return cur
                def __exit__(self,*a):return False
            return CM()
    monkeypatch.setattr(m,"_db_url",lambda:"postgres://unused")
    monkeypatch.setattr(m,"_ensure_schema",lambda _:None)
    monkeypatch.setattr(m.psycopg,"connect",lambda *a,**kw:DB())
    monkeypatch.setattr(j,"pending_bridge_order",lambda *a:True)
    assert not m.complete_bridge_exit_if_flat(DID,"POL",NOW)
    assert not any("SET status='STOPPED'" in sql for sql,args in cur.queries)
