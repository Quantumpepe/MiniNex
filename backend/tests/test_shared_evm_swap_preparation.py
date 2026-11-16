"""Combined EVM wallet, allowance and exact-amount swap preparation tests."""
import asyncio
import time

import pytest
from fastapi import HTTPException

from backend.shared_evm_execution import EvmPair, unsigned_exact_input_single
from backend.shared_evm_swap_receipts import _pair
from backend import shared_evm_swap_preparation as prep

WALLET="0x"+"ab"*20


def sample(asset,side,stable,amount,*,balance_ok=True,allowance_ok=True,gas_ok=True):
    pair=_pair(asset,stable,side)
    amount_atomic=10**pair["source_decimals"]
    tx=unsigned_exact_input_single(
        pair=EvmPair(asset,pair["chain_id"],pair["network"],
                     pair["source_token"],pair["output_token"],
                     pair["router"],3000),
        wallet=WALLET,amount_in=amount_atomic,min_out=10**pair["output_decimals"],
    )
    return pair, {
        "asset":asset,"side":side,"stable":stable,"input_amount":amount,
        "network":pair["network"],"wallet_identity_verified":True,
        "execution_enabled":False,"can_submit":False,
        "transaction_expires_at":int(time.time())+15,
        "onchain_quote_verified":True,
        "transaction":tx,
    }, {
        "mode":"SHARED_EVM_SOURCE_ALLOWANCE_READ_ONLY",
        "asset":asset,"side":side,"stable":stable,"input_amount":amount,
        "network":pair["network"],"chain_id":pair["chain_id"],
        "router":pair["router"],"source_token_address":pair["source_token"],
        "wallet_address":WALLET.lower(),
        "required_input_atomic":str(amount_atomic),
        "source_funds_sufficient":balance_ok,
        "router_allowance_sufficient":allowance_ok,
        "native_gas_available":gas_ok,
        "owner_approval_transaction":{"to":pair["source_token"]},
        "wallet_identity_verified":True,
        "execution_enabled":False,"can_submit":False,
    }


@pytest.mark.parametrize("asset,stable",[
    ("ETH","USDC"),("ETH","USDT"),("LINK","USDC"),
    ("UNI","USDT"),("BNB","USDT"),("POL","USDC"),("AVAX","USDC"),
])
@pytest.mark.parametrize("side",["BUY","SELL"])
def test_all_evm_assets_join_exact_quote_balance_and_allowance(asset,stable,side):
    pair,quote,allowance=sample(asset,side,stable,"1")
    result=prep._approved_quote_pair(
        quote,allowance,asset=asset,side=side,stable=stable,
        amount="1",pair=pair)
    assert result["asset"]==asset
    assert result["chain_id"]==pair["chain_id"]
    assert result["source_funds_sufficient"] is True
    assert result["owner_wallet_signing_possible"] is True
    assert result["unsigned_transaction"]==quote["transaction"]
    assert result["can_submit"] is False
    assert result["execution_enabled"] is False
    assert result["native_wrap_unwrap_complete"] is False
    assert result["fee_assessed"] is False


@pytest.mark.parametrize("source_ok,approved,gas,ready",[
    (True,False,True,False),(False,True,True,False),
    (True,True,False,False),(True,True,True,True),
])
def test_no_owner_swap_exposed_before_all_current_source_proofs(
    source_ok,approved,gas,ready
):
    pair,q,a=sample(
        "POL","BUY","USDC","1",
        balance_ok=source_ok,allowance_ok=approved,gas_ok=gas)
    r=prep._approved_quote_pair(
        q,a,asset="POL",side="BUY",stable="USDC",amount="1",pair=pair)
    assert (r["unsigned_transaction"] is not None)==ready
    assert (r["owner_approval_transaction"] is not None)==(
        source_ok and gas and not approved)


@pytest.mark.parametrize("change",[
    lambda q,a:q.update(transaction_expires_at=1),
    lambda q,a:q.update(onchain_quote_verified=False),
    lambda q,a:q["transaction"].update(value="1"),
    lambda q,a:q["transaction"].update(chainId=1),
    lambda q,a:q["transaction"].update(to="0x"+"cd"*20),
    lambda q,a:a.update(wallet_address="0x"+"cd"*20),
    lambda q,a:a.update(required_input_atomic="99999"),
    lambda q,a:a.update(source_token_address="0x"+"cd"*20),
    lambda q,a:a.update(chain_id=56),
    lambda q,a:a.update(router="0x"+"cd"*20),
    lambda q,a:q.update(stable="USDT"),
    lambda q,a:q["transaction"].update(data=q["transaction"]["data"][:10]+
        f"{int('0x'+'cd'*20,16):064x}"+q["transaction"]["data"][74:]),
])
def test_cross_chain_wallet_token_and_atomic_mismatches_fail_closed(change):
    pair,q,a=sample("AVAX","BUY","USDC","1")
    change(q,a)
    with pytest.raises((ValueError,TypeError)):
        prep._approved_quote_pair(
            q,a,asset="AVAX",side="BUY",stable="USDC",amount="1",pair=pair)


@pytest.mark.parametrize("asset,stable",[
    ("ETH","USDC"),("BNB","USDT"),("POL","USDC"),
    ("AVAX","USDC"),("LINK","USDC"),("UNI","USDT"),
])
def test_one_executor_routes_every_evm_asset_through_own_quote_and_shared_allowance(
    monkeypatch,asset,stable
):
    seen=[]
    pair,q,a=sample(asset,"BUY",stable,"1")
    async def eth(did,*,side,stable,amount,include_user_transaction):
        seen.append(("ETH",did,include_user_transaction))
        return q
    async def erc20(did,*,asset,side,stable,amount,include_user_transaction):
        seen.append((asset,did,include_user_transaction))
        return q
    async def native(did,*,asset,side,stable,amount,include_user_transaction):
        seen.append((asset,did,include_user_transaction))
        return q
    async def allowance(did,*,asset,side,stable,amount):
        seen.append(("ALLOWANCE",did,asset))
        return a
    monkeypatch.setattr(prep,"make_policy_compatible_eth_quote",eth)
    monkeypatch.setattr(prep,"quote_owner_erc20_trade",erc20)
    monkeypatch.setattr(prep,"quote_native_evm_trade",native)
    monkeypatch.setattr(prep,"check_evm_source_allowance",allowance)
    result=asyncio.run(prep.prepare_owner_evm_swap(
        "did:privy:test",asset=asset,side="BUY",stable=stable,amount="1"))
    assert result["exact_swap_abi_and_amount_verified"] is True
    assert (asset,"did:privy:test",True) in seen
    assert ("ALLOWANCE","did:privy:test",asset) in seen


def test_api_is_authenticated_and_never_signs():
    import inspect
    from backend.app import app
    api=next(r for r in app.routes
             if r.path=="/api/live/evm/owner-swap-preparation/{asset}")
    text=inspect.getsource(api.endpoint)
    assert "verify_privy_bearer" in text
    assert "prepare_owner_evm_swap" in text
    assert "broadcast_privy" not in text
