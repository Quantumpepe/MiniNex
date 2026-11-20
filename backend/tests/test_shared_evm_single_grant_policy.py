"""Single-grant EVM policy must never broaden token spending authority."""
import copy
import pytest

from backend.shared_evm_single_grant_policy import build_single_grant_evm_policy
from backend.shared_evm_swap_receipts import _pair

WALLET="0x"+"ab"*20
TREASURY="0x"+"cd"*20

@pytest.mark.parametrize("asset,stable", [
    ("BNB","USDT"),("POL","USDC"),("AVAX","USDC"),
    ("LINK","USDC"),("UNI","USDT"),("ARB","USDC"),
])
def test_same_owner_grant_contains_finite_router_approvals_and_fee(asset,stable):
    native=asset in ("BNB","POL","AVAX")
    args=dict(
        asset=asset,stable=stable,wallet=WALLET,treasury=TREASURY,
        owner_id="owner_independent",executor_id="signer_separate",
        buy_source_atomic_cap="123456",sell_source_atomic_cap="987654",
        native_wrap_atomic_cap="999999" if native else None,
        owner_max_fee_atomic="1000",expires_at=2000000000,now=1999990000,
    )
    policy=build_single_grant_evm_policy(**args)
    assert len(policy["rules"])==(7 if native else 5)
    assert all(rule["action"]=="ALLOW" for rule in policy["rules"])
    approvals=[rule for rule in policy["rules"] if "allowance" in rule["name"]]
    assert len(approvals)==2
    for side,rule in zip(("BUY","SELL"),approvals):
        pair=_pair(asset,stable,side)
        fields={(c["field_source"],c["field"],c["operator"]):
                c["value"] for c in rule["conditions"]}
        assert fields["ethereum_transaction","to","eq"]==pair["source_token"]
        assert fields["ethereum_transaction","chain_id","eq"]==str(pair["chain_id"])
        assert fields["ethereum_calldata","approve.spender","eq"]==pair["router"]
        assert fields["ethereum_calldata","approve.value","lte"]==(
            "123456" if side=="BUY" else "987654")
        assert fields["ethereum_transaction","value","eq"]=="0"
    fee=policy["rules"][-1]
    fee_fields={(c["field"],c["operator"]):c["value"]
                for c in fee["conditions"]}
    assert fee_fields["transfer.to","eq"]==TREASURY
    assert fee_fields["transfer.value","lte"]=="1000"
    assert all("approve" not in r["name"] for r in policy["rules"][:2])


def test_single_grant_rejects_unbounded_fee_and_invalid_wallet():
    base=dict(
        asset="LINK",stable="USDC",wallet=WALLET,treasury=TREASURY,
        owner_id="owner_independent",executor_id="signer_separate",
        buy_source_atomic_cap="123",sell_source_atomic_cap="456",
        native_wrap_atomic_cap=None,owner_max_fee_atomic="100",
        expires_at=2000000000,now=1999990000,
    )
    for field,value in [
        ("owner_max_fee_atomic",str(2**256-1)),
        ("owner_max_fee_atomic","0"),
        ("wallet","not-an-address"),
        ("treasury",WALLET),
    ]:
        bad=copy.deepcopy(base)
        bad[field]=value
        with pytest.raises(ValueError):
            build_single_grant_evm_policy(**bad)



@pytest.mark.parametrize("asset,stable",[
    ("BNB","USDT"),("POL","USDC"),("AVAX","USDC"),
    ("LINK","USDC"),("UNI","USDT"),("ARB","USDC"),
])
def test_final_router_approval_must_be_within_actual_combined_privy_grant(
    asset,stable,monkeypatch,
):
    from datetime import datetime,timezone
    from backend.shared_evm_allowance import inspect_combined_grant_router_approval
    from backend.shared_evm_single_grant_policy import build_single_grant_evm_policy
    monkeypatch.setattr(
        "backend.shared_evm_remote_grant_audit.owner_fee_treasury",
        lambda **kwargs:TREASURY,
    )
    now=1999990000
    policy=build_single_grant_evm_policy(
        asset=asset,stable=stable,wallet=WALLET,treasury=TREASURY,
        owner_id="owner_independent",executor_id="signer_separate",
        buy_source_atomic_cap="123456",sell_source_atomic_cap="987654",
        native_wrap_atomic_cap=("999999" if asset in ("BNB","POL","AVAX")
                                else None),
        owner_max_fee_atomic="1000",expires_at=2000000000,now=now,
    )
    import hashlib,json
    record={
        "asset":asset,"stable":stable,"wallet_address":WALLET,
        "owner_id":"owner_independent","signer_id":"signer_separate",
        "authorization_method":"PRIVY_SCOPED_GRANT","status":"GRANT_VERIFIED",
        "revoked_at":None,"candidate_json":policy,
        "policy_sha256":hashlib.sha256(json.dumps(
            policy,sort_keys=True,separators=(",",":"),
        ).encode()).hexdigest(),
        "expires_at":datetime.fromtimestamp(2000000000,timezone.utc),
    }
    approved=inspect_combined_grant_router_approval(
        record=record,asset=asset,side="BUY",stable=stable,
        amount_atomic=123456,now=now)
    assert approved["transaction"]["data"].endswith(f"{123456:064x}")
    assert approved["transaction_submitted"] is False
    for changes in (
        {"amount_atomic":123457},
        {"amount_atomic":2**256-1},
    ):
        with pytest.raises(ValueError):
            inspect_combined_grant_router_approval(
                record=record,asset=asset,side="BUY",stable=stable,
                now=now,**changes)
    with pytest.raises(ValueError):
        inspect_combined_grant_router_approval(
            record={**record,"authorization_method":"EIP191_AND_PRIVY"},
            asset=asset,side="BUY",stable=stable,
            amount_atomic=123456,now=now)
