"""Stage 28 read-only ETH LIVE capital/rolling daily exposure checks."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as api
from backend import eth_live_ledger_audit as audit

DID="did:privy:safety-stage28"


def limits(**change):
    d={
        "planned_capital_usd":Decimal("5000"),
        "max_order_usd":Decimal("1000"),
        "max_daily_turnover_usd":Decimal("8000"),
        "revision":10,
        "trading_paused":True,
    }
    d.update(change)
    return d


def totals(**change):
    t={
        "pending_usd":Decimal("1200"),
        "confirmed_24h_usd":Decimal("3100"),
        "pending_count":2,
        "unreconciled_count":1,
        "stale_pending_count":1,
    }
    t.update(change)
    return t


def test_eth_pending_and_confirmed_counts_limit_24h_and_capital():
    result=audit.assess_eth_live_ledger(limits(), totals())
    assert result["mode"]=="ETH_LIVE_LEDGER_READ_ONLY"
    assert result["pending_reserved_usd"]=="1200"
    assert result["confirmed_24h_usd"]=="3100"
    assert result["available_capital_after_pending_usd"]=="3800"
    assert result["available_24h_after_pending_usd"]=="3700"
    assert result["unresolved_order_count"]==2
    assert result["reconciliation_required_count"]==1
    assert result["older_than_30m_pending_count"]==1
    assert "UNRECONCILED_LIVE_ORDERS" in result["blockers"]
    assert "STALE_PENDING_LIVE_ORDERS" in result["blockers"]
    assert "USER_STOPPED" in result["blockers"]
    assert result["paper_simulation_included"] is False
    assert result["current_wallet_balance_verified"] is False
    assert result["cumulative_spend_enforced_at_execution"] is False
    assert result["execution_enabled"] is False
    assert result["can_submit"] is False


def test_budget_clamps_to_zero_and_reports_over_limit_before_any_send():
    result=audit.assess_eth_live_ledger(
        limits(),totals(pending_usd="5200",confirmed_24h_usd="3900",
                        unreconciled_count=0,stale_pending_count=0),
    )
    assert result["available_capital_after_pending_usd"]=="0"
    assert result["available_24h_after_pending_usd"]=="0"
    assert "ROLLING_TURNOVER_OVER_LIMIT" in result["blockers"]
    assert "RESERVED_CAPITAL_OVER_LIMIT" in result["blockers"]
    assert result["ready_for_live"] is False


def test_empty_live_ledger_does_not_enable_signing():
    result=audit.assess_eth_live_ledger(
        limits(),totals(pending_usd=0,confirmed_24h_usd=0,
                        pending_count=0,unreconciled_count=0,
                        stale_pending_count=0),
    )
    assert result["available_capital_after_pending_usd"]=="5000"
    assert result["available_24h_after_pending_usd"]=="8000"
    assert result["ready_for_live"] is False
    assert "LIVE_EXECUTOR_DISABLED" in result["blockers"]


@pytest.mark.parametrize("bad",[
    {"pending_usd":"NaN"},{"pending_usd":"Infinity"},
    {"pending_usd":"-1"},{"pending_usd":"1.1234567"},
    {"pending_usd":"1000000000000"},{"pending_usd":None},
    {"confirmed_24h_usd":True},
    {"pending_count":True},{"pending_count":-1},
    {"pending_count":0},{"pending_count":3,"unreconciled_count":4},
    {"pending_count":2,"unreconciled_count":3},
    {"stale_pending_count":3},
    {"pending_usd":0,"pending_count":2},
])
def test_bad_or_inconsistent_eth_ledger_fails_closed(bad):
    with pytest.raises(HTTPException) as error:
        audit.assess_eth_live_ledger(limits(),totals(**bad))
    assert error.value.status_code==503


@pytest.mark.parametrize("bad",[
    {"revision":True},{"revision":-1},{"trading_paused":"true"},
    {"max_order_usd":"10000"},{"planned_capital_usd":"NaN"},
    {"max_daily_turnover_usd":None},
])
def test_invalid_account_limits_cannot_create_audit_report(bad):
    with pytest.raises(HTTPException) as error:
        audit.assess_eth_live_ledger(limits(**bad),totals())
    assert error.value.status_code==503


def test_readonly_24h_account_query_uses_one_user_and_locks(monkeypatch):
    class Cur:
        def __init__(self):self.queries=[];self.result=None
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def execute(self,sql,params):
            query=" ".join(sql.split())
            self.queries.append((query,params))
            if query.startswith("SELECT planned_capital_usd,"):
                assert "FOR UPDATE" in query
                assert params==(DID,)
                self.result=limits()
            elif query.startswith("SELECT COALESCE"):
                assert "FROM mininex_live_order_intents" in query
                assert "asset='ETH'" in query
                assert params[-1]==DID
                assert len(params)==4
                assert params[0]==params[1]==params[2]==sorted(audit._PENDING)
                self.result=totals()
            else:raise AssertionError("Unexpected wallet audit SQL")
        def fetchone(self):return self.result
    cur=Cur()
    class Conn:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kw):return cur
    monkeypatch.setattr(audit,"_db_url",lambda:"postgresql://mock")
    monkeypatch.setattr(audit,"_ensure_schema",lambda url:None)
    monkeypatch.setattr(audit,"_prepare_rows",lambda *args:None)
    monkeypatch.setattr(audit.psycopg,"connect",
        lambda url,connect_timeout=6:Conn())
    value=audit.get_eth_live_ledger_audit(DID)
    assert value["unresolved_order_count"]==2
    assert [q.split(" ")[0] for q,p in cur.queries]==["SELECT","SELECT"]


@pytest.fixture
def auth(monkeypatch):
    private=ec.generate_private_key(ec.SECP256R1())
    pub=private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    monkeypatch.setenv("MININEX_PRIVY_VERIFICATION_KEY",pub)
    now=int(time.time())
    token=jwt.encode({
        "iss":"privy.io","aud":"cmw7sb7ty013o0gl55aqyzi3y",
        "sub":DID,"sid":"stage28-owner-session",
        "iat":now,"exp":now+500,
    },private,algorithm="ES256")
    return {"Authorization":"Bearer "+token}


def test_ledgers_only_auth_for_user_no_post_no_wallet_selector(auth,monkeypatch):
    seen=[]
    def fetch(did):
        seen.append(did)
        return audit.assess_eth_live_ledger(limits(),totals())
    monkeypatch.setattr(api,"get_eth_live_ledger_audit",fetch)
    client=TestClient(api.app)
    path="/api/live/eth/ledger-audit"
    assert client.get(path).status_code==401
    assert client.get(path,headers={"Authorization":"Bearer wrong"}).status_code==401
    resp=client.get(path,headers=auth,params={"wallet_address":"0x"+"b"*40})
    assert resp.status_code==200
    assert resp.headers["cache-control"]=="private, no-store"
    assert resp.json()["execution_enabled"] is False
    assert seen==[DID]
    assert client.post(path,headers=auth).status_code==405
