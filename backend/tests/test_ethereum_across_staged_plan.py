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
