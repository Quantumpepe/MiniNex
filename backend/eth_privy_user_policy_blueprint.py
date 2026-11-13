"""Owner-controlled, user-specific Privy ETH policy BLUEPRINTS only.

This is a pure, offline, non-executable policy *draft constructor*, NOT an
enrollment service and NOT evidence of Privy approval. No API endpoint,
Privy policy POST, signer registration, user grant, wallet RPC, transaction
builder, or money movement is exposed.

A policy template needs independently verified individual wallet ownership,
an owner key separate from the executor and exact per-user atomic caps.
No fixed $100 demo cap is embedded. Caps are NOT derived from paper balances
or an unverified user request: the future enrollment worker must source them
from authenticated LIVE limits, immutable signed user consent, and a fresh
independent stable/WETH pricing/risk conversion.

Privy evaluates AND within one rule, OR across ALLOW rules. Accordingly
EACH rule carries its own network, contract, decoded ABI, amount and expiry
constraints. Allowing even one broad alternate rule would bypass all others.

Official schema:
https://docs.privy.io/controls/policies/create-a-policy
https://docs.privy.io/controls/policies/overview
https://docs.privy.io/controls/policies/example-policies/ethereum
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re

from .eth_swaprouter02_preflight import (
    SWAPROUTER02_MAINNET, WETH_MAINNET, EXACT_INPUT_SINGLE_ABI,
)
from .live_eth_quotes import STABLES

_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_ID = re.compile(r"^[A-Za-z0-9_-]{6,160}$")
_ATOMIC = re.compile(r"^[1-9][0-9]*$")
_MAX_ATOMIC = 10**45
_MAX_LIFETIME = 30 * 86400
_APPROVED_FEES = ["500", "3000"]

WETH_DEPOSIT_ABI = [{
    "type": "function",
    "name": "deposit",
    "stateMutability": "payable",
    "inputs": [],
    "outputs": [],
}]
WETH_WITHDRAW_ABI = [{
    "type": "function",
    "name": "withdraw",
    "stateMutability": "nonpayable",
    "inputs": [{"name": "wad", "type": "uint256"}],
    "outputs": [],
}]


def _positive_atomic(value: object) -> str:
    if type(value) is not str or len(value) > 46 or _ATOMIC.fullmatch(value) is None:
        raise ValueError("Exact positive token atomic units required")
    integer = int(value)
    if integer >= _MAX_ATOMIC:
        raise ValueError("Per-user atomic cap outside supported limits")
    return str(integer)


def _condition(source: str, field: str, operator: str, value: object,
               abi: list | None = None) -> dict:
    result = {
        "field_source": source, "field": field,
        "operator": operator, "value": value,
    }
    if abi is not None:
        result["abi"] = deepcopy(abi)
    return result


def _tx(field: str, operator: str, value: object) -> dict:
    return _condition("ethereum_transaction", field, operator, value)


def _call(field: str, operator: str, value: object, abi: list) -> dict:
    return _condition("ethereum_calldata", field, operator, value, abi)


def _expiry(expires_at: int) -> dict:
    return _condition("system", "current_unix_timestamp", "lt", str(expires_at))


def _base(contract: str, expires_at: int) -> list[dict]:
    return [
        _tx("chain_id", "eq", "1"),
        _tx("to", "eq", contract),
        _expiry(expires_at),
    ]


def build_eth_privy_user_policy_blueprint(
    *, wallet: str, policy_owner_id: str, executor_signer_id: str,
    usdc_max_input_micro: str, usdt_max_input_micro: str,
    weth_max_input_wei: str, native_wrap_max_wei: str,
    expires_at: int, now: int,
) -> dict:
    """Create one restricted draft per independently verified ETH wallet.

    Limits are independently verified per-user per-transaction ceilings:
      * stablecoin input micro-units for BUY (six-decimal USDC/USDT)
      * WETH input wei for SELL and WETH withdraw
      * native ETH wei for WETH deposit (value sent to WETH9)

    The blueprint does NOT enforce a rolling cumulative cap or prove oracle
    prices, wallet ownership, revocable user consent or ERC20 allowances.
    These requirements remain before policy creation or trading activation.
    """
    if (type(wallet) is not str or _ADDR.fullmatch(wallet) is None
            or type(policy_owner_id) is not str
            or _ID.fullmatch(policy_owner_id) is None
            or type(executor_signer_id) is not str
            or _ID.fullmatch(executor_signer_id) is None
            or policy_owner_id == executor_signer_id):
        raise ValueError("Verified wallet, independent owner and executor IDs required")
    if (type(now) is not int or type(expires_at) is not int
            or now <= 0 or expires_at <= now
            or expires_at > now + _MAX_LIFETIME):
        raise ValueError("Short bounded policy expiry required")

    caps = {
        "USDC": _positive_atomic(usdc_max_input_micro),
        "USDT": _positive_atomic(usdt_max_input_micro),
    }
    max_weth = _positive_atomic(weth_max_input_wei)
    max_wrap = _positive_atomic(native_wrap_max_wei)

    rules = []
    for stable, token in STABLES.items():
        for side in ("BUY", "SELL"):
            source = token if side == "BUY" else WETH_MAINNET
            target = WETH_MAINNET if side == "BUY" else token
            cap = caps[stable] if side == "BUY" else max_weth
            conditions = _base(SWAPROUTER02_MAINNET, expires_at) + [
                _tx("value", "eq", "0"),
                _call("function_name", "eq", "exactInputSingle",
                      EXACT_INPUT_SINGLE_ABI),
                _call("exactInputSingle.params.tokenIn", "eq", source,
                      EXACT_INPUT_SINGLE_ABI),
                _call("exactInputSingle.params.tokenOut", "eq", target,
                      EXACT_INPUT_SINGLE_ABI),
                _call("exactInputSingle.params.recipient", "eq", wallet,
                      EXACT_INPUT_SINGLE_ABI),
                _call("exactInputSingle.params.fee", "in", list(_APPROVED_FEES),
                      EXACT_INPUT_SINGLE_ABI),
                _call("exactInputSingle.params.sqrtPriceLimitX96", "eq", "0",
                      EXACT_INPUT_SINGLE_ABI),
                _call("exactInputSingle.params.amountIn", "gt", "0",
                      EXACT_INPUT_SINGLE_ABI),
                _call("exactInputSingle.params.amountIn", "lte", cap,
                      EXACT_INPUT_SINGLE_ABI),
                # This does NOT by itself enforce the quoted market slippage:
                # the future executor must independently bind minimumOut to
                # the exact fresh quote, verified wallet and submitted bytes.
                _call("exactInputSingle.params.amountOutMinimum", "gt", "0",
                      EXACT_INPUT_SINGLE_ABI),
            ]
            rules.append({
                "name": f"MiniNex ETH {side} {stable} exactInputSingle",
                "method": "eth_sendTransaction",
                "action": "ALLOW",
                "conditions": conditions,
            })

    rules.append({
        "name": "MiniNex ETH direct WETH9 deposit",
        "method": "eth_sendTransaction",
        "action": "ALLOW",
        "conditions": _base(WETH_MAINNET, expires_at) + [
            _call("function_name", "eq", "deposit", WETH_DEPOSIT_ABI),
            _tx("value", "gt", "0"),
            _tx("value", "lte", max_wrap),
        ],
    })
    rules.append({
        "name": "MiniNex ETH direct WETH9 withdraw",
        "method": "eth_sendTransaction",
        "action": "ALLOW",
        "conditions": _base(WETH_MAINNET, expires_at) + [
            _call("function_name", "eq", "withdraw", WETH_WITHDRAW_ABI),
            _tx("value", "eq", "0"),
            _call("withdraw.wad", "gt", "0", WETH_WITHDRAW_ABI),
            _call("withdraw.wad", "lte", max_weth, WETH_WITHDRAW_ABI),
        ],
    })

    result = {
        "version": "1.0",
        "chain_type": "ethereum",
        "name": "MiniNex-ETH-" + wallet[-8:].lower(),
        "owner_id": policy_owner_id,
        "rules": rules,
    }
    return result


def inspect_eth_user_policy_blueprint(
    policy: object, *, wallet: str, policy_owner_id: str,
    executor_signer_id: str, usdc_max_input_micro: str,
    usdt_max_input_micro: str, weth_max_input_wei: str,
    native_wrap_max_wei: str, expires_at: int, now: int,
) -> dict:
    """Compare the *whole* draft to an independently reconstructed version.

    No ambiguous additional ALLOW, unsafe wildcard or missing condition can
    pass. This does not verify Privy's policy-parser interpretation or prove
    that the values were securely sourced: a manual staging deployment and
    controlled on-chain simulations are still required before real grants.
    """
    expected = build_eth_privy_user_policy_blueprint(
        wallet=wallet, policy_owner_id=policy_owner_id,
        executor_signer_id=executor_signer_id,
        usdc_max_input_micro=usdc_max_input_micro,
        usdt_max_input_micro=usdt_max_input_micro,
        weth_max_input_wei=weth_max_input_wei,
        native_wrap_max_wei=native_wrap_max_wei,
        expires_at=expires_at, now=now,
    )
    matched = type(policy) is dict and policy == expected
    digest = hashlib.sha256(json.dumps(
        expected, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return {
        "mode": "USER_POLICY_BLUEPRINT_STATIC_REVIEW_ONLY",
        "asset": "ETH",
        "chain_id": 1,
        "policy_draft_matches_expected": matched,
        "draft_sha256": digest if matched else None,
        "wallet_bound": matched,
        "policy_owner_separate_from_executor": matched,
        "allow_rules": 6 if matched else 0,
        "cumulative_spend_enforced_by_policy": False,
        "live_limits_verified_from_server": False,
        "user_consent_verified": False,
        "privy_policy_created": False,
        "wallet_signer_registered": False,
        "router02_live_path_enabled": False,
        "erc20_allowance_plan_implemented": False,
        "untrusted_price_slippage_protected_by_policy": False,
        "privy_abi_semantics_tested": False,
        "execution_enabled": False,
        "safe_to_sign": False,
        "can_submit": False,
    }
