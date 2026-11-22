"""One real shared EVM serialization path for ETH and every listed EVM coin."""
import pytest

from backend.shared_evm_execution import (
    EvmPair, chain_for_asset, unsigned_exact_input_single,
    bound_evm_wallet_rpc,
)

WALLET = "0x" + "ab" * 20
TOKEN_A = "0x" + "11" * 20
TOKEN_B = "0x" + "22" * 20
ROUTER = "0x" + "33" * 20

@pytest.mark.parametrize("asset,chain,network",[
    ("ETH",1,"ethereum-mainnet"),
    ("QNT",1,"ethereum-mainnet"),
    ("BNB",56,"bsc-mainnet"),
    ("POL",137,"polygon-pos-mainnet"),
    ("AVAX",43114,"avalanche-c-mainnet"),
    ("LINK",1,"ethereum-mainnet"),
    ("UNI",1,"ethereum-mainnet"),
])
def test_all_six_evm_assets_reuse_identical_transaction_engine(asset,chain,network):
    assert chain_for_asset(asset)==(chain,network)
    pair=EvmPair(asset,chain,network,TOKEN_A,TOKEN_B,ROUTER,3000)
    tx=unsigned_exact_input_single(
        pair=pair,wallet=WALLET,amount_in=10**18,min_out=100,
    )
    assert tx["chainId"]==chain
    assert tx["from"]==WALLET
    assert tx["to"]==ROUTER
    assert tx["value"]=="0"
    assert tx["data"].startswith("0x04e45aaf")
    assert len(tx["data"])==2+8+7*64
    url,payload=bound_evm_wallet_rpc(
        asset=asset,wallet_id="privy_wallet_123",wallet=WALLET,tx=tx)
    assert url.endswith("/privy_wallet_123/rpc")
    assert payload["caip2"]==f"eip155:{chain}"
    assert payload["params"]["transaction"]["data"]==tx["data"]
    assert payload["params"]["transaction"]["to"]==ROUTER


@pytest.mark.parametrize("asset",["BTC","SOL","XRP","TRX"])
def test_non_evm_coins_never_enter_evm_dispatch(asset):
    with pytest.raises(ValueError):
        chain_for_asset(asset)


@pytest.mark.parametrize("asset,incorrect_chain",[
    ("ETH",56),("LINK",137),("UNI",43114),("BNB",1),
    ("POL",56),("AVAX",137),
])
def test_chain_confusion_cannot_build_real_privy_transaction(asset,incorrect_chain):
    chain,network=chain_for_asset(asset)
    pair=EvmPair(asset,incorrect_chain,network,TOKEN_A,TOKEN_B,ROUTER,500)
    with pytest.raises(ValueError):
        unsigned_exact_input_single(pair=pair,wallet=WALLET,amount_in=100,min_out=1)
    good=unsigned_exact_input_single(
        pair=EvmPair(asset,chain,network,TOKEN_A,TOKEN_B,ROUTER,500),
        wallet=WALLET,amount_in=100,min_out=1)
    good["chainId"]=incorrect_chain
    with pytest.raises(ValueError):
        bound_evm_wallet_rpc(asset=asset,wallet_id="wallet_1234",wallet=WALLET,tx=good)


@pytest.mark.parametrize("bad", [
    ("wallet",WALLET,TOKEN_A,TOKEN_B,ROUTER,3000),
    (WALLET,WALLET,"not_an_address",TOKEN_B,ROUTER,3000),
    (WALLET,WALLET,TOKEN_A,TOKEN_A,ROUTER,3000),
    (WALLET,WALLET,TOKEN_A,TOKEN_B,"not_an_address",3000),
    (WALLET,WALLET,TOKEN_A,TOKEN_B,ROUTER,777),
])
def test_bad_wallet_address_token_or_fee_rejected(bad):
    wallet,_,token_in,token_out,router,fee=bad
    pair=EvmPair("ETH",1,"ethereum-mainnet",token_in,token_out,router,fee)
    with pytest.raises(ValueError):
        unsigned_exact_input_single(pair=pair,wallet=wallet,amount_in=100,min_out=1)


def test_privy_request_never_accepts_mismatched_wallet_or_chain():
    pair=EvmPair("BNB",56,"bsc-mainnet",TOKEN_A,TOKEN_B,ROUTER,500)
    tx=unsigned_exact_input_single(pair=pair,wallet=WALLET,amount_in=100,min_out=1)
    with pytest.raises(ValueError):
        bound_evm_wallet_rpc(asset="BNB",wallet_id="wallet_1234",
                             wallet="0x"+"cd"*20,tx=tx)
    tx["value"]="1e18"
    with pytest.raises(ValueError):
        bound_evm_wallet_rpc(asset="BNB",wallet_id="wallet_1234",wallet=WALLET,tx=tx)


def test_eth_real_quote_uses_same_builder_as_other_evm_networks():
    from backend.eth_router02_live_quote import build_unsigned_router02_swap
    from backend.eth_swaprouter02_preflight import (
        SWAPROUTER02_MAINNET,WETH_MAINNET
    )
    from backend.live_eth_quotes import STABLES
    tx=build_unsigned_router02_swap(
        wallet=WALLET,side="BUY",stable="USDC",
        amount_in=1000000,min_out=1000,fee=3000)
    generic=unsigned_exact_input_single(
        pair=EvmPair("ETH",1,"ethereum-mainnet",STABLES["USDC"],
                     WETH_MAINNET,SWAPROUTER02_MAINNET,3000),
        wallet=WALLET,amount_in=1000000,min_out=1000)
    assert tx==generic
