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

@pytest.mark.parametrize("asset,decimals",[("BTC",8),("XRP",18),("SOL",9)])
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


def test_wrapped_scope_uses_btc_exact_saved_capital_and_distinct_token_precision(monkeypatch):
    from decimal import Decimal
    from backend import shared_evm_wallet_policy_prepare as prepare
    from backend.ethereum_wrapped_spot_policy import WRAPPED_SPOT_POLICY
    monkeypatch.setattr(prepare,"owner_fee_treasury",lambda **kw:TREASURY)
    monkeypatch.setattr(prepare,"_price",lambda snapshot,asset,now:
                        {"BTC":Decimal("100000"),"XRP":Decimal("2"),"USDC":Decimal("1")}[asset])
    prices={}
    btc=prepare.build_selected_wallet_scope(asset="BTC",stable="USDC",amount=Decimal("1000"),snapshot=prices,now=NOW)
    assert btc["settlement_policy"]==WRAPPED_SPOT_POLICY
    assert btc["buy_source_atomic_cap"]=="1000000000"
    assert btc["sell_source_atomic_cap"]=="1000000"
    xrp=prepare.build_selected_wallet_scope(asset="XRP",stable="USDC",amount=1000,snapshot=prices,now=NOW)
    assert xrp["sell_source_atomic_cap"]=="500000000000000000000"
    with pytest.raises(ValueError):
        prepare.build_selected_wallet_scope(asset="BTC",stable="USDC",amount=0,snapshot={},now=NOW)


@pytest.mark.parametrize("asset",["BTC","XRP","SOL","TRX"])
def test_wrapped_owner_runtime_pins_remote_scope_and_selects_only_exact_rules(asset,monkeypatch):
    from backend import shared_evm_central_runtime as runtime
    from backend.ethereum_wrapped_spot_policy import WRAPPED_SPOT_POLICY
    selected=dict(scope(asset,"USDT"),settlement_policy=WRAPPED_SPOT_POLICY,
                  native_wrap_atomic_cap=None,owner_max_fee_atomic="0")
    args=kwargs([selected])
    policy=compose_scoped_evm_wallet_policy(**args)
    record={"wallet_id":"wallet_123456","wallet_address":WALLET,
            "owner_id":OWNER,"signer_id":SIGNER,"policy_id":"policy_123456",
            "policy_sha256":verified_wallet_policy_digest(policy,**args),
            "status":"GRANT_VERIFIED","scopes_json":{asset:selected},
            "revisions_json":{asset:3},"legacy_reconciliation_required":False,
            "original_owner_granted_at":NOW,"remote_verified_at":NOW,"revoked_at":None}
    class Cursor:
        def execute(self,*args): pass
        def fetchone(self): return record
    monkeypatch.setattr(runtime,"_locked_selected_budget",lambda *args,**kw:(100,3))
    current=runtime._locked_original_evm_coin(Cursor(),did="did:privy:wrappedtest",
                                            asset=asset,now=NOW)
    assert current["chain_id"]==1
    for side in ("BUY","SELL"):
        rule=runtime.central_policy_rule(current,asset=asset,stable="USDT",
                                        kind="swap",side=side,now=NOW)
        from backend.ethereum_wrapped_spot_policy import wrapped_swap_rule_name
        assert rule["name"]==wrapped_swap_rule_name(asset,side,"USDT")
        assert runtime.central_policy_rule(current,asset=asset,stable="USDT",
                                        kind="approval",side=side,now=NOW)["action"]=="ALLOW"
    assert runtime.central_policy_rule(current,asset=asset,stable="USDT",
             kind="approval_reset",side="BUY",now=NOW)["action"]=="ALLOW"
    with pytest.raises(ValueError):
        runtime.central_policy_rule(current,asset=asset,stable="USDT",
                                    kind="native_withdraw",now=NOW)


@pytest.mark.parametrize("asset",["BTC","XRP","SOL","TRX"])
def test_wrapped_grant_gate_refuses_changed_recipient_path_and_owner_cap(asset):
    from fastapi import HTTPException
    from backend.shared_evm_policy_swap_gate import check_evm_policy_bound_swap
    from backend.ethereum_wrapped_spot_policy import WRAPPED_SPOT_POLICY,wrapped_spot_transaction
    selected=dict(scope(asset),settlement_policy=WRAPPED_SPOT_POLICY,
                  native_wrap_atomic_cap=None,owner_max_fee_atomic="0")
    data=kwargs([selected])
    candidate=compose_scoped_evm_wallet_policy(**data)
    record={"asset":asset,"chain_id":1,"stable":"USDC","wallet_address":WALLET,
            "authorization_method":"PRIVY_CENTRAL_ONE_SIGNER","status":"GRANT_VERIFIED",
            "revoked_at":None,"scope":selected,"candidate_json":candidate,
            "policy_sha256":verified_wallet_policy_digest(candidate,**data)}
    tx=wrapped_spot_transaction(asset=asset,stable="USDC",side="BUY",wallet=WALLET,
                                 amount_atomic=1000000,min_out_atomic=42)
    args=dict(record=record,asset=asset,side="BUY",stable="USDC",wallet=WALLET,
              tx=tx,input_atomic="1000000",minimum_output_atomic="42",now=NOW)
    assert check_evm_policy_bound_swap(**args)["exact_swap_validated"] is True
    for modified in (
        dict(args,input_atomic="100000001"),
        dict(args,tx=dict(tx,data=tx["data"][:-2]+"01")),
        dict(args,record=dict(record,scope=dict(selected,settlement_policy=None))),
    ):
        with pytest.raises(HTTPException):
            check_evm_policy_bound_swap(**modified)

def test_wrapped_btc_scope_reads_only_original_live_usd_budget():
    from decimal import Decimal
    from backend.shared_evm_wallet_policy_prepare import _locked_selected_budget
    from fastapi import HTTPException
    class Cursor:
        def execute(self,query,params):
            assert "mininex_live_btc_limits" in query
            assert "FOR UPDATE" in query
        def fetchone(self):
            return {"planned_capital_usd":Decimal("235.50"),"revision":3,"stable":"USDT"}
    assert _locked_selected_budget(Cursor(),did="did:privy:btc",asset="BTC",
                    stable="USDT",expected_revision=3)==(Decimal("235.50"),3)
    with pytest.raises(HTTPException):
        _locked_selected_budget(Cursor(),did="did:privy:btc",asset="BTC",
                    stable="USDC",expected_revision=3)


@pytest.mark.parametrize("asset,precision",[("BTC",8),("XRP",18),("SOL",9)])
def test_wrapped_route_common_transport_and_exact_path_quote(asset,precision):
    from eth_abi import encode
    from backend.ethereum_wrapped_spot_quote import decode_exact_path_quote
    from backend.ethereum_wrapped_spot_policy import wrapped_spot_transaction,decode_wrapped_spot_amounts
    from backend.shared_evm_swap_receipts import _pair
    from backend.shared_evm_privy_transport import bound_privy_evm_swap_request
    pair=_pair(asset,"USDC","BUY")
    assert pair["chain_id"]==1 and pair["output_decimals"]==precision
    tx=wrapped_spot_transaction(asset=asset,stable="USDC",side="BUY",wallet=WALLET,
                                amount_atomic=1000000,min_out_atomic=42)
    assert decode_wrapped_spot_amounts(tx["data"],asset=asset,stable="USDC",side="BUY",
                                      wallet=WALLET)==(1000000,42)
    url,body=bound_privy_evm_swap_request(asset=asset,wallet_id="wallet_123456",wallet=WALLET,
             tx=tx,side="BUY",stable="USDC",input_atomic="1000000",minimum_output_atomic="42")
    assert body["caip2"]=="eip155:1"
    assert body["params"]["transaction"]["data"]==tx["data"]
    hops=1 if asset=="BTC" else 2
    raw="0x"+encode(["uint256","uint160[]","uint32[]","uint256"],
                   [500,[123]*hops,[0]*hops,200000]).hex()
    assert decode_exact_path_quote(raw,hops)==(500,200000)
    with pytest.raises(ValueError):
        decode_exact_path_quote(raw,hops+1)
    with pytest.raises(ValueError):
        decode_wrapped_spot_amounts(tx["data"]+"00",asset=asset,stable="USDC",side="BUY",wallet=WALLET)


@pytest.mark.parametrize("asset",["BTC","XRP","SOL","TRX"])
def test_wrapped_usdt_reset_is_exact_and_distinct_from_native_allowance(asset):
    from backend.shared_evm_allowance import inspect_combined_grant_router_approval,bound_owner_router_approval_rpc
    from backend.ethereum_wrapped_spot_policy import WRAPPED_SPOT_POLICY
    selected=dict(scope(asset,"USDT"),settlement_policy=WRAPPED_SPOT_POLICY,
                  native_wrap_atomic_cap=None,owner_max_fee_atomic="0")
    data=kwargs([selected])
    candidate=compose_scoped_evm_wallet_policy(**data)
    record={"asset":asset,"stable":"USDT","wallet_address":WALLET,
            "authorization_method":"PRIVY_CENTRAL_ONE_SIGNER","status":"GRANT_VERIFIED",
            "revoked_at":None,"scope":selected,"candidate_json":candidate,
            "policy_sha256":verified_wallet_policy_digest(candidate,**data)}
    for reset in (False,True):
        reviewed=inspect_combined_grant_router_approval(record=record,asset=asset,
                    side="BUY",stable="USDT",amount_atomic=1000000,now=NOW,reset=reset)
        _,body=bound_owner_router_approval_rpc(asset=asset,stable="USDT",side="BUY",
              wallet=WALLET,wallet_id="wallet_123456",amount_atomic=1000000,
              wrapped_spot=True,reset=reset)
        assert body["params"]["transaction"]["data"]==reviewed["transaction"]["data"]
        assert int(reviewed["transaction"]["data"][-64:],16)==(0 if reset else 1000000)
    with pytest.raises(ValueError):
        bound_owner_router_approval_rpc(asset=asset,stable="USDT",side="BUY",
                  wallet=WALLET,wallet_id="wallet_123456",amount_atomic=1000000)


@pytest.mark.parametrize("stable",["USDC","USDT"])
@pytest.mark.parametrize("side",["BUY","SELL"])
def test_trx_v2_exact_path_and_policy(stable,side):
    from eth_abi import encode,decode
    from backend.ethereum_wrapped_spot_policy import (
        WRAPPED_SPOT_POLICY,wrapped_spot_route,wrapped_spot_transaction,decode_wrapped_spot_amounts,
        wrapped_swap_rule_name)
    from backend.ethereum_wrapped_spot_quote import decode_v2_path_quote
    from backend.shared_evm_privy_transport import bound_privy_evm_swap_request
    from backend.shared_evm_policy_swap_gate import check_evm_policy_bound_swap
    from fastapi import HTTPException
    route=wrapped_spot_route("TRX",stable,side)
    assert route["chain_id"]==1 and route["source_decimals"]==route["destination_decimals"]==6
    assert len(route["path"])==(2 if stable=="USDC" else 3)
    assert route["path"]==list(reversed(wrapped_spot_route("TRX",stable,"SELL" if side=="BUY" else "BUY")["path"]))
    selected=dict(scope("TRX",stable),settlement_policy=WRAPPED_SPOT_POLICY,
                  native_wrap_atomic_cap=None,owner_max_fee_atomic="0")
    data=kwargs([selected]); policy=compose_scoped_evm_wallet_policy(**data)
    rule=next(r for r in policy["rules"] if r["name"]==wrapped_swap_rule_name("TRX",side,stable))
    assert any(c["field"]=="swapExactTokensForTokens.path" and c["operator"]=="eq" and
               c["value"]==route["path"] for c in rule["conditions"])
    record={"asset":"TRX","chain_id":1,"stable":stable,"wallet_address":WALLET,
            "authorization_method":"PRIVY_CENTRAL_ONE_SIGNER","status":"GRANT_VERIFIED",
            "revoked_at":None,"scope":selected,"candidate_json":policy,
            "policy_sha256":verified_wallet_policy_digest(policy,**data)}
    tx=wrapped_spot_transaction(asset="TRX",stable=stable,side=side,wallet=WALLET,
                                amount_atomic=1000000,min_out_atomic=42)
    assert decode_wrapped_spot_amounts(tx["data"],asset="TRX",stable=stable,side=side,wallet=WALLET)==(1000000,42)
    assert check_evm_policy_bound_swap(record=record,asset="TRX",side=side,stable=stable,wallet=WALLET,
                tx=tx,input_atomic="1000000",minimum_output_atomic="42",now=NOW)["exact_swap_validated"]
    _,body=bound_privy_evm_swap_request(asset="TRX",wallet_id="wallet_123456",wallet=WALLET,
                 tx=tx,side=side,stable=stable,input_atomic="1000000",minimum_output_atomic="42")
    assert body["caip2"]=="eip155:1"
    raw="0x"+encode(["uint256[]"],[[1000000]+[500000]*(len(route["path"])-1)]).hex()
    assert decode_v2_path_quote(raw,input_atomic=1000000,hops=len(route["path"])-1)==500000
    with pytest.raises(ValueError):
        decode_v2_path_quote(raw,input_atomic=1000001,hops=len(route["path"])-1)
    corrupted=tx["data"][:10]+encode(["uint256","uint256","address[]","address"],
        [1000000,42,list(reversed(route["path"])),WALLET]).hex()
    with pytest.raises(ValueError):
        decode_wrapped_spot_amounts(corrupted,asset="TRX",stable=stable,side=side,wallet=WALLET)
    with pytest.raises(HTTPException):
        check_evm_policy_bound_swap(record=record,asset="TRX",side=side,stable=stable,wallet=WALLET,
                tx=dict(tx,data=corrupted),input_atomic="1000000",minimum_output_atomic="42",now=NOW)


def test_expired_sibling_rule_reconstructed_without_extending_its_expiry():
    from backend.shared_evm_consolidated_wallet_policy import changed_wallet_coin
    old={"LINK":scope("LINK"),"QNT":scope("QNT")}
    old["LINK"]["expires_at"]=NOW+10
    original=compose_scoped_evm_wallet_policy(**kwargs(list(old.values())))
    later=dict(kwargs(list(old.values())),now=NOW+20)
    with pytest.raises(ValueError):
        compose_scoped_evm_wallet_policy(**later)
    assert compose_scoped_evm_wallet_policy(**later,allow_expired=True)==original
    new={**old,"LINK":dict(old["LINK"],expires_at=NOW+86400,buy_source_atomic_cap="200000000")}
    assert changed_wallet_coin(old,new,{"LINK":1,"QNT":2},{"LINK":3,"QNT":2})=="LINK"
    with pytest.raises(ValueError):
        changed_wallet_coin(old,new,{"LINK":1,"QNT":2},{"LINK":3,"QNT":4})
    with pytest.raises(ValueError):
        changed_wallet_coin(old,{**new,"QNT":dict(old["QNT"],buy_source_atomic_cap="200000000")})


def test_active_coin_runtime_never_uses_expired_or_changed_sibling_budget(monkeypatch):
    from backend import shared_evm_central_runtime as runtime
    from fastapi import HTTPException
    old=[dict(scope("LINK"),expires_at=NOW+10),scope("QNT")]
    args=kwargs(old)
    policy=compose_scoped_evm_wallet_policy(**args)
    record={"wallet_id":"wallet_123456","wallet_address":WALLET,
            "owner_id":OWNER,"signer_id":SIGNER,"policy_id":"policy_123456",
            "policy_sha256":verified_wallet_policy_digest(policy,**args),
            "status":"GRANT_VERIFIED","scopes_json":{x["asset"]:x for x in old},
            "revisions_json":{"LINK":1,"QNT":2},"legacy_reconciliation_required":False,
            "original_owner_granted_at":NOW,"remote_verified_at":NOW,"revoked_at":None}
    class Cursor:
        def execute(self,*args): pass
        def fetchone(self): return record
    def locked(cur,*,did,asset,stable,expected_revision):
        assert asset=="QNT" and expected_revision==2
        return 100,2
    monkeypatch.setattr(runtime,"_locked_selected_budget",locked)
    proof=runtime._locked_original_evm_coin(Cursor(),did="did:privy:wrappedtest",asset="QNT",now=NOW+20)
    assert proof["candidate_json"]==policy
    with pytest.raises(HTTPException):
        runtime._locked_original_evm_coin(Cursor(),did="did:privy:wrappedtest",asset="LINK",now=NOW+20)


@pytest.mark.parametrize("asset",["ETH","LINK","QNT","UNI","BNB","POL","ARB","AVAX","BTC","XRP","SOL","TRX"])
def test_explicit_durable_scope_preserves_every_non_time_condition(asset):
    from backend.shared_evm_consolidated_wallet_policy import (
        durable_wallet_scope,valid_scope_fields,DURABLE_EXPIRY_SENTINEL,DIRECT_SETTLEMENT_POLICY,
        DIRECT_SETTLEMENT_ASSETS)
    from backend.ethereum_wrapped_spot_policy import WRAPPED_SPOT_TOKENS,WRAPPED_SPOT_POLICY
    stable="USDT" if asset=="BNB" else "USDC"
    original=scope(asset,stable,native=asset in ("ETH","BNB","POL","AVAX"))
    if asset in WRAPPED_SPOT_TOKENS or asset in DIRECT_SETTLEMENT_ASSETS:
        original.update(settlement_policy=WRAPPED_SPOT_POLICY if asset in WRAPPED_SPOT_TOKENS
                        else DIRECT_SETTLEMENT_POLICY,native_wrap_atomic_cap=None,owner_max_fee_atomic="0")
    base=compose_scoped_evm_wallet_policy(**kwargs([original]))
    durable=durable_wallet_scope(original)
    assert valid_scope_fields(durable)
    assert not valid_scope_fields(dict(durable,expires_at=NOW+86400))
    assert not valid_scope_fields(dict(original,expires_at=DURABLE_EXPIRY_SENTINEL))
    args=kwargs([durable])
    result=compose_scoped_evm_wallet_policy(**args)
    assert result==compose_scoped_evm_wallet_policy(**dict(args,now=NOW+86400*100))
    assert len(result["rules"])==len(base["rules"])
    for old,new in zip(base["rules"],result["rules"]):
        assert old["name"]==new["name"]
        expected=[c for c in old["conditions"] if not (
            c["field_source"]=="system" and c["field"]=="current_unix_timestamp")]
        assert new["conditions"]==expected
        assert new["conditions"]
    assert verified_wallet_policy_digest(result,**args)!=verified_wallet_policy_digest(base,**kwargs([original]))


@pytest.mark.parametrize("stable",["USDC","USDT"])
def test_avax_staged_policy_preserves_sibling_and_binds_both_legs(stable):
    from backend.ethereum_stargate_staged_policy import (
        STARGATE_STAGED_POLICY, bridge_transaction, inspect_staged_bridge_transaction, CONFIG)
    from backend.shared_evm_consolidated_wallet_policy import (
        durable_wallet_scope, changed_wallet_coin, valid_scope_fields)
    original=scope("AVAX",stable,native=True)
    sibling=scope("LINK")
    staged=durable_wallet_scope(dict(original,settlement_policy=STARGATE_STAGED_POLICY,
                native_wrap_atomic_cap=None,owner_max_fee_atomic="0",
                bridge_fee_caps_atomic={"1":"10000000000000000","43114":"100000000000000000"}))
    assert valid_scope_fields(staged)
    assert changed_wallet_coin({"AVAX":original,"LINK":sibling},
            {"AVAX":staged,"LINK":sibling},{"AVAX":1,"LINK":2},{"AVAX":1,"LINK":2})=="AVAX"
    policy=compose_scoped_evm_wallet_policy(**kwargs([staged,sibling]))
    assert [r for r in policy["rules"] if r["name"].startswith("MiniNex LINK ")]==(
        compose_scoped_evm_wallet_policy(**kwargs([sibling]))["rules"])
    assert verified_wallet_policy_digest(policy,**kwargs([staged,sibling]))
    for side,chain in (("BUY",1),("SELL",43114)):
        args=dict(stable=stable,side=side,wallet=WALLET,
                  amount_atomic=1000000,min_out_atomic=990000,native_fee_atomic=1000)
        tx=bridge_transaction(**args)
        assert tx["chainId"]==chain
        assert tx["to"].lower()==CONFIG[chain]["pools"][stable].lower()
        assert inspect_staged_bridge_transaction(tx,scope=staged,**args)==tx
        for bad in (dict(tx,data=tx["data"]+"00"),dict(tx,to=TREASURY),dict(tx,value="1001")):
            with pytest.raises(ValueError):
                inspect_staged_bridge_transaction(bad,scope=staged,**args)
        with pytest.raises(ValueError):
            inspect_staged_bridge_transaction(tx,scope=staged,**dict(args,amount_atomic=100000001))


@pytest.mark.parametrize("asset",["BNB","POL","ARB"])
def test_direct_to_staged_scope_requires_exact_single_coin_transition(asset):
    from backend.shared_evm_consolidated_wallet_policy import (
        changed_wallet_coin,DIRECT_SETTLEMENT_POLICY,STAGED_ACROSS_POLICY,durable_wallet_scope)
    old=dict(scope(asset),settlement_policy=DIRECT_SETTLEMENT_POLICY,
             native_wrap_atomic_cap=None,owner_max_fee_atomic="0")
    staged=dict(old,settlement_policy=STAGED_ACROSS_POLICY,transit_atomic_cap="1200000000")
    sibling=scope("LINK")
    before={"LINK":sibling,asset:old}
    after={"LINK":sibling,asset:staged}
    revisions={"LINK":2,asset:3}
    assert changed_wallet_coin(before,after,revisions,revisions)==asset
    with pytest.raises(ValueError):
        changed_wallet_coin(before,after,revisions,dict(revisions,**{asset:4}))
    with pytest.raises(ValueError):
        changed_wallet_coin(after,before,revisions,revisions)
    with pytest.raises(ValueError):
        changed_wallet_coin(before,{**after,"LINK":dict(sibling,stable="USDT")})
    durable=durable_wallet_scope(staged)
    args=kwargs([sibling,durable])
    candidate=compose_scoped_evm_wallet_policy(**args)
    assert verified_wallet_policy_digest(candidate,**args)
