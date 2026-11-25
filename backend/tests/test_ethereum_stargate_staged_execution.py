from eth_abi import encode
from eth_utils import keccak
import pytest
from backend.ethereum_stargate_staged_execution import (
    decode_oft_quote,decode_native_fee,prove_direct_taxi_credit,
)
from backend.ethereum_settlement_stargate import CONFIG

WALLET="0x"+"a"*40
GUID="0x"+"1"*64
OTHER="0x"+"b"*40
def word(addr):return "0x"+addr[2:].rjust(64,"0")
def event(address,signature,topics,types,values):
    return {"address":address,"topics":["0x"+keccak(text=signature).hex(),*topics],
            "data":"0x"+encode(types,values).hex()}
def transfer(token,src,dst,amount):
    return event(token,"Transfer(address,address,uint256)",[word(src),word(dst)],["uint256"],[amount])

def test_exact_oft_quote_and_native_fee():
    raw="0x"+encode(["(uint256,uint256)","(int256,string)[]","(uint256,uint256)"],
                    [(1,10000000),[(1000,"fee")],(1000000,999000)]).hex()
    out=decode_oft_quote(raw,1000000)
    assert out=={"sent_atomic":1000000,"expected_atomic":999000,"minimum_atomic":994005}
    with pytest.raises(ValueError):decode_oft_quote(raw,2000000)
    with pytest.raises(ValueError):decode_oft_quote(raw+"00",1000000)
    assert decode_native_fee("0x"+encode(["(uint256,uint256)"],[(1234,0)]).hex())==1234
    with pytest.raises(ValueError):
        decode_native_fee("0x"+encode(["(uint256,uint256)"],[(1234,1)]).hex())

@pytest.mark.parametrize("stable",["USDC","USDT"])
@pytest.mark.parametrize("side,src,dst",[("BUY",1,43114),("SELL",43114,1)])
def test_direct_guid_credit_rejects_wrong_recipient_debit_or_amount(stable,side,src,dst):
    pool=CONFIG[src]["pools"][stable];target=CONFIG[dst]["pools"][stable]
    token=CONFIG[src]["tokens"][stable];output=CONFIG[dst]["tokens"][stable]
    source=[transfer(token,WALLET,pool,1000000),
        event(pool,"OFTSent(bytes32,uint32,address,uint256,uint256)",[GUID,word(WALLET)],
              ["uint32","uint256","uint256"],[CONFIG[dst]["eid"],1000000,999000])]
    dest=[transfer(output,target,WALLET,999000),
        event(target,"OFTReceived(bytes32,uint32,address,uint256)",[GUID,word(WALLET)],
              ["uint32","uint256"],[CONFIG[src]["eid"],999000])]
    kwargs=dict(side=side,stable=stable,wallet=WALLET,amount=1000000,minimum=990000)
    result=prove_direct_taxi_credit(source,dest,**kwargs)
    assert result["guid"]==GUID and result["received_atomic"]=="999000"
    for bad in ([transfer(output,target,OTHER,999000),dest[1]],
                [transfer(output,target,WALLET,998999),dest[1]],
                [dest[0],dict(dest[1],topics=[dest[1]["topics"][0],"0x"+"2"*64,word(WALLET)])],
                dest+[dest[1]]):
        with pytest.raises(ValueError):prove_direct_taxi_credit(source,bad,**kwargs)
    with pytest.raises(ValueError):prove_direct_taxi_credit(source[1:],dest,**kwargs)
