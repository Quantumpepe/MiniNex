import pytest
from backend.ethereum_settlement_policy_migration import build_recurring_direct_policy
from backend.ethereum_settlement_plan import ETHEREUM_STABLES,CROSS_COIN_CONTRACTS

W="0x"+"12"*20
KW=dict(asset="POL",stable="USDT",wallet=W,owner_id="owner123",executor_id="signer123",
    buy_cap_micro="100000000",sell_cap_atomic="100000000000000000000",
    expires_at=2000,now=1000)

def test_recurring_rules_do_not_authorize_arbitrary_router_or_local_stable():
    draft=build_recurring_direct_policy(**KW)
    assert not draft["can_submit"]
    rules=draft["policy"]["rules"]
    assert len(rules)==5
    buy={c["field"]:c["value"] for c in rules[0]["conditions"]}
    assert buy["chain_id"]=="1"
    assert buy["startBridgeTokensViaAcrossV4._bridgeData.sendingAssetId"]==ETHEREUM_STABLES["USDT"]
    assert buy["startBridgeTokensViaAcrossV4._bridgeData.destinationChainId"]=="137"
    assert buy["startBridgeTokensViaAcrossV4._acrossData.refundAddress"]=="0x"+W[2:].rjust(64,"0")
    assert buy["startBridgeTokensViaAcrossV4._acrossData.receivingAssetId"]=="0x"+CROSS_COIN_CONTRACTS["POL"][2:].rjust(64,"0")
    for rule in rules:
        assert any(c["field"]=="function_name" for c in rule["conditions"])
        assert not any(c["value"]=="transfer" for c in rule["conditions"])
    assert build_recurring_direct_policy(**{**KW,"now":1100})["policy_sha256"]==draft["policy_sha256"]
    assert build_recurring_direct_policy(**{**KW,"buy_cap_micro":"100000001"})["policy_sha256"]!=draft["policy_sha256"]

def test_unavailable_chain_and_invalid_owner_scope_rejected():
    for change in ({"asset":"AVAX"},{"executor_id":"owner123"},{"buy_cap_micro":"0"}):
        with pytest.raises(ValueError):
            build_recurring_direct_policy(**{**KW,**change})
