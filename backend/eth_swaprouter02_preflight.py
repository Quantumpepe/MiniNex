"""Independent, read-only SwapRouter02 exactInputSingle policy feasibility.

Do NOT use this code to sign, prepare allowances, or switch the existing
Universal Router ETH swap implementation. It only verifies a narrow
ERC20/WETH single-pool alternative that is easier to constrain with Privy
calldata conditions. It does NOT settle to native ETH or authorize trading.

SwapRouter02 Ethereum: Uniswap v3 deployments mainnet.
ExactInputSingleParams has seven *static* ABI fields (no deadline).
"""
from __future__ import annotations

import re

from .live_eth_quotes import STABLES, _valid_units

SWAPROUTER02_MAINNET = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"
WETH_MAINNET = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
EXACT_INPUT_SINGLE_SELECTOR = "0x04e45aaf"
EXACT_INPUT_SINGLE_ABI = [{
    "type": "function",
    "name": "exactInputSingle",
    "stateMutability": "payable",
    "inputs": [{
        "name": "params",
        "type": "tuple",
        "components": [
            {"name": "tokenIn", "type": "address"},
            {"name": "tokenOut", "type": "address"},
            {"name": "fee", "type": "uint24"},
            {"name": "recipient", "type": "address"},
            {"name": "amountIn", "type": "uint256"},
            {"name": "amountOutMinimum", "type": "uint256"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"},
        ],
    }],
    "outputs": [{"name": "amountOut", "type": "uint256"}],
}]
_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_CALL = re.compile(r"^0x[0-9a-fA-F]+$")
_ALLOWED_FEES = frozenset({100, 500, 3000, 10000})
_CALL_BYTES = 4 + 7 * 32


def _address_word(word: int) -> str:
    if word >> 160:
        raise ValueError("Non-canonical SwapRouter02 address")
    return "0x" + word.to_bytes(20, "big").hex()


def inspect_swaprouter02_exact_input(
    tx: object, *, wallet: str, stable: str, side: str,
    input_atomic: str, minimum_output_atomic: str,
) -> dict:
    """Validate a single, quote-bound ERC20/WETH call without execution.

    BUY: USDC/USDT -> WETH at the verified wallet (NOT native ETH).
    SELL: existing WETH -> USDC/USDT at the verified wallet (NOT native ETH
    spend). A later native wrap/unwrap process requires its own separate
    and carefully bounded authorization.
    """
    if (type(tx) is not dict or type(wallet) is not str
            or not _ADDR.fullmatch(wallet)
            or type(stable) is not str or stable not in STABLES
            or type(side) is not str or side not in ("BUY", "SELL")):
        raise ValueError("Invalid SwapRouter02 quote review")
    sender, to = tx.get("from"), tx.get("to")
    if (type(sender) is not str or type(to) is not str
            or sender.lower() != wallet.lower()
            or to.lower() != SWAPROUTER02_MAINNET.lower()
            or type(tx.get("chainId")) is not int or tx["chainId"] != 1):
        raise ValueError("Unexpected swap sender, router or network")
    native_value = tx.get("value")
    if not ((type(native_value) is int and native_value == 0)
            or (type(native_value) is str and native_value == "0")):
        raise ValueError("Native ETH funding is outside this ERC20-only review")
    calldata = tx.get("data")
    if (not isinstance(calldata, str) or len(calldata) != 2 + _CALL_BYTES * 2
            or not _CALL.fullmatch(calldata)
            or not calldata.lower().startswith(EXACT_INPUT_SINGLE_SELECTOR)):
        raise ValueError("Unsupported or malformed SwapRouter02 calldata")
    raw = bytes.fromhex(calldata[2:])
    data = raw[4:]
    words = [int.from_bytes(data[i:i + 32], "big")
             for i in range(0, len(data), 32)]
    token_in = _address_word(words[0])
    token_out = _address_word(words[1])
    fee = words[2]
    recipient = _address_word(words[3])
    amount_in = words[4]
    minimum_out = words[5]
    sqrt_limit = words[6]

    expected_in = STABLES[stable] if side == "BUY" else WETH_MAINNET
    expected_out = WETH_MAINNET if side == "BUY" else STABLES[stable]
    required_in = _valid_units(input_atomic)
    required_min = _valid_units(minimum_output_atomic)
    if (token_in.lower() != expected_in.lower()
            or token_out.lower() != expected_out.lower()
            or recipient.lower() != wallet.lower()
            or fee not in _ALLOWED_FEES
            or amount_in != required_in
            or minimum_out < required_min
            or sqrt_limit != 0):
        raise ValueError("SwapRouter02 route violates quote or wallet constraints")

    return {
        "mode": "ROUTER02_ERC20_WETH_PREVIEW_ONLY",
        "chain_id": 1,
        "router": SWAPROUTER02_MAINNET,
        "asset": "ETH",
        "stable": stable,
        "side": side,
        "selector_checked": True,
        "token_pair_verified": True,
        "recipient_verified": True,
        "quote_minimum_verified": True,
        "route_settlement": "WETH" if side == "BUY" else stable,
        "wallet_native_eth_settlement_complete": False,
        "router02_policy_live_authorized": False,
        "privy_wallet_grant_verified": False,
        "gas_balance_verified": False,
        "onchain_simulation_verified": False,
        "approval_or_weth_wrap_verified": False,
        "safe_to_sign": False,
        "execution_enabled": False,
        "can_submit": False,
    }


def swaprouter02_policy_scope() -> dict:
    """Information-only: never creates a Privy policy or wallet authority."""
    return {
        "mode": "ROUTER02_POLICY_COMPARISON_ONLY",
        "asset": "ETH",
        "chain_id": 1,
        "router": SWAPROUTER02_MAINNET,
        "method": "eth_sendTransaction",
        "calldata_function": "exactInputSingle",
        "function_selector": EXACT_INPUT_SINGLE_SELECTOR,
        "single_hop_token_pairs": ["USDC/WETH", "USDT/WETH", "WETH/USDC", "WETH/USDT"],
        "erc20_weth_review_available": True,
        "native_eth_trade_cycle_complete": False,
        "privy_policy_created": False,
        "privy_permission_granted": False,
        "execution_enabled": False,
        "can_submit": False,
        "explanation": (
            "This is a separately reviewed ERC20/WETH possibility, not a "
            "replacement for the existing native ETH Universal Router plan. "
            "Native ETH deposit/unwrap, exact user-scoped token permissions, "
            "allowances, gas, quote freshness, settlement and Privy consent "
            "must be implemented and independently verified first."
        ),
    }
