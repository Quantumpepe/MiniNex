"""ARB on Arbitrum One is a real ERC20, not its native gas coin.

These tests use official immutable contract addresses. They do NOT grant
Privy policies, send real transactions or infer a profitable live trade.
"""
import pytest
from fastapi import HTTPException

from backend.live_asset_registry import get_asset
from backend.shared_evm_execution import chain_for_asset, EvmPair, unsigned_exact_input_single
from backend.shared_evm_swap_receipts import _pair, inspect_finalized_evm_swap
from backend.native_evm_v3_quote import _route, build_native_v3_transaction
from backend.multicoin_evm_balances import _RPC
from backend.shared_evm_policy_reference_rates import _IDS as SPOT_IDS
from backend.shared_evm_historical_usd import _IDS as HISTORICAL_IDS

ARB="0x912CE59144191C1204E64559FE8253a0e49E6548"
USDC="0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
USDT0="0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"
ROUTER="0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"
QUOTER="0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
WALLET="0x"+"a"*40

@pytest.mark.parametrize("stable,address", [("USDC",USDC),("USDT",USDT0)])
def test_arbitrum_arb_token_route_uses_eth_gas_and_proper_stable(stable,address):
    assert chain_for_asset("ARB")== (42161,"arbitrum-one-mainnet")
    info=get_asset("ARB")
    assert info.chain_id==42161
    assert info.execution_family=="evm-erc20"
    assert stable in info.stablecoin_candidates
    assert _RPC["ARB_TOKENS"][0:2]==(42161,"arbitrum-one-mainnet")
    route=_route("ARB",stable)
    assert route.network=="arbitrum-one-mainnet"
    assert route.router==ROUTER
    assert route.quoter==QUOTER
    assert route.wrapped==ARB
    assert route.stable_contract==address
    assert route.stable_decimals==6
    buy=_pair("ARB",stable,"BUY")
    sell=_pair("ARB",stable,"SELL")
    assert buy["chain_id"]==42161 and buy["router"]==ROUTER
    assert buy["source_token"]==address
    assert buy["output_token"]==ARB
    assert sell["source_token"]==ARB
    assert sell["output_token"]==address
    assert buy["source_decimals"]==6 and buy["output_decimals"]==18
    assert sell["source_decimals"]==18 and sell["output_decimals"]==6
    assert SPOT_IDS["ARB"]=="arbitrum"
    assert HISTORICAL_IDS["ARB"]=="arbitrum"
    assert "native_wrap" not in buy

def test_arbitrum_exact_input_single_is_an_unsigned_token_transfer_via_router02():
    tx=build_native_v3_transaction(
        asset="ARB",stable="USDC",side="BUY",wallet=WALLET,
        amount_in=2_000_000,min_out=10**18,fee=500)
    assert tx["from"]==WALLET
    assert tx["to"]==ROUTER
    assert tx["chainId"]==42161
    assert tx["value"]=="0"
    assert tx["data"].startswith("0x04e45aaf")
    assert len(tx["data"])==2+8+7*64
    words=[int(tx["data"][10+i*64:10+(i+1)*64],16) for i in range(7)]
    assert words==[int(USDC,16),int(ARB,16),500,int(WALLET,16),2_000_000,10**18,0]
    sell=build_native_v3_transaction(
        asset="ARB",stable="USDT",side="SELL",wallet=WALLET,
        amount_in=10**18,min_out=100_000,fee=3000)
    words2=[int(sell["data"][10+i*64:10+(i+1)*64],16) for i in range(7)]
    assert words2[0]==int(ARB,16)
    assert words2[1]==int(USDT0,16)
    assert words2[-3:]==[10**18,100_000,0]

def test_arbitrum_wrong_settlement_and_wrong_network_are_never_signed():
    with pytest.raises(HTTPException) as err:
        _route("ARB","USDC.e")
    assert err.value.status_code==422
    with pytest.raises(ValueError):
        unsigned_exact_input_single(pair=EvmPair(
            "ARB",1,"ethereum-mainnet",USDC,ARB,ROUTER,500),
            wallet=WALLET,amount_in=10**6,min_out=10**18)
    tx=build_native_v3_transaction(
        asset="ARB",stable="USDC",side="BUY",wallet=WALLET,
        amount_in=10**6,min_out=10**18,fee=500)
    hash_="0x"+"b"*64
    pending={"hash":hash_,"from":WALLET,"to":ROUTER,
             "chainId":"0xa4b1","value":"0x0",
             "input":tx["data"],"blockHash":None,"blockNumber":None}
    proof=inspect_finalized_evm_swap(hash_,asset="ARB",stable="USDC",
        side="BUY",wallet=WALLET,chain_id="0xa4b1",
        transaction=pending,receipt=None,latest="0x100",finalized=None)
    assert proof["state"]=="PENDING"
    assert proof["swap_erc20_leg_verified"] is False
    assert proof["performance_fee_assessed"] is False
    with pytest.raises(ValueError):
        inspect_finalized_evm_swap(hash_,asset="ARB",stable="USDC",
            side="BUY",wallet=WALLET,chain_id="0x1",
            transaction=pending,receipt=None,latest="0x100",finalized=None)
