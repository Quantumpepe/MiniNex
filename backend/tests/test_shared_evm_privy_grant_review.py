"""Exact readback of Privy per-coin policies and the owner's additional signer."""
from copy import deepcopy

import pytest

from backend.shared_evm_privy_grant_review import (
    verify_exact_remote_evm_policy,assess_owner_evm_privy_grant,
)
from backend.shared_evm_privy_user_policy import build_user_evm_policy_blueprint

DID="did:privy:owner"
WALLET="0x"+"ab"*20
WALLET_ID="wallet_123456789"
POLICY="policy_123456789"
SIGNER="signer_123456789"
OWNER="owner_123456789"
NOW=1_800_000_000


def args(asset):
    return {
        "asset":asset,"stable":"USDT" if asset=="BNB" else "USDC",
        "wallet":WALLET,"owner_id":OWNER,"executor_id":SIGNER,
        "buy_source_atomic_cap":"1000000000",
        "sell_source_atomic_cap":"1000000000000000000",
        "native_wrap_atomic_cap":"1000000000000000000"
          if asset in ("BNB","POL","AVAX") else None,
        "expires_at":NOW+900,"now":NOW,
    }


def remote_policy(asset):
    expected=build_user_evm_policy_blueprint(**args(asset))
    remote={**deepcopy(expected),"id":POLICY}
    for i,rule in enumerate(remote["rules"]):
        rule["id"]="rule_1234567"+str(i)
    return expected,remote


def user():
    return {
        "id":DID,
        "linked_accounts":[{
            "type":"wallet","chain_type":"ethereum",
            "wallet_client_type":"privy",
            "id":WALLET_ID,"address":WALLET,
            "delegated":True,
        }],
    }


def metadata():
    return {
        "id":WALLET_ID,"chain_type":"ethereum",
        "address":WALLET,"entity":{"type":"user","id":DID},
        "additional_signers":[{
            "signer_id":SIGNER,
            "override_policy_ids":[POLICY],
        }],
    }


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI"])
def test_exact_remote_privy_rules_match_every_new_evm_coin(asset):
    expected,remote=remote_policy(asset)
    proven=verify_exact_remote_evm_policy(remote,expected)
    assert proven["remote_exact_match"] is True
    assert proven["policy_id"]==POLICY
    assert len(proven["policy_sha256"])==64
    assert proven["live_execution_enabled"] is False
    inputs=args(asset)
    inputs.pop("wallet")
    inputs.update(did=DID,user=user(),wallet_metadata=metadata(),
                  remote_policy=remote,policy_id=POLICY,signer_id=SIGNER)
    report=assess_owner_evm_privy_grant(**inputs)
    assert report["wallet_owned_by_user"] is True
    assert report["remote_policy_exactly_verified"] is True
    assert report["wallet_signer_binding_verified"] is True
    assert report["live_trading_enabled"] is False
    assert report["user_signed_consent_ledger_verified"] is False
    assert report["can_submit"] is False


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI"])
@pytest.mark.parametrize("mutate",[
    lambda r:r["rules"][0]["conditions"].pop(),
    lambda r:r["rules"][1]["conditions"][0].update(value="999"),
    lambda r:r["rules"].append(deepcopy(r["rules"][0])),
    lambda r:r.update(owner_id="attacker_1234567"),
    lambda r:r["rules"][0].update(action="DENY"),
])
def test_no_broadened_remote_evm_policy_may_be_verified(asset,mutate):
    expected,remote=remote_policy(asset)
    mutate(remote)
    with pytest.raises(ValueError):
        verify_exact_remote_evm_policy(remote,expected)


@pytest.mark.parametrize("tamper",[
    lambda m:m["additional_signers"][0].update(override_policy_ids=[]),
    lambda m:m["additional_signers"][0].update(
        override_policy_ids=[POLICY,"unrestricted_123456"]),
    lambda m:m["additional_signers"][0].update(signer_id="attacker_123456"),
    lambda m:m.update(entity={"type":"user","id":"did:privy:another"}),
    lambda m:m.update(entity={"type":"organization","id":DID}),
    lambda m:m.update(archived_at=1763660000000),
    lambda m:m.update(address="0x"+"cd"*20),
    lambda m:m.update(additional_signers=[]),
])
def test_reject_unrestricted_wrong_owner_or_missing_privy_signer(tamper):
    _,remote=remote_policy("LINK")
    m=metadata()
    tamper(m)
    kw=args("LINK")
    kw.pop("wallet")
    result=assess_owner_evm_privy_grant(
        did=DID,user=user(),wallet_metadata=m,
        remote_policy=remote,policy_id=POLICY,signer_id=SIGNER,**kw)
    assert result["wallet_signer_binding_verified"] is False
    assert result["live_trading_enabled"] is False


def test_no_http_endpoint_or_remote_privy_creation():
    import inspect
    from backend import shared_evm_privy_grant_review as mod
    code=inspect.getsource(mod)
    assert "client.post(" not in code
    assert "sign_privy" not in code
    from backend.app import app
    assert all("evm-grant-confirm" not in r.path for r in app.routes)
