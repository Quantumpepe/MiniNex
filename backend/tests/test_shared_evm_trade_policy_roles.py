"""Per-coin EVM trading grant must not erase ETH or another coin."""
import asyncio

import pytest
from fastapi import HTTPException

from backend import shared_evm_trade_policy_roles as roles

ASSETS=["BNB","POL","AVAX","LINK","UNI"]


def setup(monkeypatch):
    monkeypatch.setattr(roles,"_require_policy_roles",
                        lambda:("owner_12345678","eth_signer_12345"))
    for coin in ASSETS:
        monkeypatch.setenv(
            f"MININEX_PRIVY_EVM_{coin}_TRADE_SIGNER_ID",
            "trade_"+coin.lower()+"_12345")
        monkeypatch.setenv(
            f"MININEX_PRIVY_EVM_{coin}_FEE_SIGNER_ID",
            "fee_"+coin.lower()+"_12345")


@pytest.mark.parametrize("asset",ASSETS)
def test_each_coin_uses_separately_pinned_trade_key_quorum(monkeypatch,asset):
    setup(monkeypatch)
    owner,signer=roles.require_coin_trade_policy_roles(asset)
    assert owner=="owner_12345678"
    assert signer=="trade_"+asset.lower()+"_12345"
    assert signer!="eth_signer_12345"


@pytest.mark.parametrize("conflict",[
    "", "eth_signer_12345", "owner_12345678",
    "trade_uni_12345", "fee_link_12345", "bad%id",
])
def test_trade_signer_reuse_or_missing_quorum_fails_closed(monkeypatch,conflict):
    setup(monkeypatch)
    monkeypatch.setenv("MININEX_PRIVY_EVM_LINK_TRADE_SIGNER_ID",conflict)
    with pytest.raises(HTTPException) as e:
        roles.require_coin_trade_policy_roles("LINK")
    assert e.value.status_code==409


def test_original_trade_policy_uses_exact_asset_signer_and_remote_get(monkeypatch):
    setup(monkeypatch)
    seen=[]
    async def check(*,expected_quorum_id):
        seen.append(expected_quorum_id)
        return {"remote_quorum_public_key_verified":True}
    monkeypatch.setattr(roles,"check_privy_backend_quorum",check)
    result=asyncio.run(roles.check_coin_trade_backend_quorum("POL"))
    assert result["remote_quorum_public_key_verified"] is True
    assert seen==["trade_pol_12345"]


def test_no_trading_policy_can_be_approved_on_wrong_chain_asset(monkeypatch):
    setup(monkeypatch)
    with pytest.raises(ValueError):
        roles.require_coin_trade_policy_roles("ETH")


def test_enrollment_and_auto_grant_audit_use_original_asset_quorum():
    from backend import shared_evm_owner_consent as consent
    from backend import shared_evm_policy_candidate_stage as candidate
    from backend import shared_evm_privy_policy_publish as publish
    from backend import shared_evm_remote_grant_audit as audit
    from backend import shared_evm_live_readiness as readiness
    assert consent._require_policy_roles is roles.require_coin_trade_policy_roles
    assert candidate._require_policy_roles is roles.require_coin_trade_policy_roles
    assert publish._require_policy_roles is roles.require_coin_trade_policy_roles
    assert audit.check_privy_backend_quorum is roles.check_coin_trade_backend_quorum
    assert readiness.check_privy_backend_quorum is roles.check_coin_trade_backend_quorum
