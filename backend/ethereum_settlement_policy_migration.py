"""Offline exact direct-bridge policy migration preview.

Privy policy examples describe ABI field predicates and chain/address/value
conditions. Actual remote acceptance of nested bytes/boolean tuple predicates
is not established by an offline draft. Never publish or attach this candidate
until reviewed, user-authorized, and independently verified remotely.
Source-swap arrays intentionally have no generic ALLOW rule.
"""
from copy import deepcopy
import hashlib
import json
from eth_abi import decode
from .ethereum_settlement_calldata import (
    BRIDGE,ACROSS,DIRECT,DIAMOND,inspect_across_v4,
)
from .ethereum_settlement_plan import check_evm_quote_route
from .eth_privy_user_policy_blueprint import _tx,_call,_expiry
from .shared_evm_single_grant_policy import ERC20_APPROVE_ABI
from .live_eth_quotes import ETH_NATIVE

FN="startBridgeTokensViaAcrossV4"
BRIDGE_FIELDS=[
    ("transactionId","bytes32"),("bridge","string"),("integrator","string"),
    ("referrer","address"),("sendingAssetId","address"),("receiver","address"),
    ("minAmount","uint256"),("destinationChainId","uint256"),
    ("hasSourceSwaps","bool"),("hasDestinationCall","bool"),
]
ACROSS_FIELDS=[
    ("receiverAddress","bytes32"),("refundAddress","bytes32"),
    ("sendingAssetId","bytes32"),("receivingAssetId","bytes32"),
    ("outputAmount","uint256"),("outputAmountMultiplier","uint128"),
    ("exclusiveRelayer","bytes32"),("quoteTimestamp","uint32"),
    ("fillDeadline","uint32"),("exclusivityParameter","uint32"),("message","bytes"),
]
def fields(items):
    return [{"name":name,"type":typ} for name,typ in items]
DIRECT_ABI=[{"name":FN,"type":"function","stateMutability":"payable","outputs":[],
    "inputs":[{"name":"_bridgeData","type":"tuple","components":fields(BRIDGE_FIELDS)},
              {"name":"_acrossData","type":"tuple","components":fields(ACROSS_FIELDS)}]}]


def _value(value):
    if type(value) is bytes:
        return "0x"+value.hex()
    if type(value) is bool:
        return value
    if type(value) is int:
        return str(value)
    return value


def exact_direct_bridge_rules(quote,*,now):
    """Exact one-quote rule, not recurring automation authority.

    No arbitrary inner call; every decoded argument is bound. A signed
    recurring policy cannot safely be inferred from this quote-specific draft.
    """
    check_evm_quote_route(quote)
    if not quote["issued_at"]<=now<quote["expires_at"]:
        raise ValueError("Quote expired")
    inspected=inspect_across_v4(quote["unsigned_transaction"],terms=quote,now=now)
    if quote.get("route_sha256")!=inspected["route_sha256"]:
        raise ValueError("Original route digest changed")
    tx=inspected["transaction"]; raw=bytes.fromhex(tx["data"][2:])
    if raw[:4]!=DIRECT:
        raise ValueError("Nested source swap needs independently enforced policy")
    bridge,across=decode([BRIDGE,ACROSS],raw[4:],strict=True)
    base=[_tx("chain_id","eq",str(tx["chainId"])),_expiry(quote["expires_at"])]
    cond=deepcopy(base)+[_tx("to","eq",DIAMOND),_tx("value","eq",tx["value"]),
        _call("function_name","eq",FN,DIRECT_ABI)]
    for name,spec,values in (("_bridgeData",BRIDGE_FIELDS,bridge),
                             ("_acrossData",ACROSS_FIELDS,across)):
        for (field,_),value in zip(spec,values):
            cond.append(_call(FN+"."+name+"."+field,"eq",_value(value),DIRECT_ABI))
    label="MiniNex "+quote["asset"]+" "+quote["side"]+" exact direct quote"
    rules=[{"name":label,"method":"eth_sendTransaction","action":"ALLOW","conditions":cond}]
    if quote["source_token"]!=ETH_NATIVE:
        rules.append({"name":label+" allowance","method":"eth_sendTransaction","action":"ALLOW",
            "conditions":deepcopy(base)+[_tx("to","eq",quote["source_token"]),_tx("value","eq","0"),
                _call("function_name","eq","approve",ERC20_APPROVE_ABI),
                _call("approve.spender","eq",DIAMOND,ERC20_APPROVE_ABI),
                _call("approve.value","eq",str(quote["input_atomic"]),ERC20_APPROVE_ABI)]})
    digest=hashlib.sha256(json.dumps(rules,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    return {"rules":rules,"rules_sha256":digest,"route_sha256":inspected["route_sha256"],
            "remote_policy_verified":False,"recurring_auto_authority":False,"can_submit":False}


def migration_review(quote,record,*,now):
    if quote["asset"]!=record["asset"] or quote["stable"]!=record["stable"] or (
            quote["wallet_address"].lower()!=record["wallet_address"].lower()):
        raise ValueError("Original grant and quote differ")
    inspected=inspect_across_v4(quote["unsigned_transaction"],terms=quote,now=now)
    if inspected["route_sha256"]!=quote["route_sha256"]:
        raise ValueError("Original quote changed")
    rules=None
    if not inspected["source_swap"]:
        rules=exact_direct_bridge_rules(quote,now=now)
    return {"asset":quote["asset"],"source_chain_id":quote["source_chain_id"],
        "destination_chain_id":quote["destination_chain_id"],
        "old_policy_sha256":record["policy_sha256"],
        "owner_budget_revision":record["budget_revision"],
        "direct_quote_rule_sha256":rules["rules_sha256"] if rules else None,
        "migration_kind":"EXACT_DIRECT_QUOTE" if rules else "NESTED_SWAP_POLICY_UNRESOLVED",
        "requires_new_owner_consent":True,"remote_policy_verified":False,
        "can_submit":False}
