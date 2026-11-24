"""Read-only last-mile inspection of a persisted quote before Privy signing.

No public send endpoint, no approval and no broadcast. The actual worker
must separately prove current user consent, policy and budget reservation.
"""
from eth_abi import decode
from eth_utils import keccak
from fastapi import HTTPException
from .ethereum_settlement_calldata import DIAMOND, inspect_settlement_route
from .ethereum_settlement_bridge_proof import POOLS
from .ethereum_settlement_status import _rpc_url
from .shared_evm_swap_receipts import _receipt_rpc
from .live_eth_quotes import ETH_NATIVE
from .shared_evm_execution import WALLET_ID

async def prepare_bridge_privy_request(client,quote,*,wallet_id,now,manual=False):
    if not WALLET_ID.fullmatch(wallet_id) or not quote["issued_at"]<=now<quote["expires_at"]:
        raise HTTPException(409,"Original bridge quote or wallet unavailable")
    if not quote.get("unsigned_transaction"):
        raise HTTPException(409,"Route is indicative only")
    inspected=inspect_settlement_route(quote["unsigned_transaction"],terms=quote,now=now)
    if inspected["route_sha256"]!=quote["route_sha256"]:
        raise ValueError("Original bridge route digest changed")
    tx=inspected["transaction"]; chain=tx["chainId"]; url=_rpc_url(chain)
    async def rpc(method,params,rid):
        return await _receipt_rpc(client,url,method,params,rid)
    if int(await rpc("eth_chainId",[],1),16)!=chain:
        raise ValueError("Wrong source RPC chain")
    selector=bytes.fromhex(tx["data"][2:10])
    loupe="0x"+(keccak(text="facetAddress(bytes4)")[:4]+selector+bytes(28)).hex()
    facet=decode(["address"],bytes.fromhex((await rpc("eth_call",
        [{"to":DIAMOND,"data":loupe},"latest"],2))[2:]))[0].lower()
    if facet!=inspected["facet"]:
        raise HTTPException(409,"Bridge facet differs from reviewed deployment")
    destination=inspected.get("destination_swap")
    stargate=inspected["provider"]=="STARGATE_V2"
    if stargate:
        from .ethereum_settlement_stargate import CONFIG,ENDPOINT
        config=CONFIG[chain]
        raw=await rpc("eth_call",[{"to":facet,"data":"0x"+keccak(text="tokenMessaging()")[:4].hex()},"latest"],10)
        if decode(["address"],bytes.fromhex(raw[2:]))[0].lower()!=config["messaging"]:
            raise HTTPException(409,"Stargate token messaging differs")
        data="0x"+keccak(text="stargateImpls(uint16)")[:4].hex()+format(inspected["asset_id"],"064x")
        raw=await rpc("eth_call",[{"to":config["messaging"],"data":data},"latest"],11)
        if decode(["address"],bytes.fromhex(raw[2:]))[0].lower()!=config["pools"][quote["stable"]]:
            raise HTTPException(409,"Stargate pool differs")
    if destination:
        dst=quote["destination_chain_id"]; dst_url=_rpc_url(dst)
        if int(await _receipt_rpc(client,dst_url,"eth_chainId",[],20),16)!=dst:
            raise ValueError("Wrong destination RPC")
        checks=(("endpointV2()",ENDPOINT,21),("executor()",destination["executor"],22),
                ("tokenMessaging()",CONFIG[dst]["messaging"],23)) if stargate else (
                ("SPOKEPOOL()",POOLS[dst],21),("EXECUTOR()",destination["executor"],22))
        for method,expected,rid in checks:
            raw=await _receipt_rpc(client,dst_url,"eth_call",[
                {"to":destination["receiver"],"data":"0x"+keccak(text=method)[:4].hex()},"latest"],rid)
            if decode(["address"],bytes.fromhex(raw[2:]))[0].lower()!=expected:
                raise HTTPException(409,"Destination receiver deployment differs")
    source=quote["source_token"]; amount=int(quote["input_atomic"])
    owner=tx["from"][2:].rjust(64,"0")
    native=int(await rpc("eth_getBalance",[tx["from"],"latest"],3),16)
    balance=native
    if source!=ETH_NATIVE:
        balance=int(await rpc("eth_call",[{"to":source,"data":"0x70a08231"+owner},"latest"],4),16)
        allowance=int(await rpc("eth_call",[{"to":source,"data":"0xdd62ed3e"+owner+DIAMOND[2:].rjust(64,"0")},"latest"],5),16)
        if balance<amount:
            raise HTTPException(409,"Exact source funds unavailable")
        if allowance<amount:
            if not manual:
                raise HTTPException(409,"Exact bridge allowance unavailable")
            # USDT on Ethereum requires zero before a nonzero change.
            from .live_eth_quotes import STABLES
            approval_amount=0 if chain==1 and source.lower()==STABLES["USDT"].lower() and allowance>0 else amount
            data="0x095ea7b3"+DIAMOND[2:].rjust(64,"0")+format(approval_amount,"064x")
            approval={"from":tx["from"],"to":source,"data":data,"value":"0x0"}
            gas=int(await rpc("eth_estimateGas",[approval],8),16)
            price=int(await rpc("eth_gasPrice",[],9),16)
            if gas<=0 or price<=0 or native<gas*price:
                raise HTTPException(409,"Source gas funds insufficient")
            return {"manual_approval":approval,"estimated_gas_atomic":str(gas*price),
                    "execution_authorized":False}
    envelope={"from":tx["from"],"to":tx["to"],"data":tx["data"],"value":hex(int(tx["value"]))}
    # Estimate executes the exact transaction against current on-chain state
    # without sending it; a revert or missing allowance is never hidden.
    gas=int(await rpc("eth_estimateGas",[envelope],6),16)
    price=int(await rpc("eth_gasPrice",[],7),16)
    if gas<=0 or price<=0 or native<int(tx["value"])+gas*price:
        raise HTTPException(409,"Source gas funds insufficient")
    return {"url":"https://api.privy.io/v1/wallets/"+wallet_id+"/rpc",
            "body":{"method":"eth_sendTransaction","caip2":"eip155:"+str(chain),
                    "params":{"transaction":{"to":tx["to"],"data":tx["data"],"value":tx["value"]}}},
            "quote_id":str(quote["quote_id"]),"route_sha256":inspected["route_sha256"],
            "estimated_gas_atomic":str(gas*price),
            "native_bridge_fee_atomic":str(inspected["native_bridge_fee_atomic"]),
            "verified_source_balance_atomic":str(balance),
            "execution_authorized":False}
