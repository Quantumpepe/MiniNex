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
