import pytest
from eth_abi import encode
from eth_utils import keccak
from backend.ethereum_settlement_calldata import (
    ACROSS,BRIDGE,DIRECT,DIAMOND,SWAP,EXACT_SINGLE,RECEIVERS,inspect_across_v4,
)
from backend.ethereum_settlement_plan import CROSS_COIN_CONTRACTS,ETHEREUM_STABLES
from backend.native_evm_v3_quote import CHAINS

W="0x"+"12"*20
TID=bytes.fromhex("ab"*32)
def word(a):return bytes.fromhex(a[2:].rjust(64,"0"))

def example(*,recipient=None,minout=5*10**18,deposit=False,local_override=None):
    receiver,executor=RECEIVERS[137]
    stable=ETHEREUM_STABLES["USDC"]
    local=local_override or CHAINS["POL"].stable_contract.lower()
    coin=CROSS_COIN_CONTRACTS["POL"]
    router=CHAINS["POL"].router.lower()
    amount=9_000_000
    data=EXACT_SINGLE+encode(["address","address","uint24","address","uint256","uint256","uint160"],
        [local,coin,3000,recipient or executor,amount,minout,0])
    swaps=[(router,router,local,coin,amount,data,deposit)]
    message=encode(["bytes32",SWAP+"[]","address"],[TID,swaps,W])
    b=(TID,"across","mininex",W,stable,W,10_000_000,137,False,True)
    a=(word(receiver),word(W),word(stable),word(local),amount,0,bytes(32),100,1000,0,message)
    tx={"from":W,"to":DIAMOND,"chainId":1,"value":"0",
        "data":"0x"+(DIRECT+encode([BRIDGE,ACROSS],[b,a])).hex()}
    terms=dict(wallet_address=W,source_chain_id=1,destination_chain_id=137,
        source_token=stable,destination_token=coin,input_atomic="10000000",
        minimum_output_atomic=str(5*10**18))
    return tx,terms,message

def test_destination_swap_matches_owner_and_full_bridged_amount():
    tx,terms,message=example()
    result=inspect_across_v4(tx,terms=terms,now=101)
    assert result["destination_swap"]["message_hash"]==keccak(message)
    assert result["destination_swap"]["minimum_final"]==5*10**18
    assert not result["execution_authorized"]
    for changes in ({"recipient":W},{"minout":1},{"deposit":True}):
        tx,terms,_=example(**changes)
        with pytest.raises(ValueError):
            inspect_across_v4(tx,terms=terms,now=101)


def test_polygon_usdt0_is_only_an_intermediate_route_token():
    tx,terms,_=example(local_override="0xc2132d05d31c914a87c6611c10748aeb04b58e8f")
    result=inspect_across_v4(tx,terms=terms,now=101)
    assert result["destination_swap"]["minimum_final"]==5*10**18
    assert terms["source_token"]==ETHEREUM_STABLES["USDC"]
    tx,terms,_=example(local_override="0x"+"99"*20)
    with pytest.raises(ValueError):
        inspect_across_v4(tx,terms=terms,now=101)


def test_automatic_sell_cannot_fall_back_to_unrelated_native_coin():
    from backend.ethereum_settlement_quote import _source_token
    from fastapi import HTTPException
    from decimal import Decimal
    balances={"POL":{"status":"verified","balance":"100"},
              "WPOL":{"status":"verified","balance":"2"}}
    assert _source_token("POL",Decimal("2"),balances,wrapped_only=True).lower()==CROSS_COIN_CONTRACTS["POL"]
    with pytest.raises(HTTPException):
        _source_token("POL",Decimal("3"),balances,wrapped_only=True)
