"""One-signer Start Trade preparation reads original user-owned wallet/budget."""
from decimal import Decimal
import inspect

import pytest
from fastapi import HTTPException

from backend import shared_evm_wallet_policy_prepare as m
from backend.app import app

NOW=1800000000
DID="did:privy:owner12345"
WALLET="0x"+"a"*40
TREASURY="0x"+"b"*40
OWNER="owner_12345678"
SIGNER="signer_12345678"
OWNED={"address":WALLET,"wallet_id":"wallet_12345678","delegated":False}


def market(now=NOW):
    symbols={
        "ethereum":2500,"binancecoin":500,
        "polygon-ecosystem-token":0.5,"avalanche-2":50,
        "chainlink":20,"quant-network":100,"uniswap":10,
        "arbitrum":1,"usd-coin":1,"tether":1,
    }
    return {coin:{"usd":price,"last_updated_at":now}
            for coin,price in symbols.items()}


class Cur:
    def __init__(self,rows):self.rows=iter(rows);self.calls=[]
    def execute(self,sql,params):self.calls.append((sql,params))
    def fetchone(self):return next(self.rows)


def approved_wallet(metadata_override=None):
    user={"id":DID,"linked_accounts":[{
        "type":"wallet","chain_type":"ethereum",
        "wallet_client_type":"privy","id":OWNED["wallet_id"],
        "address":WALLET,"delegated":False,
    }]}
    meta={
        "id":OWNED["wallet_id"],"chain_type":"ethereum",
        "address":WALLET,
        "entity":{"type":"user","id":DID,"display_name":"MiniNex"},
        "additional_signers":[],"archived_at":None,
    }
    if metadata_override:metadata_override(meta,user)
    return user,meta


def test_exact_privy_wallet_accepts_provider_metadata_extension():
    user,meta=approved_wallet()
    assert m._require_unattached_original_wallet(
        user,DID,meta,
    )["wallet_id"]==OWNED["wallet_id"]


@pytest.mark.parametrize("change",[
    lambda meta,user:meta["additional_signers"].append({
        "signer_id":"old_signer_12345","override_policy_ids":["old_123456"],
    }),
    lambda meta,user:meta["additional_signers"].append({
        "signer_id":"old_signer_12345","override_policy_ids":[],
    }),
    lambda meta,user:meta.update(entity={"type":"user","id":"did:privy:other"}),
    lambda meta,user:meta.update(id="wallet_other12345"),
    lambda meta,user:meta.update(address="0x"+"f"*40),
    lambda meta,user:meta.update(archived_at="2026-10-01T00:00:00Z"),
    lambda meta,user:user["linked_accounts"][0].update(delegated=True),
])
def test_prior_signer_or_other_wallet_requires_migration(change):
    user,meta=approved_wallet(change)
    with pytest.raises(HTTPException) as e:
        m._require_unattached_original_wallet(user,DID,meta)
    assert e.value.status_code==409


def test_exact_eth_budget_is_not_rounded_to_whole_dollars():
    cur=Cur([
        {"planned_capital_usd":Decimal("1234.56"),"revision":7},
        {"stable":"USDC","revision":2},
    ])
    budget,revision=m._locked_selected_budget(
        cur,did=DID,asset="ETH",stable="USDC",expected_revision=7,
    )
    assert budget==Decimal("1234.56")
    assert revision==7
    assert len(cur.calls)==2
    assert all("FOR UPDATE" in sql for sql,params in cur.calls)


@pytest.mark.parametrize("asset,stable,native",[
    ("ETH","USDC",True),("BNB","USDT",True),
    ("POL","USDC",True),("AVAX","USDC",True),
    ("LINK","USDC",False),("QNT","USDC",False),
    ("UNI","USDC",False),("ARB","USDC",False),
])
def test_first_original_wallet_draft_one_signer_per_user_budget(
    monkeypatch,asset,stable,native,
):
    monkeypatch.setattr(m,"owner_fee_treasury",
        lambda **kwargs:TREASURY)
    if asset=="ETH":
        rows=[
            {"privy_did":DID},
            {"planned_capital_usd":Decimal("1200.50"),"revision":2},
            {"stable":stable,"revision":3},
            None,None,{"status":"DRAFT"},
        ]
    else:
        rows=[
            {"privy_did":DID},
            {"budget_usd":1200,"stable":stable,"revision":2},
            None,None,{"status":"DRAFT"},
        ]
    cur=Cur(rows)
    result=m._stage_one_original_wallet_locked(
        cur,did=DID,asset=asset,stable=stable,expected_revision=2,
        snapshot=market(),owned=OWNED,
        owner_id=OWNER,signer_id=SIGNER,now=NOW,
    )
    assert result["status"]=="DRAFT"
    assert result["asset"]==asset
    assert result["scope_coins"]==[asset]
    assert result["shared_signer_id"]==SIGNER
    assert result["policy_created_in_privy"] is False
    assert result["wallet_additional_signer_granted"] is False
    assert result["live_trading_enabled"] is False
    assert result["can_submit"] is False
    insert=cur.calls[-1][1]
    assert insert[0]==DID
    assert insert[4]==SIGNER
    assert len(result["policy_sha256"])==64
    assert (result["scope_rules_count"]==7)==native
    assert insert[6].obj[asset]["native_wrap_atomic_cap"] is not None if native else (
        insert[6].obj[asset]["native_wrap_atomic_cap"] is None
    )


@pytest.mark.parametrize("index,row",[
    (2,{"asset":"LINK"}), # legacy additional coin signer
    (3,{"status":"GRANT_VERIFIED"}), # legacy ETH signer
])
def test_never_stages_over_a_previous_per_coin_grant(monkeypatch,index,row):
    monkeypatch.setattr(m,"owner_fee_treasury",
        lambda **kwargs:TREASURY)
    records=[
        {"privy_did":DID},
        {"budget_usd":1200,"stable":"USDC","revision":2},
        None,None,{"status":"DRAFT"},
    ]
    records[index]=row
    with pytest.raises(HTTPException) as e:
        m._stage_one_original_wallet_locked(
            Cur(records),did=DID,asset="LINK",stable="USDC",
            expected_revision=2,snapshot=market(),owned=OWNED,
            owner_id=OWNER,signer_id=SIGNER,now=NOW,
        )
    assert e.value.status_code==409


def test_duplicate_initial_draft_does_not_overwrite_approved_signer(monkeypatch):
    monkeypatch.setattr(m,"owner_fee_treasury",
        lambda **kwargs:TREASURY)
    cur=Cur([
        {"privy_did":DID},
        {"budget_usd":1200,"stable":"USDC","revision":2},
        None,None,None,
    ])
    with pytest.raises(HTTPException) as e:
        m._stage_one_original_wallet_locked(
            cur,did=DID,asset="LINK",stable="USDC",
            expected_revision=2,snapshot=market(),owned=OWNED,
            owner_id=OWNER,signer_id=SIGNER,now=NOW,
        )
    assert e.value.status_code==409


def test_start_trade_preparation_route_only_authenticated_and_inert():
    route=next(r for r in app.routes if
        r.path=="/api/live/evm/wallet-policy/prepare/{asset}")
    assert route.methods=={"POST"}
    source=inspect.getsource(route.endpoint)
    assert "verify_privy_bearer" in source
    assert "prepare_owner_central_evm_wallet_policy" in source
    assert "addSigners" not in source
    assert "start_owner_evm_auto" not in source
