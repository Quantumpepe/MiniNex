"""Strict, read-only semantic review for a *narrow* Universal Router 2.1.2 flow.

This is NOT a signer, an execution authorizer or general Uniswap decoder.
Only one-hop V3 EXACT_INPUT ETH<->USDC/USDT with a mandatory wrap/unwrap
is recognized. Unknown commands, cross-chain routes, permits, fees and
recipient changes fail closed. Review does not expose calldata to the app.

The caller must first validate the full outer router ABI envelope and gas
transaction using inspect_uniswap_swap_tx. All real orders remain disabled.
"""
from __future__ import annotations

import re

WETH_MAINNET = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
_ROUTER_SENDER = "0x" + "0" * 39 + "1"
_ROUTER_THIS = "0x" + "0" * 39 + "2"
_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_ALLOWED_FEES = (100, 500, 3000, 10000)


def _word(data: bytes, position: int) -> int:
    if position < 0 or position % 32 or position + 32 > len(data):
        raise ValueError("Truncated router command ABI")
    return int.from_bytes(data[position:position + 32], "big")


def _address(word: int) -> str:
    if word >> 160:
        raise ValueError("Non-canonical router address")
    return "0x" + word.to_bytes(20, "big").hex()


def _aligned(length: int) -> int:
    return (length + 31) // 32 * 32


def _two_words(data: bytes) -> tuple[str, int]:
    if len(data) != 64:
        raise ValueError("Unexpected wrap/unwrap arguments")
    return _address(_word(data, 0)), _word(data, 32)


def _v3_exact_input(data: bytes) -> dict:
    # Universal Router v2.1.2 EXACT_IN has SIX ABI args, including the
    # added uint256[] minHopPriceX36. Old five-field ABI is rejected.
    if len(data) < 192 or len(data) % 32:
        raise ValueError("Truncated V3 swap input")
    recipient = _address(_word(data, 0))
    amount_in = _word(data, 32)
    amount_out_min = _word(data, 64)
    path_offset = _word(data, 96)
    payer = _word(data, 128)
    prices_offset = _word(data, 160)
    if path_offset != 192 or payer not in (0, 1):
        raise ValueError("Non-canonical V3 swap head")
    path_length = _word(data, path_offset)
    if path_length != 43:
        raise ValueError("Only one-hop V3 ETH/stable routes are reviewed")
    start = path_offset + 32
    stop = start + path_length
    path_end = start + _aligned(path_length)
    if path_end > len(data) or any(data[stop:path_end]):
        raise ValueError("Invalid V3 path padding")
    if prices_offset != path_end or prices_offset + 32 != len(data):
        raise ValueError("Unexpected V3 price guard array or trailing calls")
    if _word(data, prices_offset) != 0:
        # Nonempty per-hop guards are not yet independently interpreted.
        raise ValueError("Unreviewed V3 per-hop price guards")
    path = data[start:stop]
    first = "0x" + path[0:20].hex()
    fee = int.from_bytes(path[20:23], "big")
    last = "0x" + path[23:43].hex()
    if fee not in _ALLOWED_FEES or first.lower() == last.lower():
        raise ValueError("Unsupported V3 pool or token path")
    if not amount_in or not amount_out_min:
        raise ValueError("Zero input or minimum output")
    return {
        "recipient": recipient, "amount_in": amount_in,
        "amount_out_min": amount_out_min, "payer_is_user": payer == 1,
        "token_in": first, "token_out": last,
    }


def _is_recipient(actual: str, expected: str, sentinel: str) -> bool:
    return actual.lower() in (expected.lower(), sentinel.lower())


def review_eth_v3_commands(*, commands: bytes, inputs: tuple[bytes, ...],
                           side: str, stable_token: str, wallet: str,
                           router: str, input_atomic: str,
                           minimum_atomic: str) -> dict:
    """Confirm quoted spend/minOut/path and final wallet for two fixed flows.

    BUY: V3_SWAP_EXACT_IN(stable->WETH, router) then UNWRAP_WETH(wallet).
    SELL: WRAP_ETH(router) then V3_SWAP_EXACT_IN(WETH->stable, wallet).
    No permit, transfer, sweep, callback, subplan, fee or arbitrary recipient
    is allowed. Matching this narrow pattern is NOT permission to sign.
    """
    if (type(commands) is not bytes or type(inputs) is not tuple
            or len(inputs) != 2 or not all(type(v) is bytes for v in inputs)
            or side not in ("BUY", "SELL")
            or not all(isinstance(v, str) and _ADDR.fullmatch(v)
                       for v in (stable_token, wallet, router))
            or not all(isinstance(v, str) and v.isascii() and v.isdecimal()
                       and len(v) < 50 for v in (input_atomic, minimum_atomic))):
        raise ValueError("Invalid V3 route review input")
    spend = int(input_atomic)
    minimum = int(minimum_atomic)
    if spend <= 0 or minimum <= 0:
        raise ValueError("Invalid quoted spending or minimum")
    if side == "BUY":
        if commands != b"\x00\x0c":
            raise ValueError("Unreviewed ETH purchase command sequence")
        swap = _v3_exact_input(inputs[0])
        unwrap_to, unwrap_min = _two_words(inputs[1])
        if (swap["token_in"].lower() != stable_token.lower()
                or swap["token_out"].lower() != WETH_MAINNET.lower()
                or swap["amount_in"] != spend
                or swap["amount_out_min"] < minimum
                or not swap["payer_is_user"]
                or not _is_recipient(swap["recipient"], router, _ROUTER_THIS)
                or not _is_recipient(unwrap_to, wallet, _ROUTER_SENDER)
                or unwrap_min < minimum):
            raise ValueError("ETH buy route does not match the verified quote")
    else:
        if commands != b"\x0b\x00":
            raise ValueError("Unreviewed ETH sale command sequence")
        wrap_to, wrap_amount = _two_words(inputs[0])
        swap = _v3_exact_input(inputs[1])
        if (not _is_recipient(wrap_to, router, _ROUTER_THIS)
                or wrap_amount != spend
                or swap["token_in"].lower() != WETH_MAINNET.lower()
                or swap["token_out"].lower() != stable_token.lower()
                or swap["amount_in"] != spend
                or swap["amount_out_min"] < minimum
                or swap["payer_is_user"]
                or not _is_recipient(swap["recipient"], wallet, _ROUTER_SENDER)):
            raise ValueError("ETH sell route does not match the verified quote")
    return {
        "router_command_pattern_verified": True,
        "router_commands_decoded": True,
        "router_command_contents_verified": True,
        "router_recipients_verified": True,
        "router_quote_bounds_verified": True,
        "router_path_type": "V3_SINGLE_HOP_ONLY",
    }
