"""Exact Ethereum/Avalanche Stargate V2 LI.FI routes.

Official lifinance/contracts StargateFacetV2, ReceiverStargateV2 and
stargate-protocol/stargate-v2 deployment manifests. Only taxi sends, known
USDC/USDT pools and one exact Router02 swap. No opaque arbitrary calls.
"""
from eth_abi import decode,encode
from eth_utils import keccak
from .ethereum_settlement_calldata import (
    BRIDGE,SWAP,DIAMOND,EXACT_SINGLE,normalized_transaction,route_digest,_word_address,
)
from .ethereum_settlement_plan import ETHEREUM_STABLES,CROSS_COIN_CONTRACTS,check_evm_quote_route
from .native_evm_v3_quote import CHAINS
from .live_eth_quotes import ETH_NATIVE
from .ethereum_settlement_bridge_proof import address_word,word

STARGATE="(uint16,(uint32,bytes32,uint256,uint256,bytes,bytes,bytes),(uint256,uint256),address)"
DIRECT=keccak(text="startBridgeTokensViaStargate("+BRIDGE+","+STARGATE+")")[:4]
WITH_SWAP=keccak(text="swapAndStartBridgeTokensViaStargate("+BRIDGE+","+SWAP+"[],"+STARGATE+")")[:4]
ENDPOINT="0x1a44076050125825900e736c501f859c50fe728c"
AVAX_STABLES={"USDC":"0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e",
              "USDT":"0x9702230a8ea53601f5cd2dc00fdbc13d4df4a8c7"}
CONFIG={
    1:{"eid":30101,"facet":"0xbf4ad13fa0e6e05916a78c201f147c5152dbe1c9",
       "messaging":"0x6d6620efa72948c5f68a3c8646d58c00d3f4a980",
       "receiver":"0xb539b40793171211dca8834da044fc14bce64bdc",
       "executor":"0xd9b2da9c45b118e4e93a004fb1452bcdb6cc0e88",
       "tokens":ETHEREUM_STABLES,"pools":{
           "USDC":"0xc026395860db2d07ee33e05fe50ed7bd583189c7",
           "USDT":"0x933597a323eb81cae705c5bc29985172fd5a3973"}},
    43114:{"eid":30106,"facet":"0x6e378c84e657c57b2a8d183cff30ee5cc8989b61",
       "messaging":"0x17e450be3ba9557f2378e20d64ad417e59ef9a34",
       "receiver":"0x1493e7b8d4dfade0a178dad9335470337a3a219a",
       "executor":"0x2dfadab8266483bed9fd9a292ce56596a2d1378d",
       "tokens":AVAX_STABLES,"pools":{
           "USDC":"0x5634c4a5fed09819e3c46d86a965dd9447d86e47",
           "USDT":"0x12dc9256acc9895b076f6638d628382881e62cee"}},
}
def _options(raw):
    # LayerZero Type 3 executor gas only; no native drop or arbitrary worker.
    if not raw:
        return
    if raw[:2]!=b"\x00\x03":
        raise ValueError("Unreviewed LayerZero options")
    i=2
    seen=set()
    while i<len(raw):
        if len(raw)-i<4 or raw[i]!=1:
            raise ValueError("Unreviewed LayerZero worker")
        size=int.from_bytes(raw[i+1:i+3],"big")
        end=i+3+size
        if size<1 or end>len(raw):
            raise ValueError("Malformed LayerZero option")
        kind=raw[i+3]; data=raw[i+4:end]; i=end
        if kind in seen:
            raise ValueError("Duplicate LayerZero option")
        seen.add(kind)
        if kind==3:
            if len(data)<2 or data[:2]!=bytes(2):
                raise ValueError("Unexpected compose index")
            data=data[2:]
        elif kind!=1:
            raise ValueError("Unreviewed LayerZero option")
        if len(data) not in (16,32) or int.from_bytes(data[:16],"big")==0 or any(data[16:]):
            raise ValueError("Unexpected extra native delivery")

def _swap(row,*,sending,receiving,recipient,deposit,amount=None,minimum):
    router=CHAINS["AVAX"].router.lower()
    call_to,approve_to,source,target,spend,data,requires_deposit=row
    if (call_to.lower()!=router or approve_to.lower()!=router
        or source.lower()!=sending or target.lower()!=receiving
        or requires_deposit!=deposit or spend<=0 or (amount is not None and spend!=amount)
        or len(data)!=228 or data[:4]!=EXACT_SINGLE):
        raise ValueError("Unreviewed Avalanche swap")
    a,b,fee,to,input_amount,minout,limit=decode(
        ["address","address","uint24","address","uint256","uint256","uint160"],data[4:],strict=True)
    if (a.lower()!=(CROSS_COIN_CONTRACTS["AVAX"] if sending==ETH_NATIVE else sending)
        or b.lower()!=receiving or to.lower()!=recipient or fee not in (100,500,3000,10000)
        or input_amount!=spend or minout<minimum or limit!=0):
        raise ValueError("Avalanche swap token, recipient or amount differs")
    return spend,minout

def inspect_stargate(tx,*,terms,now):
    route=check_evm_quote_route(terms)
    src,dst=route["source_chain_id"],route["destination_chain_id"]
    if terms["asset"]!="AVAX" or src not in CONFIG or dst not in CONFIG:
        raise ValueError("Unsupported Stargate settlement")
    wallet=terms["wallet_address"].lower(); tx=normalized_transaction(tx,src)
    if tx["from"]!=wallet or tx["to"]!=DIAMOND:
        raise ValueError("Wrong Stargate sender or router")
    amount=int(terms["input_atomic"]); minimum=int(terms["minimum_output_atomic"])
    stable=terms["stable"]; source=terms["source_token"].lower(); final=terms["destination_token"].lower()
    raw=bytes.fromhex(tx["data"][2:]); selector=raw[:4]
    if selector==DIRECT: types=[BRIDGE,STARGATE]
    elif selector==WITH_SWAP: types=[BRIDGE,SWAP+"[]",STARGATE]
    else: raise ValueError("Unsupported Stargate selector")
    values=decode(types,raw[4:],strict=True)
    if encode(types,values)!=raw[4:]:
        raise ValueError("Noncanonical Stargate calldata")
    tid,_,_,_,bridge_token,receiver,bridge_min,chain,source_swap,dest_call=values[0]
    asset_id,send,fee,refund=values[-1]
    eid,to,send_amount,send_min,options,message,cmd=send
    native_fee,lz_fee=fee
    bridge_in=CONFIG[src]["tokens"][stable]
    bridge_out=CONFIG[dst]["tokens"][stable]
    if (tid==bytes(32) or receiver.lower()!=wallet or refund.lower()!=wallet
        or bridge_token.lower()!=bridge_in or chain!=dst or eid!=CONFIG[dst]["eid"]
        or asset_id<=0 or send_amount!=bridge_min or send_min<=0 or send_min>send_amount
        or lz_fee!=0 or cmd or bool(message)!=dest_call or source_swap!=(selector==WITH_SWAP)):
        raise ValueError("Stargate owner, token, amount or destination differs")
    _options(options)
    buy=terms["side"]=="BUY"
    destination=None
    if buy:
        if source_swap or not dest_call or source!=bridge_in or final!=CROSS_COIN_CONTRACTS["AVAX"] or bridge_min!=amount:
            raise ValueError("Unsupported Avalanche BUY route")
        cfg=CONFIG[dst]
        if _word_address(to)!=cfg["receiver"]:
            raise ValueError("Unexpected Stargate destination receiver")
        types=["bytes32",SWAP+"[]","address"]
        decoded=decode(types,message,strict=True)
        if encode(types,decoded)!=message or decoded[0]!=tid or decoded[2].lower()!=wallet or len(decoded[1])!=1:
            raise ValueError("Stargate compose owner or payload differs")
        spend,minout=_swap(decoded[1][0],sending=bridge_out,receiving=final,
            recipient=cfg["executor"],deposit=False,minimum=minimum)
        if not send_min<=spend<=send_amount:
            raise ValueError("Compose input differs from bridged amount")
        destination={"receiver":cfg["receiver"],"executor":cfg["executor"],
            "transaction_id":tid,"compose_message":message,"bridge_amount":spend,"minimum_final":minout}
    else:
        if not source_swap or dest_call or final!=bridge_out or _word_address(to)!=wallet or len(values[1])!=1:
            raise ValueError("Unsupported Avalanche SELL route")
        if send_min<minimum:
            raise ValueError("Stargate minimum below requested output")
        _swap(values[1][0],sending=source,receiving=bridge_in,
            recipient=DIAMOND,deposit=True,amount=amount,minimum=bridge_min)
    if int(tx["value"])!=native_fee+(amount if source==ETH_NATIVE else 0):
        raise ValueError("Stargate source native fee differs")
    return {"provider":"STARGATE_V2","transaction":tx,"route_sha256":route_digest(tx,src),
        "facet":CONFIG[src]["facet"],"asset_id":asset_id,"bridge_source_token":bridge_in,
        "bridge_destination_token":bridge_out,"bridge_minimum_atomic":bridge_min,
        "bridge_min_received":send_min,"native_bridge_fee_atomic":native_fee,
        "source_swap":source_swap,"destination_swap":destination,"stable":stable,
        "minimum_output_atomic":minimum,"calldata_verified":True,"execution_authorized":False}

def _event(logs,address,signature,types,indexed=0):
    topic="0x"+keccak(text=signature).hex()
    found=[]
    for event in logs:
        topics=event.get("topics",[])
        if str(event.get("address","")).lower()!=address or not topics or topics[0].lower()!=topic:
            continue
        if event.get("removed") is True or len(topics)!=indexed+1:
            raise ValueError("Malformed Stargate event")
        raw=bytes.fromhex(event["data"][2:]); values=decode(types,raw,strict=True)
        if encode(types,values)!=raw:
            raise ValueError("Noncanonical Stargate event")
        found.append((tuple(word(x) for x in topics[1:]),values))
    if len(found)!=1:
        raise ValueError("Original Stargate event not uniquely proven")
    return found[0]

def prove_stargate_pair(source_logs,destination_logs,*,src,dst,wallet,route,received):
    stable=route["stable"]; cfg=CONFIG[dst]; pool=cfg["pools"][stable]
    ((guid,sender),(eid,sent,credited))=_event(source_logs,CONFIG[src]["pools"][stable],
        "OFTSent(bytes32,uint32,address,uint256,uint256)",["uint32","uint256","uint256"],2)
    if (guid==bytes(32) or sender!=address_word(DIAMOND) or eid!=cfg["eid"]
        or sent<route["bridge_minimum_atomic"] or credited<route["bridge_min_received"]
        or (not route["source_swap"] and sent!=route["bridge_minimum_atomic"])
        or received<route["minimum_output_atomic"]):
        raise ValueError("Original Stargate send differs")
    destination=route["destination_swap"]
    if destination:
        # LayerZero authenticates a compose call by the destination pool,
        # receiver, source GUID and index; the actual output credit must match.
        _,(sender,receiver,compose_guid,index)=_event(destination_logs,ENDPOINT,
            "ComposeDelivered(address,address,bytes32,uint16)",["address","address","bytes32","uint16"])
        if (sender.lower()!=pool or receiver.lower()!=destination["receiver"]
            or compose_guid!=guid or index!=0 or credited!=destination["bridge_amount"]
            or received<destination["minimum_final"]):
            raise ValueError("Original Stargate compose differs")
        ((tid,),values)=_event(destination_logs,destination["executor"],
            "LiFiTransferCompleted(bytes32,address,address,uint256,uint256)",
            ["address","address","uint256","uint256"],1)
        if tid!=destination["transaction_id"] or values[0].lower()!=route["bridge_destination_token"] or values[1].lower()!=wallet:
            raise ValueError("Original Stargate completion differs")
    else:
        ((dst_guid,to),(origin_eid,actual))=_event(destination_logs,pool,
            "OFTReceived(bytes32,uint32,address,uint256)",["uint32","uint256"],2)
        if dst_guid!=guid or to!=address_word(wallet) or origin_eid!=CONFIG[src]["eid"] or actual!=credited or actual!=received:
            raise ValueError("Original Stargate credit differs")
    return {"deposit_id":str(int.from_bytes(guid,"big")),"bridge_message_verified":True}
