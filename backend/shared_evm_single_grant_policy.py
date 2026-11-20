"""Single-owner-grant EVM policy blueprint: trades, router approvals and fee.

Pure construction only. A remote Privy wallet-owner addSigners signature must
approve the ENTIRE policy, followed by independent remote verification.
This module never sends transactions or turns an existing trading-only grant
into authority to collect fees.
"""
from __future__ import annotations

from copy import deepcopy

from .shared_evm_privy_user_policy import build_user_evm_policy_blueprint
from .shared_evm_fee_privy_policy_draft import (
    build_owner_evm_fee_policy_draft,
)
from .eth_privy_user_policy_blueprint import _tx, _call, _expiry, _positive_atomic
from .shared_evm_swap_receipts import _pair

ERC20_APPROVE_ABI = [{
    "type": "function", "name": "approve",
    "stateMutability": "nonpayable",
    "inputs": [
        {"name": "spender", "type": "address"},
        {"name": "value", "type": "uint256"},
    ],
    "outputs": [{"name": "", "type": "bool"}],
}]


def build_single_grant_evm_policy(
    *, asset: str, stable: str, wallet: str, treasury: str,
    owner_id: str, executor_id: str,
    buy_source_atomic_cap: str, sell_source_atomic_cap: str,
    native_wrap_atomic_cap: str | None, owner_max_fee_atomic: str,
    expires_at: int, now: int,
) -> dict:
    """Exactly scoped route + finite approval + treasury fee under ONE signer.

    Transaction-level approvals only permit the verified Router02 spender
    and a finite approved amount. Runtime must additionally verify the
    approved grant, unmodified budget revision, transaction source, available
    balance, and confirmed allowance before any swap. Profit fees require
    separately verified net-realized-PnL debt, not merely this rule.
    """
    trade = build_user_evm_policy_blueprint(
        asset=asset, stable=stable, wallet=wallet,
        owner_id=owner_id, executor_id=executor_id,
        buy_source_atomic_cap=buy_source_atomic_cap,
        sell_source_atomic_cap=sell_source_atomic_cap,
        native_wrap_atomic_cap=native_wrap_atomic_cap,
        expires_at=expires_at, now=now,
    )
    fee = build_owner_evm_fee_policy_draft(
        asset=asset, stable=stable, wallet=wallet, treasury=treasury,
        owner_id=owner_id, executor_id=executor_id,
        owner_max_fee_atomic=owner_max_fee_atomic,
        expires_at=expires_at, now=now,
    )
    if (trade["owner_id"] != fee["owner_id"] or
            trade["chain_type"] != fee["chain_type"]):
        raise ValueError("Incompatible owner grant scopes")
    rules = deepcopy(trade["rules"])
    for side, cap in (
        ("BUY", buy_source_atomic_cap),
        ("SELL", sell_source_atomic_cap),
    ):
        pair = _pair(asset, stable, side)
        conditions = [
            _tx("chain_id", "eq", str(pair["chain_id"])),
            _tx("to", "eq", pair["source_token"]),
            _expiry(expires_at),
            _tx("value", "eq", "0"),
            _call("function_name", "eq", "approve", ERC20_APPROVE_ABI),
            _call("approve.spender", "eq", pair["router"], ERC20_APPROVE_ABI),
            _call("approve.value", "gt", "0", ERC20_APPROVE_ABI),
            _call("approve.value", "lte", _positive_atomic(cap),
                  ERC20_APPROVE_ABI),
        ]
        rules.append({
            "name": f"MiniNex {asset} {side} finite Router02 allowance",
            "method": "eth_sendTransaction", "action": "ALLOW",
            "conditions": conditions,
        })
    rules.extend(deepcopy(fee["rules"]))
    trade["rules"] = rules
    trade["name"] = f"MiniNex-one-grant-{asset}-{wallet[-8:].lower()}"
    return trade
