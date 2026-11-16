"""The active ETH AUTO transport and all EVM routes share one exact payload gate."""
import pytest

from backend.shared_evm_execution import EvmPair,unsigned_exact_input_single
from backend.shared_evm_swap_receipts import _pair
from backend.shared_evm_auto_preflight import inspect_evm_exact_swap
from backend.eth_privy_auto_transport import bound_privy_rpc_request

WALLET="0x"+"ab"*20
WALLET_ID="wallet_12345678"


@pytest.mark.parametrize("asset,stable",[
    ("ETH","USDC"),("ETH","USDT"),("LINK","USDC"),
    ("UNI","USDT"),("BNB","USDT"),("POL","USDC"),("AVAX","USDC")
])
@pytest.mark.parametrize("side",["BUY","SELL"])
def test_six_asset_real_auto_swap_gate_is_identical(asset,stable,side):
    p=_pair(asset,stable,side)
    tx=unsigned_exact_input_single(
        pair=EvmPair(asset,p["chain_id"],p["network"],p["source_token"],
                     p["output_token"],p["router"],3000),
        wallet=WALLET,amount_in=1000000,min_out=500,
    )
    r=inspect_evm_exact_swap(
        tx,asset=asset,wallet=WALLET,side=side,stable=stable,
        input_atomic="1000000",minimum_output_atomic="500")
    assert r["chain_id"]==p["chain_id"]
    assert r["token_pair_verified"] is True
    assert r["server_broadcast_authorized"] is False
    if asset=="ETH":
        url,body=bound_privy_rpc_request(
            wallet_id=WALLET_ID,wallet=WALLET,tx=tx,
            side=side,stable=stable,input_atomic="1000000",
            minimum_output_atomic="500")
        assert body["caip2"]=="eip155:1"


@pytest.mark.parametrize("mutation",[
    lambda t:t.update(chainId=1),
    lambda t:t.update(to="0x"+"cd"*20),
    lambda t:t.update(value="1"),
    lambda t:t.update(**{"from":"0x"+"cd"*20}),
    lambda t:t.update(data=t["data"][:10]+
      f"{int('0x'+'cd'*20,16):064x}"+t["data"][74:]),
    lambda t:t.update(data=t["data"][:-64]+f"{1:064x}"),
    lambda t:t.update(data=t["data"][:10+4*64]+f"{999:064x}"+t["data"][10+5*64:]),
])
def test_mismatch_rejected_even_before_privy_signing(mutation):
    asset,side,stable="BNB","BUY","USDT"
    p=_pair(asset,stable,side)
    tx=unsigned_exact_input_single(
        pair=EvmPair(asset,p["chain_id"],p["network"],p["source_token"],
                     p["output_token"],p["router"],500),
        wallet=WALLET,amount_in=1000000,min_out=500)
    mutation(tx)
    with pytest.raises(ValueError):
        inspect_evm_exact_swap(
            tx,asset=asset,wallet=WALLET,side=side,stable=stable,
            input_atomic="1000000",minimum_output_atomic="500")


@pytest.mark.parametrize("bad",["-1","0","1.1","1e6"," 100","00001",""])
def test_noncanonical_or_zero_atomic_units_fail_closed(bad):
    p=_pair("POL","USDC","BUY")
    tx=unsigned_exact_input_single(
        pair=EvmPair("POL",137,p["network"],p["source_token"],
                     p["output_token"],p["router"],500),
        wallet=WALLET,amount_in=1000,min_out=100)
    with pytest.raises(ValueError):
        inspect_evm_exact_swap(tx,asset="POL",wallet=WALLET,
            side="BUY",stable="USDC",input_atomic=bad,
            minimum_output_atomic="100")
