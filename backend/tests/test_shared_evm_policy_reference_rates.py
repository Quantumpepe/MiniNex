"""Real user budget drives per-coin Privy atomic caps; no platform max."""
from decimal import Decimal
import inspect
import pytest
from fastapi import HTTPException

from backend.shared_evm_policy_reference_rates import (
    _price, derive_user_budget_evm_atomic_caps,
)
from backend.shared_evm_policy_candidate_stage import _stage_from_prices_locked

NOW=1800000000
SYMBOLS={
    "BNB":("binancecoin",500),
    "POL":("polygon-ecosystem-token",0.5),
    "AVAX":("avalanche-2",50),
    "LINK":("chainlink",20),
    "UNI":("uniswap",10),
}


def prices(**revisions):
    result={
        ticker:{"usd":price,"last_updated_at":NOW}
        for ticker,price in SYMBOLS.values()
    }
    result.update({
        "usd-coin":{"usd":1,"last_updated_at":NOW},
        "tether":{"usd":1,"last_updated_at":NOW},
    })
    for key,val in revisions.items():
        result[key]=val
    return result


@pytest.mark.parametrize("asset,stable,source,coin",[
    ("BNB","USDT",10**21,2*10**18),
    ("POL","USDC",10**9,2000*10**18),
    ("AVAX","USDC",10**9,20*10**18),
    ("LINK","USDC",10**9,50*10**18),
    ("UNI","USDT",10**9,100*10**18),
])
def test_five_exact_user_budget_caps_respect_network_stable_decimals(
    asset,stable,source,coin
):
    output=derive_user_budget_evm_atomic_caps(
        asset=asset,stable=stable,budget_usd=1000,
        snapshot=prices(),now=NOW)
    assert int(output["buy_source_atomic_cap"])==source
    assert int(output["sell_source_atomic_cap"])==coin
    assert output["native_wrap_atomic_cap"]==(
        str(coin) if asset in ("BNB","POL","AVAX") else None)
    assert output["budget_usd_user_selected"]==1000
    assert output["swap_quote_ready"] is False


def test_large_owner_selected_budget_has_no_arbitrary_system_ceiling():
    value=derive_user_budget_evm_atomic_caps(
        asset="LINK",stable="USDC",budget_usd=500000,
        snapshot=prices(),now=NOW)
    assert int(value["buy_source_atomic_cap"])==500000000000
    assert int(value["sell_source_atomic_cap"])==25000*10**18


@pytest.mark.parametrize("field,new",[
    ("usd-coin",{"usd":0.85,"last_updated_at":NOW}),
    ("tether",{"usd":1.2,"last_updated_at":NOW}),
    ("chainlink",{"usd":20,"last_updated_at":NOW-200}),
    ("chainlink",{"usd":0,"last_updated_at":NOW}),
    ("chainlink",{"usd":"NaN","last_updated_at":NOW}),
    ("chainlink",{"usd":20,"last_updated_at":NOW+20}),
])
def test_offpeg_stale_or_unpriced_coin_fails_closed(field,new):
    snapshot=prices()
    snapshot[field]=new
    stable="USDT" if field=="tether" else "USDC"
    with pytest.raises(ValueError):
        derive_user_budget_evm_atomic_caps(
            asset="LINK",stable=stable,budget_usd=1000,
            snapshot=snapshot,now=NOW)


class Cur:
    def __init__(self,rows):self.rows=iter(rows);self.calls=[]
    def execute(self,q,params):self.calls.append((q,params))
    def fetchone(self):return next(self.rows)


def test_policy_candidate_recomputes_caps_from_locked_latest_budget():
    wallet="0x"+"ab"*20
    did="did:privy:owner"
    stable="USDC"
    settings={"stable":stable,"budget_usd":1000,"revision":3}
    cur=Cur([
        settings,settings,
        {"status":"SIGNED_INTENT","wallet_address":wallet,
         "wallet_id":"wallet_1234567","chain_id":1,
         "stable":stable,"budget_revision":3,"budget_usd":1000,
         "confirmed_at":object(),"message_sha256":"a"*64},
        None,{"status":"DRAFT"},
    ])
    report=_stage_from_prices_locked(
        cur,did=did,asset="LINK",
        owned={"wallet_id":"wallet_1234567","address":wallet},
        owner_id="owner_1234567",signer_id="signer_1234567",
        snapshot=prices(),now=NOW)
    assert report["rules_count"]==2
    assert report["signed_budget_revision"]==3
    assert report["policy_published_in_privy"] is False
    assert all(
        "INSERT INTO mininex_live_evm_owner_permissions" not in sql
        or params[0]==did for sql,params in cur.calls)


def test_authenticated_policy_prepare_does_not_create_privy_remote_grant():
    from backend.app import app
    r=next(r for r in app.routes if
        r.path=="/api/live/evm/policy/{asset}/prepare")
    assert r.methods=={"POST"}
    source=inspect.getsource(r.endpoint)
    assert "verify_privy_bearer" in source
    assert "stage_owner_evm_policy_from_budget" in source
    assert "publish" not in source
