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


def test_direct_sender_binds_original_policy_scope_and_refund():
    from eth_abi import encode
    from backend.ethereum_settlement_auto_submit import check_direct_bridge_scope
    from backend.ethereum_settlement_calldata import ACROSS,BRIDGE,DIRECT,DIAMOND,route_digest
    from backend.shared_evm_consolidated_wallet_policy import compose_scoped_evm_wallet_policy,DIRECT_SETTLEMENT_POLICY
    asset="POL";stable="USDT";source=ETHEREUM_STABLES[stable];dest=CROSS_COIN_CONTRACTS[asset]
    scope=dict(asset=asset,stable=stable,treasury="0x"+"ab"*20,buy_source_atomic_cap="100000000",
        sell_source_atomic_cap="100000000000000000000",native_wrap_atomic_cap=None,
        owner_max_fee_atomic="0",expires_at=2000,settlement_policy=DIRECT_SETTLEMENT_POLICY)
    policy=compose_scoped_evm_wallet_policy(wallet=W,owner_id="owner123",executor_id="signer123",
        scopes=[scope],now=1000)
    rec=dict(asset=asset,stable=stable,scope=scope,authorization_method="PRIVY_CENTRAL_ONE_SIGNER",
        status="GRANT_VERIFIED",revoked_at=None,wallet_address=W,wallet_id="wallet123",candidate_json=policy)
    word=lambda a:bytes.fromhex(a[2:].rjust(64,"0"))
    bridge=(bytes.fromhex("ab"*32),"across","mininex",W,source,W,1000000,137,False,False)
    across=(word(W),word(W),word(source),word(dest),10**18,0,bytes(32),1000,2000,0,b"")
    tx={"from":W,"to":DIAMOND,"chainId":1,"value":"0","data":"0x"+(DIRECT+encode([BRIDGE,ACROSS],[bridge,across])).hex()}
    quote=dict(asset=asset,side="BUY",stable=stable,wallet_address=W,source_token=source,destination_token=dest,
        source_chain_id=1,destination_chain_id=137,input_atomic="1000000",minimum_output_atomic=str(10**18),
        issued_at=1000,expires_at=1030,unsigned_transaction=tx,route_sha256=route_digest(tx,1))
    url,body=check_direct_bridge_scope(quote,rec,now=1001)
    assert url.endswith("/wallet123/rpc")
    assert body["caip2"]=="eip155:1"
    assert body["params"]["transaction"]["data"]==tx["data"]
    from backend.ethereum_settlement_auto_submit import check_direct_bridge_approval
    plan=dict(verified_source_allowance_atomic="1",verified_source_balance_atomic="1000000",
        gas_estimate_kind="APPROVAL_ONLY",route_sha256=quote["route_sha256"],quote_id="quote",
        manual_approval={"from":W,"to":source,"data":"0x095ea7b3"+DIAMOND[2:].rjust(64,"0")+"0"*64,"value":"0x0"})
    quote["quote_id"]="quote"
    _,approval,mode=check_direct_bridge_approval(quote,rec,plan,now=1001)
    assert mode=="BRIDGE_USDT_ZERO"
    assert approval["caip2"]=="eip155:1"
    from fastapi import HTTPException
    for bad in (dict(rec,wallet_address="0x"+"34"*20),
                dict(rec,scope={**scope,"buy_source_atomic_cap":"1"}),
                dict(rec,scope={**scope,"settlement_policy":None})):
        with pytest.raises(HTTPException):
            check_direct_bridge_scope(quote,bad,now=1001)
