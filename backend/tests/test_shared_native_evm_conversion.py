"""Same native WETH9-style transaction encoder across four EVM networks."""
import pytest

from backend.shared_native_evm_conversion import bound_native_evm_conversion
from backend.eth_privy_native_rpc import bound_weth9_rpc
from backend.eth_swaprouter02_preflight import WETH_MAINNET
from backend.native_evm_v3_quote import CHAINS

WALLET="0x"+"ab"*20
WALLET_ID="wallet_12345678"
AMOUNT=123456789000000000


@pytest.mark.parametrize("asset,chain",[
    ("ETH",1),("BNB",56),("POL",137),("AVAX",43114),
])
@pytest.mark.parametrize("operation",["DEPOSIT","WITHDRAW"])
def test_all_native_evm_chains_share_weth9_wrap_unwrap_call(asset,chain,operation):
    url,payload=bound_native_evm_conversion(
        asset=asset,wallet_id=WALLET_ID,wallet=WALLET,
        operation=operation,amount_wei=AMOUNT,
    )
    assert url.endswith("/"+WALLET_ID+"/rpc")
    assert payload["caip2"]==f"eip155:{chain}"
    assert payload["method"]=="eth_sendTransaction"
    tx=payload["params"]["transaction"]
    assert tx["to"]==(WETH_MAINNET if asset=="ETH" else CHAINS[asset].wrapped)
    if operation=="DEPOSIT":
        assert tx["data"]=="0xd0e30db0"
        assert tx["value"]==str(AMOUNT)
    else:
        assert tx["data"]=="0x2e1a7d4d"+f"{AMOUNT:064x}"
        assert tx["value"]=="0"


def test_existing_real_eth_live_signer_keeps_exact_original_rpc_payload():
    old=bound_weth9_rpc(
        wallet_id=WALLET_ID,wallet=WALLET,
        operation="DEPOSIT",amount_wei=AMOUNT,
    )
    shared=bound_native_evm_conversion(
        asset="ETH",wallet_id=WALLET_ID,wallet=WALLET,
        operation="DEPOSIT",amount_wei=AMOUNT,
    )
    assert old==shared


@pytest.mark.parametrize("asset",["LINK","UNI","BTC","SOL","ADA"])
def test_non_native_assets_cannot_wrap_as_evm_gas_coins(asset):
    with pytest.raises(ValueError):
        bound_native_evm_conversion(
            asset=asset,wallet_id=WALLET_ID,wallet=WALLET,
            operation="DEPOSIT",amount_wei=100,
        )


@pytest.mark.parametrize("bad",[-1,0,True,1.5,"100",10**45])
def test_invalid_or_ambiguous_native_amounts_are_blocked(bad):
    with pytest.raises(ValueError):
        bound_native_evm_conversion(
            asset="BNB",wallet_id=WALLET_ID,wallet=WALLET,
            operation="WITHDRAW",amount_wei=bad,
        )


@pytest.mark.parametrize("wallet",["","0xabc","0x"+"cd"*19])
def test_native_conversion_must_be_bound_to_full_wallet_address(wallet):
    with pytest.raises(ValueError):
        bound_native_evm_conversion(
            asset="POL",wallet_id=WALLET_ID,wallet=wallet,
            operation="DEPOSIT",amount_wei=100,
        )
