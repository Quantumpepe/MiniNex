"""One owner Privy scoped grant, no preceding EIP191 signature.

The prepared policy is inert until an independent remote wallet owner,
exact restricted policy and signer readback succeeds. Capital revisions
cannot silently inherit another grant. No real user signing or network IO.
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from backend.shared_evm_policy_candidate_stage import _stage_locked
from backend.shared_evm_remote_grant_audit import _grant_scope_digest
from backend.eth_privy_user_grant import eth_grant_budget_digest


class Prepared:
    def __init__(self, responses):
        self.rows=iter(responses)
        self.queries=[]
    def execute(self,sql,args):
        self.queries.append((sql,args))
    def fetchone(self):
        return next(self.rows)


@pytest.mark.parametrize("asset,chain",[
    ("BNB",56),("POL",137),("AVAX",43114),
    ("LINK",1),("UNI",1),("ARB",42161),
])
def test_one_evm_privy_grant_draft_never_demands_extra_message_signature(asset,chain):
    cur=Prepared([
        {"budget_usd":1500,"stable":("USDT" if asset=="BNB" else "USDC"),
         "revision":7},
        None,
        {"status":"DRAFT"},
    ])
    wallet="0x"+"ab"*20
    prepared=_stage_locked(
        cur,did="did:privy:single_grant_user",asset=asset,
        owned={"wallet_id":"wallet_123456","address":wallet},
        owner_id="backend_policy_owner_001",
        signer_id="backend_executor_001",
        buy_source_atomic_cap="1500000000",
        sell_source_atomic_cap="2500000000000000000",
        native_wrap_atomic_cap=("2500000000000000000"
                                if asset in ("BNB","POL","AVAX") else None),
        expires_at=1_900_000_000,now=1_899_990_000,
        single_privy_grant=True,
    )
    assert prepared["asset"]==asset
    assert prepared["chain_id"]==chain
    assert prepared["mode"]=="OWNER_EVM_PRIVY_GRANT_DRAFT_ONLY"
    assert prepared["owner_delegation_granted"] is False
    assert prepared["policy_published_in_privy"] is False
    assert prepared["live_auto_enabled"] is False
    assert len(cur.queries)==3
    assert all("consent_challenges" not in sql for sql,_ in cur.queries)
    sql,params=cur.queries[-1]
    assert "authorization_method" in sql
    assert params[-1]=="PRIVY_SCOPED_GRANT"
    assert params[-6] is None # no fabricated owner's EIP191 digest


def test_one_evm_owner_grant_budget_revision_and_wallet_are_immutable():
    record={
        "authorization_method":"PRIVY_SCOPED_GRANT",
        "asset":"LINK","chain_id":1,"stable":"USDC",
        "wallet_id":"wallet_123456","wallet_address":"0x"+"aa"*20,
        "owner_id":"owner_123456","signer_id":"signer_123456",
        "policy_id":"policy_123456","policy_sha256":"a"*64,
        "budget_revision":3,
    }
    digest=_grant_scope_digest(
        did="did:privy:original",record=record,budget_usd=1500)
    for field,value in (
        ("budget_revision",4),
        ("wallet_address","0x"+"bb"*20),
        ("asset","UNI"),
        ("policy_id","policy_789123"),
    ):
        altered=deepcopy(record)
        altered[field]=value
        assert _grant_scope_digest(
            did="did:privy:original",record=altered,budget_usd=1500)!=digest
    assert _grant_scope_digest(
        did="did:privy:original",record=record,budget_usd=1501)!=digest


def test_one_eth_grant_binds_original_live_budget_and_revision():
    policy={
        "authorization_method":"PRIVY_SCOPED_GRANT",
        "wallet_address":"0x"+"ab"*20,
        "limits_revision":4,
        "policy_owner_id":"owner_123456",
        "execution_signer_id":"signer_123456",
        "policy_id":"policy_123456",
        "candidate_sha256":"a"*64,
    }
    limits={
        "planned_capital_usd":"1500",
        "max_order_usd":"1500",
        "max_daily_turnover_usd":"1500",
    }
    before=eth_grant_budget_digest(
        did="did:privy:original",policy=policy,limits=limits)
    for field,value in (("planned_capital_usd","1501"),
                        ("max_order_usd","1400")):
        changed=dict(limits,**{field:value})
        assert eth_grant_budget_digest(
            did="did:privy:original",policy=policy,limits=changed)!=before
    assert eth_grant_budget_digest(
        did="did:privy:original",
        policy=dict(policy,limits_revision=5),limits=limits)!=before
