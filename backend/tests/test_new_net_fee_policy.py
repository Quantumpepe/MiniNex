from backend.shared_evm_consolidated_wallet_policy import compose_scoped_evm_wallet_policy,with_net_fee_scope,valid_scope_fields
from backend.ethereum_settlement_plan import ETHEREUM_STABLES
from backend.shared_evm_fee_privy_policy_draft import build_owner_evm_fee_policy_draft

def test_new_central_fee_rule_is_ethereum_and_explicit(monkeypatch):
    wallet="0x"+"a"*40;treasury="0x"+"b"*40
    monkeypatch.setenv("MININEX_TREASURY_WALLET",treasury)
    scope=dict(asset="LINK",stable="USDC",treasury=treasury,buy_source_atomic_cap="100000000",
       sell_source_atomic_cap="1000000000000000000",native_wrap_atomic_cap=None,
       owner_max_fee_atomic="100000000",expires_at=1800000000)
    args=dict(wallet=wallet,owner_id="owner_123",executor_id="signer_123",now=1799999900)
    old=compose_scoped_evm_wallet_policy(scopes=[scope],**args)
    upgraded=with_net_fee_scope(scope)
    assert valid_scope_fields(upgraded)
    new=compose_scoped_evm_wallet_policy(scopes=[upgraded],**args)
    assert len(old["rules"])==len(new["rules"])
    fee=new["rules"][-1]
    assert fee["name"]=="MiniNex LINK USDC realized net fee Ethereum"
    assert any(x["field"]=="chain_id" and str(x["value"])=="1" for x in fee["conditions"])
    assert any(x["field"]=="transfer.to" and x["value"]==treasury for x in fee["conditions"])
    assert any(x["field"]=="to" and x["value"].lower()==ETHEREUM_STABLES["USDC"] for x in fee["conditions"])
    assert not valid_scope_fields({**upgraded,"net_fee_atomic_cap":"100000001"})
