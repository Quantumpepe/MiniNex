from eth_abi import encode
import pytest
from backend.ethereum_settlement_calldata import ACROSS,BRIDGE,DIRECT,DIAMOND,route_digest
from backend.ethereum_settlement_policy_migration import exact_direct_bridge_rules,migration_review
from backend.live_eth_quotes import STABLES

W="0x"+"12"*20
T="0x912ce59144191c1204e64559fe8253a0e49e6548"
def word(a):return bytes.fromhex(a[2:].rjust(64,"0"))
def quote():
    stable=STABLES["USDC"].lower()
    b=(bytes.fromhex("ab"*32),"across","mininex",W,stable,W,1000000,42161,False,False)
    a=(word(W),word(W),word(stable),word(T),500000000000000000,0,bytes(32),100,1000,0,b"")
    tx=dict(chainId=1,**{"from":W},to=DIAMOND,value="0",data="0x"+(DIRECT+encode([BRIDGE,ACROSS],[b,a])).hex())
    return dict(asset="ARB",side="BUY",stable="USDC",wallet_address=W,source_chain_id=1,
        destination_chain_id=42161,source_token=stable,destination_token=T,input_atomic="1000000",
        minimum_output_atomic="500000000000000000",issued_at=100,expires_at=130,
        unsigned_transaction=tx,route_sha256=route_digest(tx,1))
def test_draft_has_exact_owner_refund_output_and_no_auto_authority():
    q=quote()
    draft=exact_direct_bridge_rules(q,now=101)
    assert len(draft["rules"])==2
    fields={x["field"]:x["value"] for x in draft["rules"][0]["conditions"]}
    assert fields["chain_id"]=="1"
    assert fields["startBridgeTokensViaAcrossV4._acrossData.refundAddress"]=="0x"+word(W).hex()
    assert fields["startBridgeTokensViaAcrossV4._acrossData.message"]=="0x"
    assert not draft["recurring_auto_authority"] and not draft["can_submit"]
    review=migration_review(q,dict(asset="ARB",stable="USDC",wallet_address=W,
        policy_sha256="ab"*32,budget_revision=1),now=101)
    assert review["requires_new_owner_consent"] and not review["remote_policy_verified"]
    with pytest.raises(ValueError):exact_direct_bridge_rules(q,now=131)
    with pytest.raises(ValueError):exact_direct_bridge_rules({**q,"route_sha256":"00"*32},now=101)
