"""No fee Privy policy may overwrite a live trading or other fee signer."""
import asyncio

import pytest
from fastapi import HTTPException

from backend import shared_evm_fee_policy_roles as roles


def install(monkeypatch, trading="trade_12345678", owner="owner_12345678"):
    monkeypatch.setattr(roles,"_require_policy_roles",lambda:(owner,trading))
    for asset in ("BNB","POL","AVAX","LINK","UNI"):
        monkeypatch.setenv(
            "MININEX_PRIVY_EVM_"+asset+"_FEE_SIGNER_ID",
            "fee_"+asset.lower()+"_12345678")


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI"])
def test_five_assets_have_distinct_and_valid_original_fee_quorums(monkeypatch,asset):
    install(monkeypatch)
    owner,signer=roles.require_fee_policy_roles(asset)
    assert owner=="owner_12345678"
    assert signer=="fee_"+asset.lower()+"_12345678"
    assert signer!="trade_12345678"


@pytest.mark.parametrize("bad",["","trade_12345678","owner_12345678",
                                 "bad%quorum","id"])
def test_fee_policy_refuses_trading_signer_reuse_or_unconfigured_quorum(
    monkeypatch,bad,
):
    install(monkeypatch)
    monkeypatch.setenv("MININEX_PRIVY_EVM_LINK_FEE_SIGNER_ID",bad)
    with pytest.raises(HTTPException) as exc:
        roles.require_fee_policy_roles("LINK")
    assert exc.value.status_code==409


def test_fee_quorum_reuse_across_coins_is_not_permitted(monkeypatch):
    install(monkeypatch)
    monkeypatch.setenv("MININEX_PRIVY_EVM_UNI_FEE_SIGNER_ID",
                       "fee_link_12345678")
    with pytest.raises(HTTPException) as exc:
        roles.require_fee_policy_roles("LINK")
    assert exc.value.status_code==409


def test_fresh_fee_quorum_remote_get_never_checks_trading_quorum(monkeypatch):
    install(monkeypatch)
    checked=[]
    async def fake_check(*,expected_quorum_id):
        checked.append(expected_quorum_id)
        return {"remote_quorum_public_key_verified":True}
    monkeypatch.setattr(roles,"check_privy_backend_quorum",fake_check)
    outcome=asyncio.run(roles.check_privy_fee_backend_quorum("BNB"))
    assert checked==["fee_bnb_12345678"]
    assert outcome["remote_quorum_public_key_verified"] is True


def test_fee_enrollment_does_not_use_eth_trading_quorum_helper():
    from backend import shared_evm_fee_auto_consent as consent
    from backend import shared_evm_fee_policy_publish as publication
    assert consent._require_policy_roles is roles.require_fee_policy_roles
    assert publication._require_policy_roles is roles.require_fee_policy_roles


def test_bad_asset_cannot_query_another_quorum(monkeypatch):
    install(monkeypatch)
    with pytest.raises(ValueError):
        roles.require_fee_policy_roles("ETH")


def test_no_fee_quorum_can_reuse_a_distinct_coin_trading_signer(monkeypatch):
    install(monkeypatch)
    monkeypatch.setenv("MININEX_PRIVY_EVM_BNB_TRADE_SIGNER_ID",
                       "trade_bnb_12345678")
    monkeypatch.setenv("MININEX_PRIVY_EVM_POL_TRADE_SIGNER_ID",
                       "trade_pol_12345678")
    monkeypatch.setenv("MININEX_PRIVY_EVM_UNI_FEE_SIGNER_ID",
                       "trade_bnb_12345678")
    with pytest.raises(HTTPException) as err:
        roles.require_fee_policy_roles("UNI")
    assert err.value.status_code==409
