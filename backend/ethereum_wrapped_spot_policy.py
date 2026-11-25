"""Ethereum-settled spot policy for BTC, XRP and SOL trading inventory.

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
    # Wormhole Ethereum SOL; verified V3 SOL/WETH 0.3% pool.
    "SOL": ("0xD31a59c85aE9D8edEFeC411D448f90841571b89c", 9),
    # BTTC-bridged TRX (6 decimals), traded through the liquid V2 TRX/USDC pair.
    "TRX": ("0x50327c6c5a14DCaDE707ABad2E27eB517df87AB5", 6),
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
V2_EXACT_INPUT_ABI = [{
    "type": "function", "name": "swapExactTokensForTokens", "stateMutability": "payable",
    "inputs": [{"name": "amountIn", "type": "uint256"},
               {"name": "amountOutMin", "type": "uint256"},
               {"name": "path", "type": "address[]"},
               {"name": "to", "type": "address"}],
    "outputs": [{"name": "amountOut", "type": "uint256"}],
}]
_V2_TYPES = ["uint256", "uint256", "address[]", "address"]
_V2_SELECTOR = keccak(text="swapExactTokensForTokens(uint256,uint256,address[],address)")[:4]


def wrapped_swap_rule_name(asset, side, stable):
    return (f"MiniNex {asset} {side} {stable} wrapped V2 exact" if asset == "TRX"
            else f"MiniNex {asset} {side} {stable} wrapped exactInput")


def wrapped_swap_amount_field(asset):
    return "swapExactTokensForTokens.amountIn" if asset == "TRX" else "exactInput.params.amountIn"



def wrapped_spot_route(asset, stable, side):
    if asset not in WRAPPED_SPOT_TOKENS or stable not in STABLES or side not in ("BUY", "SELL"):
        raise ValueError("Unsupported Ethereum wrapped spot route")
    token, decimals = WRAPPED_SPOT_TOKENS[asset]
    if asset=="TRX":
        # Both directions use the verified V2 TRX/USDC pair. USDT has
        # an additional V2 USDT/USDC leg; no local Tron stablecoin.
        tokens=[STABLES[stable],token] if stable=="USDC" else [STABLES[stable],STABLES["USDC"],token]
        if side=="SELL":
            tokens.reverse()
        return {"asset":asset,"stable":stable,"side":side,"chain_id":1,
                "network":"ethereum-mainnet","router":SWAPROUTER02_MAINNET,
                "source_token":tokens[0],"destination_token":tokens[-1],
                "source_decimals":6,"destination_decimals":6,
                "protocol":"UNISWAP_V2","path":tokens}
    tokens = [STABLES[stable], token] if asset == "BTC" else [STABLES[stable], WETH_MAINNET, token]
    fees = [500] if asset == "BTC" else [500, 3000 if asset == "SOL" else 10000]
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
    amount=_atomic_int(amount_atomic)
    minimum=_atomic_int(min_out_atomic)
    if asset=="TRX":
        data="0x"+(_V2_SELECTOR+encode(_V2_TYPES,[amount,minimum,route["path"],wallet.lower()])).hex()
    else:
        values=(bytes.fromhex(route["path"][2:]),wallet.lower(),amount,minimum)
        data="0x"+(_SELECTOR+encode([_TUPLE],[values])).hex()
    return {"from": wallet.lower(), "to": route["router"].lower(),
            "chainId": 1, "value": "0", "data": data}


def inspect_wrapped_spot_transaction(tx, *, asset, stable, side, wallet,
                                     amount_atomic, min_out_atomic):
    expected = wrapped_spot_transaction(asset=asset, stable=stable, side=side,
        wallet=wallet, amount_atomic=amount_atomic, min_out_atomic=min_out_atomic)
    if type(tx) is not dict or tx != expected:
        raise ValueError("Wrapped spot transaction differs from original quote")
    # Canonical re-encoding above binds every byte, including the complete
    # V2 address array or V3 packed path, recipient and exact atomic amounts.
    return expected


def decode_wrapped_spot_amounts(data, *, asset, stable, side, wallet):
    """Only the canonical complete ABI encoding, including the approved path."""
    if type(data) is not str or not re.fullmatch(r"0x[0-9a-fA-F]+",data) or len(data)>2048:
        raise ValueError("Unverified wrapped spot calldata")
    try:
        raw=bytes.fromhex(data[2:])
        if asset=="TRX":
            if raw[:4]!=_V2_SELECTOR:
                raise ValueError("Unexpected V2 wrapped spot selector")
            amount,minimum,path,recipient=decode(_V2_TYPES,raw[4:])
        else:
            if raw[:4]!=_SELECTOR:
                raise ValueError("Unexpected wrapped spot selector")
            path,recipient,amount,minimum=decode([_TUPLE],raw[4:])[0]
        expected=wrapped_spot_transaction(asset=asset,stable=stable,side=side,wallet=wallet,
            amount_atomic=amount,min_out_atomic=minimum)
        if expected["data"]!=data.lower():
            raise ValueError("Wrapped spot ABI not canonical")
        return amount,minimum
    except Exception as exc:
        raise ValueError("Wrapped spot calldata differs from owner route") from exc


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
        if asset=="TRX":
            # Exact array equality, never a loose membership condition.
            # Privy's documented condition value schema accepts string[].
            # Publication/readback must preserve this exact condition; no
            # fallback to a broader router-only grant if Privy rejects it.
            fn="swapExactTokensForTokens"
            conditions=[
                _call("function_name","eq",fn,V2_EXACT_INPUT_ABI),
                _call(fn+".path","eq",route["path"],V2_EXACT_INPUT_ABI),
                _call(fn+".to","eq",wallet,V2_EXACT_INPUT_ABI),
                _call(fn+".amountIn","gt","0",V2_EXACT_INPUT_ABI),
                _call(fn+".amountIn","lte",cap,V2_EXACT_INPUT_ABI),
                _call(fn+".amountOutMin","gt","0",V2_EXACT_INPUT_ABI),
            ]
        else:
            conditions=[
                _call("function_name","eq","exactInput",EXACT_INPUT_ABI),
                _call("exactInput.params.path","eq",route["path"],EXACT_INPUT_ABI),
                _call("exactInput.params.recipient","eq",wallet,EXACT_INPUT_ABI),
                _call("exactInput.params.amountIn","gt","0",EXACT_INPUT_ABI),
                _call("exactInput.params.amountIn","lte",cap,EXACT_INPUT_ABI),
                _call("exactInput.params.amountOutMinimum","gt","0",EXACT_INPUT_ABI),
            ]
        rules.append({
            "name": wrapped_swap_rule_name(asset,side,stable),
            "method":"eth_sendTransaction","action":"ALLOW",
            "conditions":common+[_tx("to","eq",router)]+conditions,
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
