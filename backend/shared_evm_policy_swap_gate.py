"""Exact Privy per-coin policy caps versus one independently quoted EVM swap.

This is the final read-only check before an authorized, single-attempt send.
No platform-wide fixed budget limit is added: these are the exact atomic
constraints that the wallet owner selected/approved for this one policy.
"""
from __future__ import annotations

from fastapi import HTTPException
from .shared_evm_remote_grant_audit import validate_pinned_evm_policy, _lte
from .shared_evm_swap_receipts import _pair
from .shared_evm_auto_preflight import inspect_evm_exact_swap


def check_evm_policy_bound_swap(
    *,record:dict,asset:str,side:str,stable:str,wallet:str,
    tx:dict,input_atomic:str,minimum_output_atomic:str,now:int,
)->dict:
    if (type(record) is not dict or type(now) is not int
            or record.get("asset")!=asset
            or record.get("stable")!=stable
            or type(wallet) is not str
            or record.get("wallet_address")!=wallet.lower()):
        raise HTTPException(409,"EVM policy wallet, asset or settlement differs")
    try:
        policy=validate_pinned_evm_policy(record,asset=asset,now=now)
        preflight=inspect_evm_exact_swap(
            tx,asset=asset,wallet=wallet,side=side,stable=stable,
            input_atomic=input_atomic,
            minimum_output_atomic=minimum_output_atomic)
        rule_index={"BUY":0,"SELL":1}[side]
        cap=int(_lte(
            policy["rules"][rule_index],"exactInputSingle.params.amountIn"))
        amount=int(input_atomic)
        if (type(input_atomic) is not str or input_atomic!=str(amount)
                or not 0<amount<=cap):
            raise ValueError("Exact swap exceeds this owner's reviewed atomic cap")
        if (preflight.get("asset")!=asset
                or preflight.get("chain_id")!=_pair(
                    asset,stable,side)["chain_id"]):
            raise ValueError("Wrong policy chain or swap source")
    except (ValueError,TypeError,KeyError,OverflowError):
        raise HTTPException(409,"Current Privy owner policy does not cover this swap") from None
    return {
        "mode":"ONE_EXACT_REVIEWED_EVM_SWAP_WITHIN_PRIVY_POLICY",
        "asset":asset,"side":side,"stable":stable,
        "chain_id":preflight["chain_id"],"policy_sha256":record["policy_sha256"],
        "exact_swap_validated":True,
        "atomic_input_within_owner_policy":True,
        "wallet_signing_permitted_by_this_check":False,
        "order_claimed":False,"transaction_submitted":False,
    }
