"""Read-only WETH9 ETH/native conversion review for future MiniNex LIVE.

WETH9.deposit() and WETH9.withdraw(uint256) are simpler to restrict in
Privy than Universal Router nested execute commands. The two-stage plan
for native ETH has a NON-ATOMIC boundary: each stage requires a separately
verified chain receipt, exact wallet/token balance deltas and fee accounting.

This module NEVER creates a transaction, approval, wallet delegation,
policy, signature, broadcast, reservation or trading permission. It checks
a candidate call supplied by a future trusted internal integration, with
explicit preflight balances; caller-provided balances are NOT independently
verified by this module. No API route or worker uses these methods.
"""
from __future__ import annotations

import re

from .eth_swaprouter02_preflight import WETH_MAINNET, SWAPROUTER02_MAINNET
from .live_eth_quotes import STABLES

WETH9_DEPOSIT_SELECTOR = "0xd0e30db0"
WETH9_WITHDRAW_SELECTOR = "0x2e1a7d4d"
MIN_NATIVE_GAS_RESERVE_WEI = 5_000_000_000_000_000

_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_UNITS = re.compile(r"^(?:0|[1-9][0-9]*)$")


def _atomic(raw: object, *, permit_zero: bool = False) -> int:
    """Strict integer units, no floats, negative, hex or rounding."""
    if (type(raw) is not str or len(raw) > 50
            or _UNITS.fullmatch(raw) is None):
        raise ValueError("Invalid native/token atomic amount")
    amount = int(raw)
    if amount >= 10**45 or (amount == 0 and not permit_zero):
        raise ValueError("Invalid native/token atomic amount")
    return amount


def _value(raw: object) -> int:
    if type(raw) is int and raw >= 0:
        return _atomic(str(raw), permit_zero=True)
    return _atomic(raw, permit_zero=True)


def inspect_weth9_conversion(
    tx: object, *, wallet: str, operation: str,
    exact_amount_wei: str,
    native_balance_wei: str, weth_balance_wei: str,
) -> dict:
    """Check precisely one direct WETH9 deposit/withdraw candidate.

    Deposit: native ETH value is exactly the expected new WETH amount;
    native ETH left after deposit must cover a conservative gas reserve.
    Withdraw: tx.value = 0 and calldata contains exactly one uint256
    withdrawal amount; only the verified wallet's OWN WETH may be redeemed.
    Real gas and real balances must still be independently read from RPC.
    """
    if (type(tx) is not dict or type(wallet) is not str
            or _ADDR.fullmatch(wallet) is None
            or operation not in ("DEPOSIT", "WITHDRAW")):
        raise ValueError("Invalid WETH9 call review")
    sender, recipient, chain_id, data = (
        tx.get("from"), tx.get("to"), tx.get("chainId"), tx.get("data")
    )
    if (type(sender) is not str or type(recipient) is not str
            or type(chain_id) is not int or chain_id != 1
            or sender.lower() != wallet.lower()
            or recipient.lower() != WETH_MAINNET.lower()
            or type(data) is not str):
        raise ValueError("WETH9 wallet, contract or network mismatch")

    expected = _atomic(exact_amount_wei)
    native = _atomic(native_balance_wei, permit_zero=True)
    weth = _atomic(weth_balance_wei, permit_zero=True)
    actual_value = _value(tx.get("value"))

    if operation == "DEPOSIT":
        if (data.lower() != WETH9_DEPOSIT_SELECTOR
                or actual_value != expected
                or native - expected < MIN_NATIVE_GAS_RESERVE_WEI):
            raise ValueError("Unsafe or underfunded native ETH wrap")
        movement = "ETH_TO_WETH"
    else:
        expected_calldata = (
            WETH9_WITHDRAW_SELECTOR + expected.to_bytes(32, "big").hex()
        )
        if (data.lower() != expected_calldata
                or actual_value != 0
                or weth < expected
                or native < MIN_NATIVE_GAS_RESERVE_WEI):
            raise ValueError("Unsafe or underfunded WETH to native ETH unwrap")
        movement = "WETH_TO_ETH"

    return {
        "mode": "WETH9_NATIVE_CONVERSION_REVIEW_ONLY",
        "asset": "ETH",
        "chain_id": 1,
        "weth_contract": WETH_MAINNET,
        "operation": operation,
        "movement": movement,
        "exact_amount_wei": str(expected),
        "wallet_address_matched": True,
        "contract_address_matched": True,
        "value_and_call_data_checked": True,
        "reported_balance_and_gas_reserve_checked": True,
        "independent_rpc_balance_proof": False,
        "independent_chain_simulation_verified": False,
        "confirmed_token_deltas_verified": False,
        "nonce_reserved": False,
        "privy_authorization_verified": False,
        "safe_to_sign": False,
        "can_submit": False,
        "execution_enabled": False,
    }


def native_eth_swap_stages(*, side: str, stable: str) -> dict:
    """Show fixed native ETH cycle: every leg stops for verified receipt.

    This does not create signing instructions, calldata or a ready-to-send
    plan. It intentionally cannot skip the inter-leg receipt, balance and
    allowance checks even if the first trade seemed profitable.
    """
    if type(side) is not str or side not in ("BUY", "SELL"):
        raise ValueError("Unknown ETH swap side")
    if type(stable) is not str or stable not in STABLES:
        raise ValueError("Unknown ETH stablecoin")

    if side == "BUY":
        stages = [
            {
                "name": "SWAP_STABLE_TO_WETH",
                "contract": SWAPROUTER02_MAINNET,
                "method": "exactInputSingle",
                "expected_output_asset": "WETH",
                "requires_confirmed_receipt": True,
            },
            {
                "name": "UNWRAP_RECEIVED_WETH",
                "contract": WETH_MAINNET,
                "method": "withdraw(uint256)",
                "expected_output_asset": "ETH",
                "amount_source": "INDEPENDENTLY_VERIFIED_WETH_TOKEN_DELTA",
                "requires_confirmed_receipt": True,
            },
        ]
    else:
        stages = [
            {
                "name": "WRAP_NATIVE_ETH",
                "contract": WETH_MAINNET,
                "method": "deposit()",
                "expected_output_asset": "WETH",
                "requires_confirmed_receipt": True,
            },
            {
                "name": "SWAP_CONFIRMED_WETH_TO_STABLE",
                "contract": SWAPROUTER02_MAINNET,
                "method": "exactInputSingle",
                "expected_output_asset": stable,
                "amount_source": "INDEPENDENTLY_VERIFIED_WETH_TOKEN_DELTA",
                "requires_confirmed_receipt": True,
            },
        ]

    return {
        "mode": "NATIVE_ETH_TWO_STAGE_BLUEPRINT_ONLY",
        "asset": "ETH",
        "chain_id": 1,
        "side": side,
        "settlement": stable,
        "stages": stages,
        "stages_are_non_atomic": True,
        "must_reconcile_each_stage": True,
        "must_check_allowances_and_new_fees_before_second_stage": True,
        "cannot_assume_quote_equals_actual_fill": True,
        "cannot_skip_pending_or_uncertain_first_stage": True,
        "wallet_displays_native_eth_only_after_confirmed_unwrap": True,
        "privy_policy_created": False,
        "privy_consent_verified": False,
        "execution_enabled": False,
        "safe_to_sign": False,
        "can_submit": False,
    }
