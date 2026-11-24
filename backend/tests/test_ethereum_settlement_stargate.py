"""Exact AVAX route fixtures: no RPC, no transaction, no fabricated live quote."""
from eth_abi import encode
from eth_utils import keccak
import pytest
from backend.ethereum_settlement_calldata import BRIDGE,SWAP,DIAMOND,EXACT_SINGLE,inspect_settlement_route
from backend.ethereum_settlement_stargate import CONFIG,STARGATE,DIRECT,WITH_SWAP,ENDPOINT,prove_stargate_pair
from backend.ethereum_settlement_plan import CROSS_COIN_CONTRACTS,ETHEREUM_STABLES
from backend.native_evm_v3_quote import CHAINS
from backend.live_eth_quotes import ETH_NATIVE

W="0x"+"12"*20
T=bytes.fromhex("ab"*32)
G=bytes.fromhex("cd"*32)
FEE=1234
COIN=CROSS_COIN_CONTRACTS["AVAX"]
ROUTER=CHAINS["AVAX"].router.lower()
def word(a):return bytes.fromhex(a[2:].rjust(64,"0"))
def tx(side,stable,*,bad_receiver=False):
    buy=side=="BUY"
    src,dst=(1,43114) if buy else (43114,1)
    s,d=CONFIG[src],CONFIG[dst]
    source=s["tokens"][stable] if buy else COIN
    target=COIN if buy else d["tokens"][stable]
    amount=10_000_000 if buy else 10**18
    minimum=10**17 if buy else 8_000_000
    bridge_min=amount if buy else 9_500_000
    bridge_received=9_000_000
    router_in=d["tokens"][stable] if buy else COIN
    router_out=COIN if buy else s["tokens"][stable]
    spend=bridge_received if buy else amount
    router_min=minimum if buy else bridge_min
    recipient=(W if bad_receiver else d["executor"]) if buy else DIAMOND
    data=EXACT_SINGLE+encode(["address","address","uint24","address","uint256","uint256","uint160"],
        [router_in,router_out,3000,recipient,spend,router_min,0])
    swaps=[(ROUTER,ROUTER,router_in,router_out,spend,data,not buy)]
    message=encode(["bytes32",SWAP+"[]","address"],[T,swaps,W]) if buy else b""
    b=(T,"stargateV2","mininex",W,s["tokens"][stable],W,bridge_min,dst,not buy,buy)
    sg=(1,(d["eid"],word(d["receiver"] if buy else W),bridge_min,bridge_received,b"",message,b""),(FEE,0),W)
    raw=(DIRECT+encode([BRIDGE,STARGATE],[b,sg])) if buy else (WITH_SWAP+encode([BRIDGE,SWAP+"[]",STARGATE],[b,swaps,sg]))
    unsigned={"from":W,"to":DIAMOND,"chainId":src,"data":"0x"+raw.hex(),"value":str(FEE)}
    terms=dict(asset="AVAX",side=side,stable=stable,wallet_address=W,source_chain_id=src,
        destination_chain_id=dst,source_token=source,destination_token=target,
        input_atomic=str(amount),minimum_output_atomic=str(minimum))
    return unsigned,terms

def event(address,signature,types,values,topics=()):
    return {"address":address,"topics":["0x"+keccak(text=signature).hex(),*("0x"+t.hex() for t in topics)],
        "data":"0x"+encode(types,values).hex(),"removed":False}

@pytest.mark.parametrize("stable",["USDC","USDT"])
@pytest.mark.parametrize("side",["BUY","SELL"])
def test_avalanche_exact_buy_sell_and_original_guid(stable,side):
    unsigned,terms=tx(side,stable)
    route=inspect_settlement_route(unsigned,terms=terms,now=101)
    assert route["provider"]=="STARGATE_V2"
    assert route["native_bridge_fee_atomic"]==FEE
    assert not route["execution_authorized"]
    src,dst=terms["source_chain_id"],terms["destination_chain_id"]
    s,d=CONFIG[src],CONFIG[dst]
    sent=route["bridge_minimum_atomic"]
    credit=9_000_000
    source=[event(s["pools"][stable],"OFTSent(bytes32,uint32,address,uint256,uint256)",
        ["uint32","uint256","uint256"],[d["eid"],sent,credit],(G,word(DIAMOND)))]
    if side=="BUY":
        received=10**17
        destination=[
            event(ENDPOINT,"ComposeDelivered(address,address,bytes32,uint16)",
                ["address","address","bytes32","uint16"],[d["pools"][stable],d["receiver"],G,0]),
            event(d["executor"],"LiFiTransferCompleted(bytes32,address,address,uint256,uint256)",
                ["address","address","uint256","uint256"],[d["tokens"][stable],W,received,102],(T,))]
    else:
        received=credit
        destination=[event(d["pools"][stable],"OFTReceived(bytes32,uint32,address,uint256)",
            ["uint32","uint256"],[s["eid"],credit],(G,word(W)))]
    assert prove_stargate_pair(source,destination,src=src,dst=dst,wallet=W,route=route,
        received=received)["deposit_id"]==str(int.from_bytes(G,"big"))
    with pytest.raises(ValueError):
        prove_stargate_pair(source,destination,src=src,dst=dst,wallet=W,route=route,received=1)
    with pytest.raises(ValueError):
        inspect_settlement_route({**unsigned,"value":"0"},terms=terms,now=101)

def test_wrong_destination_router_recipient_refused():
    unsigned,terms=tx("BUY","USDC",bad_receiver=True)
    with pytest.raises(ValueError):
        inspect_settlement_route(unsigned,terms=terms,now=101)
