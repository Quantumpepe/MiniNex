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


@pytest.mark.parametrize("side",["BUY","SELL"])
@pytest.mark.parametrize("stable",["USDC","USDT"])
def test_local_swap_leg_is_exactly_bound_to_stargate_stable_and_owner(side,stable):
    from backend.ethereum_stargate_staged_execution import staged_swap_transaction,staged_swap_route
    from eth_abi import decode
    from backend.ethereum_settlement_plan import CROSS_COIN_CONTRACTS
    source,target,router,_=staged_swap_route(side,stable)
    coin=CROSS_COIN_CONTRACTS["AVAX"];local=CONFIG[43114]["tokens"][stable]
    assert (source,target)==((local,coin) if side=="BUY" else (coin,local))
    tx=staged_swap_transaction(side=side,stable=stable,wallet=WALLET,
                              amount_atomic=1000000,min_out_atomic=42,fee=3000)
    assert tx["chainId"]==43114 and tx["to"]==router and tx["value"]=="0"
    assert decode(["address","address","uint24","address","uint256","uint256","uint160"],
                  bytes.fromhex(tx["data"][10:]))==(source,target,3000,WALLET,1000000,42,0)


@pytest.mark.parametrize("side,first,second",[("BUY","BRIDGE","SWAP"),("SELL","SWAP","BRIDGE")])
def test_staged_order_uses_exact_credit_and_can_return_all_proceeds(side,first,second):
    from backend.ethereum_stargate_staged_journal import stage_kind,exact_stage_amount
    assert stage_kind(side,1)==first
    assert stage_kind(side,2)==second
    order={"side":side,"stage":1,"original_input_atomic":1000000,
           "remaining_atomic":0,"scope_json":{"buy_source_atomic_cap":"1000000"}}
    assert exact_stage_amount(order)==1000000
    order.update(stage=2,remaining_atomic=1200000)
    assert exact_stage_amount(order)==1000000
    order["remaining_atomic"]=200000
    assert exact_stage_amount(order)==200000
    order["remaining_atomic"]=0
    with pytest.raises(ValueError):exact_stage_amount(order)

def test_staged_claim_rejects_quote_amount_recipient_or_unapproved_allowance():
    from backend.ethereum_stargate_staged_journal import check_stage_prepared
    from backend.ethereum_stargate_staged_policy import bridge_transaction,STARGATE_STAGED_POLICY
    from backend.ethereum_stargate_staged_execution import _call_data
    stable="USDT";side="BUY";scope={"asset":"AVAX","stable":stable,
        "settlement_policy":STARGATE_STAGED_POLICY,"buy_source_atomic_cap":"1000000",
        "sell_source_atomic_cap":"1000000000000000000","bridge_fee_caps_atomic":{"1":"10000","43114":"10000"}}
    order={"side":side,"stable":stable,"wallet_address":WALLET,"scope_json":scope,
           "stage":1,"original_input_atomic":1000000,"remaining_atomic":0}
    tx=bridge_transaction(side=side,stable=stable,wallet=WALLET,amount_atomic=1000000,
                          min_out_atomic=990000,native_fee_atomic=1000)
    prepared={"mode":"AVAX_DIRECT_STARGATE_TAXI_PREPARATION","asset":"AVAX","side":side,
              "stable":stable,"wallet_address":WALLET,"input_atomic":"1000000",
              "source_balance_atomic":"1000000","minimum_output_atomic":"990000",
              "native_bridge_fee_atomic":"1000","source_chain_id":1,"destination_chain_id":43114,
              "source_token":CONFIG[1]["tokens"][stable],"source_allowance_atomic":"1000000",
              "issued_at":100,"transaction":tx,"approval":None}
    assert check_stage_prepared(order,prepared,101)==("BRIDGE",tx)
    for bad in (dict(prepared,input_atomic="1000001"),dict(prepared,transaction=dict(tx,**{"from":OTHER})),
                dict(prepared,source_allowance_atomic="1"),dict(prepared,issued_at=50)):
        with pytest.raises(ValueError):check_stage_prepared(order,bad,101)
    approval={"from":WALLET,"to":CONFIG[1]["tokens"][stable],"chainId":1,"value":"0",
        "data":_call_data("approve(address,uint256)",["address","uint256"],[CONFIG[1]["pools"][stable],0])}
    assert check_stage_prepared(order,dict(prepared,source_allowance_atomic="1",approval=approval),101)==("APPROVAL",approval)



def test_staged_order_identity_is_session_scoped_and_stable_across_requotes():
    from datetime import datetime,timezone,timedelta
    from backend.ethereum_stargate_staged_journal import staged_order_identity
    did="did:privy:owner"
    started=datetime(2026,11,25,14,0,tzinfo=timezone.utc)
    first=staged_order_identity(did,started,120)
    assert first==staged_order_identity(did,started,179)
    assert first==staged_order_identity(did,started.astimezone(timezone(timedelta(hours=1))),120)
    assert first!=staged_order_identity(did,started,180)
    assert first!=staged_order_identity(did,started+timedelta(seconds=1),120)
    assert first!=staged_order_identity("did:privy:other",started,120)
    with pytest.raises(ValueError):
        staged_order_identity(did,started.replace(tzinfo=None),120)


def test_avax_exit_and_pause_follow_original_session(monkeypatch):
    import asyncio
    from datetime import datetime,timezone
    from backend import ethereum_stargate_staged_worker as m
    now=datetime(2026,11,25,tzinfo=timezone.utc)
    scope={"settlement_policy":"ETHEREUM_STARGATE_STAGED_V1"}
    row={"stable":"USDC","budget_revision":2,"started_by_owner_at":now,"session_status":"EXIT_PENDING"}
    order={**row,"order_id":"original","side":"BUY","stage":1,"scope_json":scope}
    seen=[]
    monkeypatch.setattr(m,"read_open_order",lambda *a:order)
    monkeypatch.setattr(m,"read_pending_step",lambda *a:None)
    monkeypatch.setattr(m,"cancel_unspent_buy_for_exit",lambda *a:seen.append("cancel") or True)
    async def audit(*a):seen.append("audit");return {"scope":scope}
    async def prepare(*a):seen.append("prepare");return {}
    async def submit(*a):seen.append("submit");return {"state":"SUBMITTED"}
    monkeypatch.setattr(m,"_audited",audit)
    monkeypatch.setattr(m,"_prepare",prepare)
    monkeypatch.setattr(m,"_submit",submit)
    def run():return asyncio.run(m.advance_avax_order("did:privy:original",row=row,client=None))
    assert run()["state"]=="CANCELLED" and seen==["cancel"]
    seen.clear();order["stage"]=2
    assert run()["state"]=="SUBMITTED" and seen==["audit","prepare","submit"]
    seen.clear();row["session_status"]="PAUSED"
    assert run()["reason"]=="ORIGINAL_SESSION_PAUSED" and seen==[]
