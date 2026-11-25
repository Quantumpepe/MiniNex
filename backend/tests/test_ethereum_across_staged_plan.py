import pytest
from eth_abi import encode,decode
from backend.ethereum_across_staged_plan import (
    staged_route,bridge_terms,inspect_staged_bridge,staged_local_transaction,build_staged_across_policy,
)
from backend.ethereum_settlement_calldata import DIRECT,BRIDGE,ACROSS,DIAMOND
from backend.ethereum_settlement_plan import ETHEREUM_STABLES,CROSS_COIN_CONTRACTS

WALLET="0x"+"a"*40
OTHER="0x"+"b"*40
def word(address):return bytes.fromhex(address[2:]).rjust(32,b"\x00")

@pytest.mark.parametrize("asset,chain,precision",[("BNB",56,18),("POL",137,6),("ARB",42161,6)])
@pytest.mark.parametrize("stable",["USDC","USDT"])
def test_staged_routes_return_to_ethereum_and_keep_exact_local_units(asset,chain,precision,stable):
    buy=staged_route(asset,"BUY",stable);sell=staged_route(asset,"SELL",stable)
    assert buy["bridge_source"]==(1,ETHEREUM_STABLES[stable],6)
    assert sell["bridge_target"]==buy["bridge_source"]
    assert buy["bridge_target"]==buy["swap_source"]==sell["swap_target"]==sell["bridge_source"]
    assert buy["bridge_target"][0]==chain and buy["bridge_target"][2]==precision
    assert buy["swap_target"]==(chain,CROSS_COIN_CONTRACTS[asset],18)
    assert sell["swap_source"]==buy["swap_target"]
    assert buy["stages"]==("BRIDGE","SWAP") and sell["stages"]==("SWAP","BRIDGE")

@pytest.mark.parametrize("asset",["BNB","POL","ARB"])
@pytest.mark.parametrize("side",["BUY","SELL"])
def test_staged_local_swap_encodes_only_canonical_owner_and_transit_token(asset,side):
    route=staged_route(asset,side,"USDC")
    tx=staged_local_transaction(asset,side,"USDC",WALLET,1000,42,3000)
    assert tx["chainId"]==route["swap_source"][0] and tx["value"]=="0"
    assert decode(["address","address","uint24","address","uint256","uint256","uint160"],
        bytes.fromhex(tx["data"][10:]))==(route["swap_source"][1],route["swap_target"][1],3000,WALLET,1000,42,0)

@pytest.mark.parametrize("asset",["BNB","POL","ARB"])
@pytest.mark.parametrize("side",["BUY","SELL"])
def test_staged_bridge_only_accepts_direct_exact_owner_and_token(asset,side):
    terms=bridge_terms(asset,side,"USDT",WALLET,1000,900,100)
    src,dst=terms["source_token"],terms["destination_token"]
    bridge=(bytes.fromhex("11"*32),"across","mininex","0x"+"0"*40,src,WALLET,
            1000,terms["destination_chain_id"],False,False)
    across=(word(WALLET),word(WALLET),word(src),word(dst),950,10**18,bytes(32),100,1000,0,b"")
    def tx(a):
        return {"chainId":terms["source_chain_id"],"from":WALLET,"to":DIAMOND,"value":"0",
                "data":"0x"+(DIRECT+encode([BRIDGE,ACROSS],[bridge,a])).hex()}
    assert inspect_staged_bridge(terms,tx(across),now=101)["direct_output_atomic"]==950
    for bad in ((word(OTHER),*across[1:]),(across[0],word(OTHER),*across[2:]),
                (*across[:3],word(OTHER),*across[4:])):
        with pytest.raises(ValueError):inspect_staged_bridge(terms,tx(bad),now=101)
    with pytest.raises(ValueError):inspect_staged_bridge(terms,tx(across),now=131)

@pytest.mark.parametrize("asset",["BNB","POL","ARB"])
def test_policy_proposal_has_separate_exact_caps_and_no_live_authorization(asset):
    draft=build_staged_across_policy(asset=asset,stable="USDT",wallet=WALLET,
        owner_id="owner_123",executor_id="signer_123",eth_buy_atomic_cap="1000000",
        coin_sell_atomic_cap="2000000000000000000",transit_atomic_cap="3000000000000000000",
        expires_at=1000,now=100)
    assert draft["can_submit"] is False and draft["remote_policy_verified"] is False
    rules=draft["policy"]["rules"]
    assert len({r["name"] for r in rules})==len(rules)
    assert all(r["action"]=="ALLOW" and r["conditions"] for r in rules)
    assert not any("swapAndStartBridge" in str(r) for r in rules)
    for label,cap in (("BUY USDT staged direct bridge","1000000"),
                      ("BUY USDT staged local swap","3000000000000000000"),
                      ("SELL USDT staged local swap","2000000000000000000"),
                      ("SELL USDT staged direct bridge","3000000000000000000")):
        rule=next(r for r in rules if r["name"]==f"MiniNex {asset} {label}")
        assert any(c.get("operator")=="lte" and c.get("value")==cap for c in rule["conditions"])


@pytest.mark.parametrize("asset",["BNB","POL","ARB"])
def test_staged_identity_and_transit_split_stay_bound_to_original_coin(asset):
    from datetime import datetime,timezone,timedelta
    from backend.ethereum_across_staged_journal import staged_order_identity,exact_stage_amount
    start=datetime(2026,11,25,tzinfo=timezone.utc)
    did="did:privy:original"
    first=staged_order_identity(did,asset,start,120)
    assert first==staged_order_identity(did,asset,start,179)
    assert first!=staged_order_identity(did,asset,start+timedelta(seconds=1),120)
    assert first!=staged_order_identity(did,"ARB" if asset!="ARB" else "POL",start,120)
    order={"stage":2,"side":"SELL","remaining_atomic":130,"scope_json":{"transit_atomic_cap":"100"}}
    assert exact_stage_amount(order)==100
    assert exact_stage_amount(dict(order,remaining_atomic=30))==30

def test_gas_is_counted_per_chain_not_across_incompatible_units():
    from backend.ethereum_across_staged_journal import stage_gas_by_chain
    rows=[{"chain_id":1,"proof":{"source_gas_atomic":"100"}},
          {"chain_id":56,"proof":{"source_gas_atomic":"500"}},
          {"chain_id":1,"proof":{"source_gas_atomic":"20"}}]
    assert stage_gas_by_chain(rows)=={1:120,56:500}
    with pytest.raises(ValueError):
        stage_gas_by_chain([{"chain_id":1,"proof":{"source_gas_atomic":True}}])

def test_final_stage_requires_original_hash_and_real_bridge_proof():
    from backend.ethereum_across_staged_journal import validate_final_stage_proof
    source="0x"+"1"*64;dest="0x"+"2"*64
    current={"kind":"BRIDGE","chain_id":1,"tx_hash":source}
    proof={"state":"BRIDGE_CREDIT_VERIFIED","source_hash":source,"destination_hash":dest,
           "source_gas_atomic":"100","bridge_message_verified":True,"deposit_id":"4",
           "destination_credit_verified":True,"received_atomic":"1000"}
    assert validate_final_stage_proof(current,proof)=="BRIDGE_CREDIT_VERIFIED"
    for change in ({"source_hash":dest},{"bridge_message_verified":False},{"destination_hash":None},
                   {"deposit_id":"-1"},{"received_atomic":"0"},{"source_gas_atomic":"-1"}):
        with pytest.raises(ValueError):
            validate_final_stage_proof(current,{**proof,**change})


@pytest.mark.parametrize("asset,chain",[("BNB",56),("POL",137),("ARB",42161)])
def test_destination_scan_is_exact_finalized_and_resumable(monkeypatch,asset,chain):
    import asyncio
    from backend import ethereum_across_staged_execution as m
    from backend.ethereum_settlement_bridge_proof import POOLS,FILL_TOPIC
    prepared={"destination_start_block":100,"terms":{"source_chain_id":1,"destination_chain_id":chain}}
    h="0x"+"1"*64
    expected=[FILL_TOPIC,"0x"+format(1,"064x"),"0x"+format(7,"064x")]
    event={"address":POOLS[chain],"topics":expected+["0x"+"2"*64],
           "blockNumber":hex(150),"transactionHash":h}
    calls=[]
    logs=[]
    async def rpc(client,url,method,args,rid):
        calls.append((method,args))
        if method=="eth_chainId":return hex(chain)
        if method=="eth_getBlockByNumber":return {"number":hex(200)}
        if method=="eth_getLogs":return list(logs)
        raise AssertionError(method)
    monkeypatch.setattr(m,"_receipt_rpc",rpc)
    monkeypatch.setattr(m,"_rpc_url",lambda _: "https://example.invalid")
    def scan(start=100):
        return asyncio.run(m.find_staged_across_destination(None,prepared=prepared,deposit_id="7",start_block=start))
    assert scan()=={"destination_hash":None,"next_block":201}
    assert calls[-1][1][0]["topics"]==expected
    assert calls[-1][1][0]["toBlock"]==hex(200)
    logs.append(event)
    assert scan()=={"destination_hash":h,"next_block":100}
    logs.append(event)
    with pytest.raises(ValueError):scan()
    with pytest.raises(ValueError):scan(99)
