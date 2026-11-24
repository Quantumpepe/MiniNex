"""Independent Across deposit/fill correlation for the restricted LI.FI route.

Addresses are from the official lifinance/contracts config/across.json and
across-protocol/contracts deployments; ABI is V3SpokePoolInterface.sol.
Avalanche is not in that official Across deployment set. Never fabricate
a contract or interpret a provider DONE flag as final settlement.
"""
from eth_abi import decode, encode
from eth_utils import keccak

POOLS = {
    1: "0x5c7bcd6e7de5423a257d81b442095a1a6ced35c5",
    56: "0x4e8e101924ede233c13e2d8622dc8aed2872d505",
    137: "0x9295ee1d8c5b022be115a2ad3c30c72e34e7f096",
    42161: "0xe35e9842fceaca96570b734083f4a58e8f7c5f2a",
}
DEPOSIT_TYPES = ["bytes32","bytes32","uint256","uint256","uint32",
                 "uint32","uint32","bytes32","bytes32","bytes"]
FILL_TYPES = ["bytes32","bytes32","uint256","uint256","uint256","uint32",
              "uint32","bytes32","bytes32","bytes32","bytes32",
              "(bytes32,bytes32,uint256,uint8)"]
DEPOSIT_TOPIC = "0x"+keccak(text="FundsDeposited(bytes32,bytes32,uint256,uint256,uint256,uint256,uint32,uint32,uint32,bytes32,bytes32,bytes32,bytes)").hex()
FILL_TOPIC = "0x"+keccak(text="FilledRelay(bytes32,bytes32,uint256,uint256,uint256,uint256,uint256,uint32,uint32,bytes32,bytes32,bytes32,bytes32,bytes32,(bytes32,bytes32,uint256,uint8))").hex()


def word(value):
    if not isinstance(value,str) or len(value)!=66 or not value.startswith("0x"):
        raise ValueError("Invalid bridge event topic")
    return bytes.fromhex(value[2:])


def address_word(value):
    if not isinstance(value,str) or len(value)!=42 or not value.startswith("0x"):
        raise ValueError("Invalid bridge event address")
    return bytes(12)+bytes.fromhex(value[2:])


def single_event(logs, chain, topic, types):
    pool=POOLS.get(chain)
    if pool is None:
        return None
    matches=[]
    for event in logs:
        if str(event.get("address","")).lower()!=pool:
            continue
        topics=event.get("topics",[])
        if not topics or str(topics[0]).lower()!=topic:
            continue
        if event.get("removed") is True or len(topics)!=4:
            raise ValueError("Malformed bridge event")
        raw=bytes.fromhex(event["data"][2:])
        data=decode(types,raw,strict=True)
        if encode(types,data)!=raw:
            raise ValueError("Noncanonical bridge event")
        matches.append((tuple(word(t) for t in topics[1:]),data))
    if len(matches)>1:
        raise ValueError("Ambiguous bridge event")
    return matches[0] if matches else None


def prove_across_pair(source_logs,destination_logs,*,src,dst,wallet,route,received):
    """Match the same deposit and fill, not simply equal wallet credits."""
    source=single_event(source_logs,src,DEPOSIT_TOPIC,DEPOSIT_TYPES)
    target=single_event(destination_logs,dst,FILL_TOPIC,FILL_TYPES)
    if source is None or target is None:
        return None
    (dst_word,deposit_id,depositor),d=source
    (src_word,fill_id,_relayer),r=target
    token_in,token_out,amount,output,quote_ts,deadline,exclusive_deadline,recipient,exclusive_relayer,message=d
    i,o,a,b,_repay,fill_deadline,fill_exclusive,fill_relayer,fill_depositor,fill_recipient,message_hash,execution=r
    updated_recipient,updated_message_hash,updated_amount,fill_type=execution
    owner=address_word(wallet)
    destination=route.get("destination_swap")
    receiver=address_word(destination["receiver"]) if destination else owner
    expected_message_hash=destination["message_hash"] if destination else bytes(32)
    bridge_received=destination["bridge_amount"] if destination else received
    if (int.from_bytes(dst_word,"big")!=dst or int.from_bytes(src_word,"big")!=src
            or deposit_id!=fill_id or depositor!=owner or recipient!=receiver
            or fill_depositor!=owner or fill_recipient!=receiver or updated_recipient!=receiver
            or (keccak(message) if message else bytes(32))!=expected_message_hash
            or message_hash!=expected_message_hash or updated_message_hash!=expected_message_hash
            or token_in!=address_word(route["bridge_source_token"])
            or token_out!=address_word(route["bridge_destination_token"])
            or (i,o,a,b)!=(token_in,token_out,amount,output)
            or quote_ts!=route["quote_timestamp"] or deadline!=route["fill_deadline"]
            or fill_deadline!=deadline or fill_exclusive!=exclusive_deadline
            or fill_relayer!=exclusive_relayer
            or fill_type not in (0,1,2) or updated_amount!=bridge_received
            or received<route["minimum_output_atomic"]):
        raise ValueError("Bridge fill does not match the original owner deposit")
    if route["source_swap"]:
        if amount<route["bridge_minimum_atomic"] or output!=amount*route["output_multiplier"]//10**18:
            raise ValueError("Bridge deposit differs from inspected source swap")
    elif amount!=route["bridge_minimum_atomic"] or output!=route["direct_output_atomic"]:
        raise ValueError("Direct bridge deposit differs from inspected calldata")
    if updated_amount!=output:
        raise ValueError("Modified bridge payout not accepted for original order")
    if destination:
        if received<destination["minimum_final"]:
            raise ValueError("Final swap credit below its exact minimum")
        completion="0x"+keccak(text="LiFiTransferCompleted(bytes32,address,address,uint256,uint256)").hex()
        recovery="0x"+keccak(text="LiFiTransferRecovered(bytes32,address,address,uint256,uint256)").hex()
        matches=0
        for event in destination_logs:
            topics=event.get("topics",[])
            if len(topics)!=2 or topics[1].lower()!="0x"+destination["transaction_id"].hex():
                continue
            address=str(event.get("address","")).lower()
            if address==destination["receiver"] and topics[0].lower()==recovery:
                raise ValueError("Destination swap recovered intermediate asset, not completed")
            if address==destination["executor"] and topics[0].lower()==completion:
                if event.get("removed") is True:
                    raise ValueError("Removed completion event")
                data=bytes.fromhex(event["data"][2:])
                fields=["address","address","uint256","uint256"]
                values=decode(fields,data,strict=True)
                if encode(fields,values)!=data or values[0].lower()!=route["bridge_destination_token"] or values[1].lower()!=wallet:
                    raise ValueError("Executor completion does not bind original wallet")
                matches+=1
        if matches!=1:
            raise ValueError("Original destination swap completion not proven")
    return {"deposit_id":str(int.from_bytes(deposit_id,"big")),
            "bridge_message_verified":True}
