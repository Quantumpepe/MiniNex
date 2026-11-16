"""Common six-asset exactInputSingle invariant used before Privy signing.

This is the SAME strict transaction inspection for the currently active ETH
AUTO sender and for the other EVM adapters. No chain or token may substitute
the ETH owner's policy, and this is not authorization to broadcast.
"""
from __future__ import annotations

import re
from .shared_evm_swap_receipts import _pair
from .shared_evm_execution import EXACT_INPUT_SINGLE

_ADDRESS=re.compile(r"^0x[0-9a-fA-F]{40}$")
_ATOMIC=re.compile(r"^[1-9][0-9]{0,44}$")
_CALL=re.compile(r"^0x[0-9a-fA-F]{456}$")


def inspect_evm_exact_swap(
    tx: object, *, asset: str, wallet: str,
    side: str, stable: str, input_atomic: str,
    minimum_output_atomic: str,
) -> dict:
    """Inspect the identical strict bounded payload accepted by a real signer.

    Nothing from the request selects a destination token, router or network;
    all those addresses are derived from the canonical on-chain route registry.
    """
    pair=_pair(asset,stable,side)
    if (type(tx) is not dict or type(wallet) is not str
            or not _ADDRESS.fullmatch(wallet)
            or type(input_atomic) is not str or not _ATOMIC.fullmatch(input_atomic)
            or type(minimum_output_atomic) is not str
            or not _ATOMIC.fullmatch(minimum_output_atomic)
            or type(tx.get("from")) is not str
            or tx["from"].lower()!=wallet.lower()
            or type(tx.get("to")) is not str
            or tx["to"].lower()!=pair["router"].lower()
            or type(tx.get("chainId")) is not int
            or tx["chainId"]!=pair["chain_id"]
            or type(tx.get("value")) not in (str,int)
            or tx["value"] not in ("0",0)
            or type(tx.get("data")) is not str
            or not _CALL.fullmatch(tx["data"])
            or not tx["data"].lower().startswith(EXACT_INPUT_SINGLE)):
        raise ValueError("Unverified EVM swap transaction")
    words=[int(tx["data"][10+i*64:10+(i+1)*64],16) for i in range(7)]
    token_in,token_out,fee,recipient,amount_in,minimum_out,limit=words
    if (token_in>=2**160 or token_out>=2**160
            or recipient>=2**160
            or token_in!=int(pair["source_token"],16)
            or token_out!=int(pair["output_token"],16)
            or recipient!=int(wallet,16)
            or fee not in pair["fee_levels"]
            or amount_in!=int(input_atomic)
            or minimum_out<int(minimum_output_atomic)
            or minimum_out<=0
            or limit!=0):
        raise ValueError("EVM calldata differs from the approved quote")
    return {
        "asset":asset,"network":pair["network"],
        "chain_id":pair["chain_id"],"side":side,"stable":stable,
        "router":pair["router"],"wallet":wallet.lower(),
        "source_token":pair["source_token"],
        "output_token":pair["output_token"],
        "input_atomic":str(amount_in),
        "minimum_output_atomic":str(minimum_out),
        "token_pair_verified":True,
        "recipient_verified":True,
        "quote_minimum_verified":True,
        "server_broadcast_authorized":False,
    }
