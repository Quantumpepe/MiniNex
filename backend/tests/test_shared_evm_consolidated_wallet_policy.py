"""Central Privy signer proposal: offline, no wallet or live side effects."""
from copy import deepcopy

import pytest

from backend.shared_evm_consolidated_wallet_policy import (
    compose_scoped_evm_wallet_policy, verified_wallet_policy_digest,
)

WALLET = "0x" + "a" * 40
TREASURY = "0x" + "b" * 40
OWNER = "owner_123456"
SIGNER = "signer_987654"
NOW = 1_800_000_000


def scope(asset, stable="USDC", native=False):
    return {
        "asset": asset, "stable": stable, "treasury": TREASURY,
        "buy_source_atomic_cap": "100000000",
        "sell_source_atomic_cap": "1000000000000000000",
        "native_wrap_atomic_cap": "1000000000000000000" if native else None,
        "owner_max_fee_atomic": "5000000",
        "expires_at": NOW + 3600,
    }


def kwargs(scopes):
    return dict(
        wallet=WALLET, owner_id=OWNER, executor_id=SIGNER,
        scopes=scopes, now=NOW,
    )


def test_one_wallet_one_quorum_policy_merges_exact_active_coin_rules():
    data = kwargs([scope("LINK"), scope("BNB", "USDT", native=True)])
    candidate = compose_scoped_evm_wallet_policy(**data)
    assert candidate["owner_id"] == OWNER
    assert candidate["chain_type"] == "ethereum"
    assert candidate["version"] == "1.0"
    assert len(candidate["rules"]) == 12
    names = [r["name"] for r in candidate["rules"]]
    assert names[0].startswith("MiniNex BNB ")
    assert names[7].startswith("MiniNex LINK ")
    assert len(names) == len(set(names))
    assert all(r["method"] == "eth_sendTransaction" for r in candidate["rules"])
    assert all(r["action"] == "ALLOW" for r in candidate["rules"])
    assert not any(r["method"] == "*" for r in candidate["rules"])
    assert all(r["conditions"] for r in candidate["rules"])
    assert len(verified_wallet_policy_digest(candidate, **data)) == 64
    assert candidate == compose_scoped_evm_wallet_policy(
        **kwargs(list(reversed(data["scopes"])))
    )


def test_unapproved_or_duplicate_coin_scope_rejected():
    valid = scope("LINK")
    for scopes in ([], [valid, valid], [scope("BTC")],
                   [scope("XRP")], [dict(valid, any_unverified_key=True)]):
        with pytest.raises(ValueError):
            compose_scoped_evm_wallet_policy(**kwargs(scopes))


def test_modified_fees_rules_or_budget_cannot_be_verified():
    data = kwargs([scope("UNI")])
    original = compose_scoped_evm_wallet_policy(**data)
    for change in (
        lambda p: p["rules"][0].update(method="*"),
        lambda p: p["rules"][-1].update(conditions=[]),
        lambda p: p.update(owner_id="another_owner"),
        lambda p: p["rules"].append(deepcopy(p["rules"][0])),
    ):
        copy = deepcopy(original)
        change(copy)
        with pytest.raises(ValueError):
            verified_wallet_policy_digest(copy, **data)


def test_missing_or_invalid_explicit_budget_cannot_broaden_privy_permissions():
    for candidate in ("0", "-1", "not-a-number", None):
        bad = scope("ARB")
        bad["buy_source_atomic_cap"] = candidate
        with pytest.raises((ValueError, TypeError)):
            compose_scoped_evm_wallet_policy(**kwargs([bad]))


@pytest.mark.parametrize("asset",["BNB","POL","ARB"])
@pytest.mark.parametrize("stable",["USDC","USDT"])
def test_versioned_direct_bridge_scope_coexists_with_unchanged_ethereum_scope(asset,stable):
    from backend.shared_evm_consolidated_wallet_policy import DIRECT_SETTLEMENT_POLICY
    old=scope("LINK")
    cross=dict(scope(asset,stable),settlement_policy=DIRECT_SETTLEMENT_POLICY,
               owner_max_fee_atomic="0")
    candidate=compose_scoped_evm_wallet_policy(**kwargs([old,cross]))
    legacy=compose_scoped_evm_wallet_policy(**kwargs([old]))
    assert [r for r in candidate["rules"] if r["name"].startswith("MiniNex LINK ")]==legacy["rules"]
    bridge=[r for r in candidate["rules"] if r["name"].startswith("MiniNex "+asset+" ")]
    assert len(bridge)==4
    assert not any(r["name"].endswith(" native") for r in bridge)
    assert not any("fee" in r["name"] for r in bridge)
    assert verified_wallet_policy_digest(candidate,**kwargs([old,cross]))


def test_only_one_legacy_coin_can_migrate_without_changing_sibling_or_budget():
    from backend.shared_evm_consolidated_wallet_policy import changed_wallet_coin,DIRECT_SETTLEMENT_POLICY
    original={"LINK":scope("LINK"),"POL":scope("POL",native=True)}
    migrated=dict(original["POL"],settlement_policy=DIRECT_SETTLEMENT_POLICY,
                  native_wrap_atomic_cap=None,owner_max_fee_atomic="0")
    target={**original,"POL":migrated}
    revisions={"LINK":2,"POL":3}
    assert changed_wallet_coin(original,target,revisions,revisions)=="POL"
    for bad in ({**target,"LINK":scope("LINK","USDT")},
                {"POL":migrated},
                {**target,"POL":dict(migrated,stable="USDT")},
                {**target,"POL":dict(migrated,owner_max_fee_atomic="1")}):
        with pytest.raises(ValueError):
            changed_wallet_coin(original,bad,revisions,revisions)
    with pytest.raises(ValueError):
        changed_wallet_coin(original,target,revisions,{"LINK":2,"POL":4})

@pytest.mark.parametrize("asset,decimals",[("BTC",8),("XRP",18)])
@pytest.mark.parametrize("stable",["USDC","USDT"])
def test_wrapped_trade_scope_is_distinct_and_exact(asset,decimals,stable):
    from backend.ethereum_wrapped_spot_policy import (
        WRAPPED_SPOT_POLICY, wrapped_spot_route, wrapped_spot_transaction,
        inspect_wrapped_spot_transaction,
    )
    selected=dict(scope(asset,stable),settlement_policy=WRAPPED_SPOT_POLICY,
                  native_wrap_atomic_cap=None,owner_max_fee_atomic="0")
    data=kwargs([scope("LINK"),selected])
    policy=compose_scoped_evm_wallet_policy(**data)
    assert verified_wallet_policy_digest(policy,**data)
    for side in ("BUY","SELL"):
        route=wrapped_spot_route(asset,stable,side)
        assert route["chain_id"]==1
        assert route["source_decimals"]==(6 if side=="BUY" else decimals)
        rule=next(r for r in policy["rules"] if r["name"]==f"MiniNex {asset} {side} {stable} wrapped exactInput")
        assert any(c["field"]=="exactInput.params.path" and c["value"]==route["path"] for c in rule["conditions"])
        tx=wrapped_spot_transaction(asset=asset,stable=stable,side=side,wallet=WALLET,
                                   amount_atomic=100000,min_out_atomic=42)
        assert inspect_wrapped_spot_transaction(tx,asset=asset,stable=stable,side=side,
                                   wallet=WALLET,amount_atomic=100000,min_out_atomic=42)==tx
        with pytest.raises(ValueError):
            inspect_wrapped_spot_transaction(dict(tx,value="1"),asset=asset,stable=stable,
                 side=side,wallet=WALLET,amount_atomic=100000,min_out_atomic=42)


def test_wrapped_scope_uses_btc_owner_percent_and_distinct_token_precision(monkeypatch):
    from decimal import Decimal
    from backend import shared_evm_wallet_policy_prepare as prepare
    from backend.ethereum_wrapped_spot_policy import WRAPPED_SPOT_POLICY
    monkeypatch.setattr(prepare,"owner_fee_treasury",lambda **kw:TREASURY)
    monkeypatch.setattr(prepare,"_price",lambda snapshot,asset,now:
                        {"BTC":Decimal("100000"),"XRP":Decimal("2"),"USDC":Decimal("1")}[asset])
    prices={"_mininex_verified_btc_wrapped_capital_usd":Decimal("2000")}
    btc=prepare.build_selected_wallet_scope(asset="BTC",stable="USDC",amount=50,snapshot=prices,now=NOW)
    assert btc["settlement_policy"]==WRAPPED_SPOT_POLICY
    assert btc["buy_source_atomic_cap"]=="1000000000"
    assert btc["sell_source_atomic_cap"]=="1000000"
    xrp=prepare.build_selected_wallet_scope(asset="XRP",stable="USDC",amount=1000,snapshot=prices,now=NOW)
    assert xrp["sell_source_atomic_cap"]=="500000000000000000000"
    with pytest.raises(ValueError):
        prepare.build_selected_wallet_scope(asset="BTC",stable="USDC",amount=50,snapshot={},now=NOW)
