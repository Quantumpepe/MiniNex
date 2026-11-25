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
from .shared_evm_central_runtime import central_policy_rule
from .ethereum_wrapped_spot_policy import (WRAPPED_SPOT_POLICY, WRAPPED_SPOT_TOKENS,
    inspect_wrapped_spot_transaction, wrapped_spot_route)


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
        if record.get("authorization_method")=="PRIVY_CENTRAL_ONE_SIGNER":
            rule=central_policy_rule(
                record,asset=asset,kind="swap",
                side=side,stable=stable,now=now,
            )
        else:
            policy=validate_pinned_evm_policy(record,asset=asset,now=now)
            rule=policy["rules"][{"BUY":0,"SELL":1}[side]]
        if asset in WRAPPED_SPOT_TOKENS:
            if (record.get("authorization_method")!="PRIVY_CENTRAL_ONE_SIGNER"
                    or record.get("scope",{}).get("settlement_policy")!=WRAPPED_SPOT_POLICY):
                raise ValueError("Original wrapped trading consent missing")
            amount=int(input_atomic)
            minimum=int(minimum_output_atomic)
            if (type(input_atomic) is not str or input_atomic!=str(amount)
                    or type(minimum_output_atomic) is not str or minimum_output_atomic!=str(minimum)
                    or not 0<amount<=int(_lte(rule,"exactInput.params.amountIn"))
                    or minimum<=0):
                raise ValueError("Exact wrapped swap exceeds owner's scope")
            inspect_wrapped_spot_transaction(tx,asset=asset,stable=stable,side=side,
                wallet=wallet,amount_atomic=amount,min_out_atomic=minimum)
            route=wrapped_spot_route(asset,stable,side)
            if record.get("chain_id")!=route["chain_id"]:
                raise ValueError("Wrong original wrapped settlement network")
            return {
                "mode":"ONE_EXACT_REVIEWED_EVM_SWAP_WITHIN_PRIVY_POLICY",
                "asset":asset,"side":side,"stable":stable,"chain_id":1,
                "policy_sha256":record["policy_sha256"],"exact_swap_validated":True,
                "atomic_input_within_owner_policy":True,
                "wallet_signing_permitted_by_this_check":False,
                "order_claimed":False,"transaction_submitted":False,
            }
        preflight=inspect_evm_exact_swap(
            tx,asset=asset,wallet=wallet,side=side,stable=stable,
            input_atomic=input_atomic,
            minimum_output_atomic=minimum_output_atomic)
        cap=int(_lte(
            rule,"exactInputSingle.params.amountIn"))
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
