import pytest
from eth_abi import encode
from backend.ethereum_settlement_calldata import (
    ACROSS, BRIDGE, DIRECT, DIAMOND, inspect_across_v4, route_digest,
)
from backend.live_eth_quotes import STABLES

WALLET="0x"+"12"*20
OTHER="0x"+"34"*20
TOKEN="0x912ce59144191c1204e64559fe8253a0e49e6548"

def word(addr):
    return bytes.fromhex(addr[2:].rjust(64,"0"))

def test_direct_across_binds_refund_destination_minimum_and_exact_calldata():
    stable=STABLES["USDC"].lower()
    terms=dict(wallet_address=WALLET,source_chain_id=1,destination_chain_id=42161,
        source_token=stable,destination_token=TOKEN,input_atomic="1000000",
        minimum_output_atomic="500000000000000000")
    bridge=(bytes.fromhex("ab"*32),"across","mininex",OTHER,stable,WALLET,
        1000000,42161,False,False)
    across=(word(WALLET),word(WALLET),word(stable),word(TOKEN),
        500000000000000000,0,bytes(32),100,1000,0,b"")
    def tx(b=bridge,a=across):
        return {"from":WALLET,"to":DIAMOND,"chainId":1,"value":"0",
                "data":"0x"+(DIRECT+encode([BRIDGE,ACROSS],[b,a])).hex()}
    result=inspect_across_v4(tx(),terms=terms,now=101)
    assert result["route_sha256"]==route_digest(tx(),1)
    assert result["execution_authorized"] is False
    for a in (
        (across[0],word(OTHER),*across[2:]),
        (*across[:4],1,*across[5:]),
        (*across[:-1],b"arbitrary"),
    ):
        with pytest.raises(ValueError):
            inspect_across_v4(tx(a=a),terms=terms,now=101)
    with pytest.raises(ValueError):
        inspect_across_v4({**tx(),"value":"1"},terms=terms,now=101)


def test_native_pol_source_swap_without_redundant_wrap():
    from backend.ethereum_settlement_calldata import WITH_SWAP,SWAP,EXACT_SINGLE
    from backend.native_evm_v3_quote import CHAINS
    from backend.live_eth_quotes import ETH_NATIVE
    c=CHAINS["POL"]; local=c.stable_contract.lower(); stable=STABLES["USDC"].lower()
    amount=10**18; minimum=500000; bridge_min=510000
    bridge=(bytes.fromhex("ab"*32),"across","mininex",OTHER,local,WALLET,bridge_min,1,True,False)
    across=(word(WALLET),word(WALLET),word(local),word(stable),minimum,10**18,bytes(32),100,1000,0,b"")
    def tx(token):
        swap=EXACT_SINGLE+encode(["address","address","uint24","address","uint256","uint256","uint160"],
            [token,local,3000,DIAMOND,amount,bridge_min,0])
        swaps=[(c.router,c.router,ETH_NATIVE,local,amount,swap,True)]
        return {"from":WALLET,"to":DIAMOND,"chainId":137,"value":str(amount),
            "data":"0x"+(WITH_SWAP+encode([BRIDGE,SWAP+"[]",ACROSS],[bridge,swaps,across])).hex()}
    terms=dict(wallet_address=WALLET,source_chain_id=137,destination_chain_id=1,
        source_token=ETH_NATIVE,destination_token=stable,input_atomic=str(amount),
        minimum_output_atomic=str(minimum))
    assert inspect_across_v4(tx(c.wrapped),terms=terms,now=101)["source_swap"]
    with pytest.raises(ValueError):
        inspect_across_v4(tx(OTHER),terms=terms,now=101)
    with pytest.raises(ValueError):
        inspect_across_v4({**tx(c.wrapped),"value":"0"},terms=terms,now=101)
