"""Ethereum-settled spot policy for BTC and XRP trading inventory.

Native deposit/withdrawal wallets are deliberately NOT this inventory.
Only the Ethereum wallet's actual wrapped token balance is eligible.
The caller must still obtain a live quote, verify the remote policy, bind
the owner-approved budget and reconcile the actual token receipts.
"""
from __future__ import annotations

import re
from eth_abi import encode, decode
from eth_utils import keccak

from .eth_swaprouter02_preflight import SWAPROUTER02_MAINNET, WETH_MAINNET
from .live_eth_quotes import STABLES
from .eth_privy_user_policy_blueprint import _tx, _call, _expiry, _positive_atomic
from .shared_evm_single_grant_policy import ERC20_APPROVE_ABI

WRAPPED_SPOT_POLICY = "ETHEREUM_WRAPPED_SPOT_V1"
# Versioned immutable token identities; not user-supplied contract addresses.
WRAPPED_SPOT_TOKENS = {
    "BTC": ("0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", 8),
    "XRP": ("0x39fBBABf11738317a448031930706cd3e612e1B9", 18),
}
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_ID = re.compile(r"^[A-Za-z0-9_-]{6,160}$")
EXACT_INPUT_ABI = [{
    "type": "function", "name": "exactInput", "stateMutability": "payable",
    "inputs": [{"name": "params", "type": "tuple", "components": [
        {"name": "path", "type": "bytes"},
        {"name": "recipient", "type": "address"},
        {"name": "amountIn", "type": "uint256"},
        {"name": "amountOutMinimum", "type": "uint256"},
    ]}],
    "outputs": [{"name": "amountOut", "type": "uint256"}],
}]
_SELECTOR = keccak(text="exactInput((bytes,address,uint256,uint256))")[:4]
_TUPLE = "(bytes,address,uint256,uint256)"


def wrapped_spot_route(asset, stable, side):
    if asset not in WRAPPED_SPOT_TOKENS or stable not in STABLES or side not in ("BUY", "SELL"):
        raise ValueError("Unsupported Ethereum wrapped spot route")
    token, decimals = WRAPPED_SPOT_TOKENS[asset]
    tokens = [STABLES[stable], token] if asset == "BTC" else [STABLES[stable], WETH_MAINNET, token]
    fees = [500] if asset == "BTC" else [500, 10000]
    if side == "SELL":
        tokens.reverse()
        fees.reverse()
    path = bytes.fromhex(tokens[0][2:])
    for fee, destination in zip(fees, tokens[1:]):
        path += fee.to_bytes(3, "big") + bytes.fromhex(destination[2:])
    return {
        "asset": asset, "stable": stable, "side": side,
        "chain_id": 1, "network": "ethereum-mainnet",
        "router": SWAPROUTER02_MAINNET,
        "source_token": tokens[0], "destination_token": tokens[-1],
        "source_decimals": 6 if side == "BUY" else decimals,
        "destination_decimals": decimals if side == "BUY" else 6,
        "path": "0x" + path.hex(),
    }


def _atomic_int(value):
    if type(value) is not int or not 0 < value < 2**256:
        raise ValueError("Exact positive token atomic amount required")
    return value


def wrapped_spot_transaction(*, asset, stable, side, wallet, amount_atomic, min_out_atomic):
    """Compile a quote-bound swap; WETH intermediate never enters user inventory."""
    if type(wallet) is not str or not _ADDRESS.fullmatch(wallet):
        raise ValueError("Original Ethereum owner required")
    route = wrapped_spot_route(asset, stable, side)
    values = (bytes.fromhex(route["path"][2:]), wallet.lower(),
              _atomic_int(amount_atomic), _atomic_int(min_out_atomic))
    data = "0x" + (_SELECTOR + encode([_TUPLE], [values])).hex()
    return {"from": wallet.lower(), "to": route["router"].lower(),
            "chainId": 1, "value": "0", "data": data}


def inspect_wrapped_spot_transaction(tx, *, asset, stable, side, wallet,
                                     amount_atomic, min_out_atomic):
    expected = wrapped_spot_transaction(asset=asset, stable=stable, side=side,
        wallet=wallet, amount_atomic=amount_atomic, min_out_atomic=min_out_atomic)
    if type(tx) is not dict or tx != expected:
        raise ValueError("Wrapped spot transaction differs from original quote")
    decoded = decode([_TUPLE], bytes.fromhex(tx["data"][10:]))[0]
    if decoded[0].hex() != wrapped_spot_route(asset, stable, side)["path"][2:]:
        raise ValueError("Wrapped spot path mismatch")
    return expected


def build_wrapped_spot_policy(*, asset, stable, wallet, owner_id, executor_id,
                              buy_source_atomic_cap, sell_source_atomic_cap,
                              expires_at, now):
    if (type(wallet) is not str or not _ADDRESS.fullmatch(wallet)
            or type(owner_id) is not str or not _ID.fullmatch(owner_id)
            or type(executor_id) is not str or not _ID.fullmatch(executor_id)
            or owner_id == executor_id or type(now) is not int or now <= 0
            or type(expires_at) is not int or not now < expires_at <= now + 30 * 86400):
        raise ValueError("Original wallet, distinct owner and signer, valid expiry required")
    rules = []
    for side, cap in (("BUY", buy_source_atomic_cap), ("SELL", sell_source_atomic_cap)):
        route = wrapped_spot_route(asset, stable, side)
        cap = _positive_atomic(cap)
        router = route["router"]
        common = [_tx("chain_id", "eq", "1"), _expiry(expires_at), _tx("value", "eq", "0")]
        rules.append({
            "name": f"MiniNex {asset} {side} {stable} wrapped exactInput",
            "method": "eth_sendTransaction", "action": "ALLOW",
            "conditions": common + [
                _tx("to", "eq", router),
                _call("function_name", "eq", "exactInput", EXACT_INPUT_ABI),
                _call("exactInput.params.path", "eq", route["path"], EXACT_INPUT_ABI),
                _call("exactInput.params.recipient", "eq", wallet, EXACT_INPUT_ABI),
                _call("exactInput.params.amountIn", "gt", "0", EXACT_INPUT_ABI),
                _call("exactInput.params.amountIn", "lte", cap, EXACT_INPUT_ABI),
                _call("exactInput.params.amountOutMinimum", "gt", "0", EXACT_INPUT_ABI),
            ],
        })
        approval = common + [
            _tx("to", "eq", route["source_token"]),
            _call("function_name", "eq", "approve", ERC20_APPROVE_ABI),
            _call("approve.spender", "eq", router, ERC20_APPROVE_ABI),
            _call("approve.value", "lte", cap, ERC20_APPROVE_ABI),
        ]
        rules.append({"name": f"MiniNex {asset} {side} {stable} wrapped allowance",
                      "method": "eth_sendTransaction", "action": "ALLOW",
                      "conditions": approval + [_call("approve.value", "gt", "0", ERC20_APPROVE_ABI)]})
        if side == "BUY" and stable == "USDT":
            rules.append({"name": f"MiniNex {asset} {side} USDT allowance reset",
                          "method": "eth_sendTransaction", "action": "ALLOW",
                          "conditions": approval + [_call("approve.value", "eq", "0", ERC20_APPROVE_ABI)]})
    return {"version": "1.0", "chain_type": "ethereum",
            "name": f"MiniNex-wrapped-{asset}-{wallet[-8:].lower()}",
            "owner_id": owner_id, "rules": rules}
