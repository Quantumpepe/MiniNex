"""Fee grant must not overwrite the trading Privy key-quorum policy."""
import asyncio

import pytest
from fastapi import HTTPException

from backend import shared_evm_fee_policy_roles as roles


def install(monkeypatch, trading="trade_12345678", owner="owner_12345678"):
    monkeypatch.setattr(roles,"_require_policy_roles",lambda:(owner,trading))


def test_distinct_fee_quorum_uses_configured_original_owner(monkeypatch):
    install(monkeypatch)
    monkeypatch.setenv("MININEX_PRIVY_EVM_FEE_SIGNER_ID","fee_12345678")
    assert roles.require_fee_policy_roles()==(
        "owner_12345678","fee_12345678")


@pytest.mark.parametrize("bad",[
    "", "trade_12345678", "owner_12345678",
    "bad%quorum", "id",
])
def test_fee_policy_refuses_trading_signer_reuse_or_unconfigured_quorum(
    monkeypatch,bad,
):
    install(monkeypatch)
    monkeypatch.setenv("MININEX_PRIVY_EVM_FEE_SIGNER_ID",bad)
    with pytest.raises(HTTPException) as exc:
        roles.require_fee_policy_roles()
    assert exc.value.status_code==409


def test_fresh_fee_quorum_remote_get_never_checks_trading_quorum(monkeypatch):
    install(monkeypatch)
    monkeypatch.setenv("MININEX_PRIVY_EVM_FEE_SIGNER_ID","fee_12345678")
    checked=[]
    async def fake_check(*,expected_quorum_id):
        checked.append(expected_quorum_id)
        return {"remote_quorum_public_key_verified":True}
    monkeypatch.setattr(roles,"check_privy_backend_quorum",fake_check)
    outcome=asyncio.run(roles.check_privy_fee_backend_quorum())
    assert checked==["fee_12345678"]
    assert outcome["remote_quorum_public_key_verified"] is True


def test_fee_enrollment_does_not_use_eth_trading_quorum_helper():
    from backend import shared_evm_fee_auto_consent as consent
    from backend import shared_evm_fee_policy_publish as publication
    assert consent._require_policy_roles is roles.require_fee_policy_roles
    assert publication._require_policy_roles is roles.require_fee_policy_roles
