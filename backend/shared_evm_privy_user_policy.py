"""Per-asset Privy v3 policy templates built from ONE EVM execution registry.

These are OFFLINE drafts, not Privy grants, deploys, signer registrations, or
authorization to send. User-specific atomic caps and policy owner roles must
come from independently verified live settings and signed consent.

No cross-chain wildcard, contract wildcard, uncapped token approval, or
arbitrary destination is allowed. ETH retains its existing granted policy.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

from .eth_privy_user_policy_blueprint import (
    _tx, _call, _expiry, _positive_atomic,
    WETH_DEPOSIT_ABI, WETH_WITHDRAW_ABI,
)
from .eth_swaprouter02_preflight import EXACT_INPUT_SINGLE_ABI
from .shared_evm_swap_receipts import _pair

_ADDRESS=re.compile(r"^0x[0-9a-fA-F]{40}$")
_ID=re.compile(r"^[A-Za-z0-9_-]{6,160}$")
_NATIVE=frozenset(("BNB","POL","AVAX"))
_ADDITIONAL=frozenset(("BNB","POL","AVAX","LINK","UNI","ARB"))


def build_user_evm_policy_blueprint(
    *,asset:str,stable:str,wallet:str,owner_id:str,executor_id:str,
    buy_source_atomic_cap:str,sell_source_atomic_cap:str,
    native_wrap_atomic_cap:str|None,expires_at:int,now:int,
)->dict:
    """Allow only this asset's chain-bound BUY/SELL and optional native wrap.

    Each Privy ALLOW rule independently enforces exact input token, output
    token, recipient wallet, chain, Router02 address, fee, amount and expiry.
    A backend order must also enforce independent minOut from its live quote.
    """
    if asset not in _ADDITIONAL:
        raise ValueError("Unsupported per-coin EVM policy")
    pair_buy=_pair(asset,stable,"BUY")
    pair_sell=_pair(asset,stable,"SELL")
    if (type(wallet) is not str or not _ADDRESS.fullmatch(wallet)
            or type(owner_id) is not str or not _ID.fullmatch(owner_id)
            or type(executor_id) is not str or not _ID.fullmatch(executor_id)
            or owner_id==executor_id
            or type(now) is not int or now<=0
            or type(expires_at) is not int
            or not now<expires_at<=now+30*86400):
        raise ValueError("Bound owner wallet, distinct quorum and expiry required")
    cap_buy=_positive_atomic(buy_source_atomic_cap)
    cap_sell=_positive_atomic(sell_source_atomic_cap)
    if asset in _NATIVE:
        if native_wrap_atomic_cap is None:
            raise ValueError("Missing independently verified native cap")
        cap_wrap=_positive_atomic(native_wrap_atomic_cap)
    elif native_wrap_atomic_cap is not None:
        raise ValueError("Token asset must not receive native wrap permission")
    else:
        cap_wrap=None
    chain=str(pair_buy["chain_id"])
    router=pair_buy["router"]
    rules=[]
    for side,pair,cap in (
        ("BUY",pair_buy,cap_buy),("SELL",pair_sell,cap_sell)
    ):
        abi=EXACT_INPUT_SINGLE_ABI
        conditions=[
            _tx("chain_id","eq",chain),
            _tx("to","eq",router),
            _expiry(expires_at),
            _tx("value","eq","0"),
            _call("function_name","eq","exactInputSingle",abi),
            _call("exactInputSingle.params.tokenIn","eq",
                  pair["source_token"],abi),
            _call("exactInputSingle.params.tokenOut","eq",
                  pair["output_token"],abi),
            _call("exactInputSingle.params.recipient","eq",wallet,abi),
            _call("exactInputSingle.params.fee","in",
                  [str(v) for v in pair["fee_levels"]],abi),
            _call("exactInputSingle.params.sqrtPriceLimitX96","eq","0",abi),
            _call("exactInputSingle.params.amountIn","gt","0",abi),
            _call("exactInputSingle.params.amountIn","lte",cap,abi),
            _call("exactInputSingle.params.amountOutMinimum","gt","0",abi),
        ]
        rules.append({
            "name":f"MiniNex {asset} {side} {stable} exactInputSingle",
            "method":"eth_sendTransaction",
            "action":"ALLOW",
            "conditions":conditions,
        })
    if cap_wrap is not None:
        wrapped=pair_sell["source_token"]
        rules.extend((
            {
                "name":f"MiniNex {asset} native deposit",
                "method":"eth_sendTransaction","action":"ALLOW",
                "conditions":[
                    _tx("chain_id","eq",chain),_tx("to","eq",wrapped),
                    _expiry(expires_at),
                    _call("function_name","eq","deposit",WETH_DEPOSIT_ABI),
                    _tx("value","gt","0"),_tx("value","lte",cap_wrap),
                ],
            },
            {
                "name":f"MiniNex {asset} native withdraw",
                "method":"eth_sendTransaction","action":"ALLOW",
                "conditions":[
                    _tx("chain_id","eq",chain),_tx("to","eq",wrapped),
                    _expiry(expires_at),
                    _call("function_name","eq","withdraw",WETH_WITHDRAW_ABI),
                    _tx("value","eq","0"),
                    _call("withdraw.wad","gt","0",WETH_WITHDRAW_ABI),
                    _call("withdraw.wad","lte",cap_sell,WETH_WITHDRAW_ABI),
                ],
            },
        ))
    return {
        "version":"1.0","chain_type":"ethereum",
        "name":f"MiniNex-{asset}-{wallet[-8:].lower()}",
        "owner_id":owner_id,"rules":rules,
    }


def inspect_user_evm_policy_blueprint(
    policy:object, **params,
)->dict:
    """Compare entire submitted draft, not only a few selected rule fields."""
    expected=build_user_evm_policy_blueprint(**params)
    matched=type(policy) is dict and policy==expected
    digest=hashlib.sha256(json.dumps(
        expected,sort_keys=True,separators=(",",":")
    ).encode("utf-8")).hexdigest()
    asset=params["asset"]
    pair=_pair(asset,params["stable"],"BUY")
    return {
        "mode":"SHARED_EVM_PRIVY_USER_POLICY_DRAFT_ONLY",
        "asset":asset,"network":pair["network"],
        "chain_id":pair["chain_id"],
        "wallet_bound":matched,
        "policy_draft_matches_expected":matched,
        "draft_sha256":digest if matched else None,
        "allow_rules":len(expected["rules"]) if matched else 0,
        "policy_created_in_privy":False,
        "user_consent_verified":False,
        "additional_signer_grant_verified":False,
        "auto_execution_enabled":False,
        "can_submit":False,
    }
