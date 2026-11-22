"""One restricted wallet-owned Privy policy for every additional EVM coin."""
import copy
import pytest
from backend.shared_evm_privy_user_policy import (
    build_user_evm_policy_blueprint,inspect_user_evm_policy_blueprint,
)
from backend.shared_evm_swap_receipts import _pair

WALLET="0x"+"ab"*20
OWNER="owner_independent"
EXECUTOR="signer_separate"

def params(asset,stable):
    return dict(
        asset=asset,stable=stable,wallet=WALLET,
        owner_id=OWNER,executor_id=EXECUTOR,
        buy_source_atomic_cap="123000000000000000",
        sell_source_atomic_cap="1000000000000000000",
        native_wrap_atomic_cap=(
            "1000000000000000000" if asset in ("BNB","POL","AVAX") else None),
        expires_at=2000000000,now=1999990000,
    )


@pytest.mark.parametrize("asset,stable,chain,rule_count",[
    ("ETH","USDC",1,4),("BNB","USDT",56,4),("POL","USDC",137,4),
    ("AVAX","USDC",43114,4),("LINK","USDC",1,2),("QNT","USDC",1,2),
    ("UNI","USDT",1,2),
])
def test_all_five_assets_share_exact_privy_chain_bound_rules(
    asset,stable,chain,rule_count
):
    args=params(asset,stable)
    p=build_user_evm_policy_blueprint(**args)
    assert p["chain_type"]=="ethereum"
    assert len(p["rules"])==rule_count
    assert all(x["method"]=="eth_sendTransaction" for x in p["rules"])
    for side,rule in zip(("BUY","SELL"),p["rules"][:2]):
        pair=_pair(asset,stable,side)
        checks={(c["field_source"],c["field"],c["operator"]):
                c["value"] for c in rule["conditions"]}
        assert checks["ethereum_transaction","chain_id","eq"]==str(chain)
        assert checks["ethereum_transaction","to","eq"]==pair["router"]
        assert checks["ethereum_calldata","exactInputSingle.params.tokenIn","eq"]==pair["source_token"]
        assert checks["ethereum_calldata","exactInputSingle.params.tokenOut","eq"]==pair["output_token"]
        assert checks["ethereum_calldata","exactInputSingle.params.recipient","eq"]==WALLET
        assert checks["ethereum_transaction","value","eq"]=="0"
        assert checks["ethereum_calldata","exactInputSingle.params.amountOutMinimum","gt"]=="0"
        assert "exactInputSingle.params.amountIn" in {
            c["field"] for c in rule["conditions"]
        }
    inspected=inspect_user_evm_policy_blueprint(p,**args)
    assert inspected["policy_draft_matches_expected"] is True
    assert inspected["wallet_bound"] is True
    assert inspected["auto_execution_enabled"] is False
    assert inspected["policy_created_in_privy"] is False
    assert len(inspected["draft_sha256"])==64


@pytest.mark.parametrize("asset,stable",[
    ("ETH","USDC"),("BNB","USDT"),("POL","USDC"),("AVAX","USDC"),
    ("LINK","USDC"),("QNT","USDC"),("UNI","USDT"),
])
@pytest.mark.parametrize("tamper",[
    lambda p:p["rules"][0]["conditions"].pop(),
    lambda p:p["rules"][1]["conditions"][0].update(value="999"),
    lambda p:p["rules"][0]["conditions"][1].update(value="0x"+"cd"*20),
    lambda p:p["rules"].append({"action":"ALLOW","method":"eth_sendTransaction","conditions":[]}),
])
def test_privy_blueprint_rejects_missing_replaced_or_broad_allow_rules(
    asset,stable,tamper
):
    args=params(asset,stable)
    p=build_user_evm_policy_blueprint(**args)
    tamper(p)
    assert inspect_user_evm_policy_blueprint(p,**args)[
        "policy_draft_matches_expected"] is False


@pytest.mark.parametrize("asset,stable",[
    ("BTC","USDC"),
    ("LINK","DAI"),("AVAX","USDT"),
    ("BNB","USDC"),
])
def test_unapproved_asset_or_settlement_never_reaches_privy_policy(asset,stable):
    with pytest.raises(ValueError):
        build_user_evm_policy_blueprint(**params(asset,stable))


def test_no_asset_specific_double_systems_and_no_remote_grant():
    import inspect
    from backend import shared_evm_privy_user_policy as mod
    text=inspect.getsource(mod)
    assert "AsyncClient" not in text
    assert "privy.io/v1" not in text
    assert "sign_privy" not in text


@pytest.mark.parametrize("mutation", [
    lambda tx: tx.update(chainId=1),
    lambda tx: tx.update(to="0x" + "cd"*20),
    lambda tx: tx.update(value="0x1"),
    lambda tx: tx.update(data=tx["data"][:10] + "00"*32 + tx["data"][-64:]),
    lambda tx: tx.update(data=tx["data"][:-64] + "f"*64),
    lambda tx: tx.update(from_="other"),
])
def test_router_approval_exact_scope_rejects_mutation(mutation):
    from backend.shared_evm_allowance import (
        owner_approval_draft, verify_router_approval_transaction,
    )
    token="0x"+"ab"*20
    router="0x"+"bc"*20
    wallet="0x"+"cd"*20
    args=dict(token=token,router=router,wallet=wallet,chain_id=56,
              amount_atomic=123456)
    draft=owner_approval_draft(**args)
    assert verify_router_approval_transaction(draft,**args)
    bad=copy.deepcopy(draft)
    mutation(bad)
    assert not verify_router_approval_transaction(bad,**args)


def test_router_approval_rejects_infinite_amount_and_extra_fields():
    from backend.shared_evm_allowance import (
        owner_approval_draft, verify_router_approval_transaction,
    )
    args=dict(token="0x"+"ab"*20,router="0x"+"bc"*20,
              wallet="0x"+"cd"*20,chain_id=1,amount_atomic=123)
    approval=owner_approval_draft(**args)
    assert not verify_router_approval_transaction(
        {**approval,"data":approval["data"][:-64]+"f"*64},**args)
    assert not verify_router_approval_transaction({**approval,"gas":"0x0"},**args)
