"""Shared native EVM coin wrap/unwrap call preparation.

ETH/WETH, BNB/WBNB, POL/WMATIC and AVAX/WAVAX use the same WETH9-style
deposit and withdraw ABI with chain-specific canonical contracts.
This module cannot sign, broadcast, move user funds or grant an allowance.
"""
from __future__ import annotations

from .shared_evm_execution import bound_evm_wallet_rpc, chain_for_asset
from .eth_swaprouter02_preflight import WETH_MAINNET
from .native_evm_v3_quote import CHAINS

_NATIVE_ASSETS = ("ETH", "BNB", "POL", "AVAX")


def bound_native_evm_conversion(
    *, asset: str, wallet_id: str, wallet: str,
    operation: str, amount_wei: int,
) -> tuple[str, dict]:
    """Construct canonical WETH9-style Privy RPC for one native coin.

    The caller is responsible for independently confirming that this
    chain's canonical wrapped token accepts WETH9 deposit()/withdraw().
    Trading permissions and settlement receipts are NOT inherited from ETH.
    """
    if (type(asset) is not str or asset not in _NATIVE_ASSETS
            or operation not in ("DEPOSIT", "WITHDRAW")
            or type(operation) is not str
            or type(amount_wei) is not int
            or not 0 < amount_wei < 10**45):
        raise ValueError("Invalid native EVM coin or conversion amount")
    chain_id, network = chain_for_asset(asset)
    if asset == "ETH":
        wrapped = WETH_MAINNET
    else:
        route = CHAINS[asset]
        if (route.chain_id != chain_id or route.network != network):
            raise ValueError("Native EVM chain identity mismatch")
        wrapped = route.wrapped
    calldata = ("0xd0e30db0" if operation == "DEPOSIT"
                else "0x2e1a7d4d" + f"{amount_wei:064x}")
    value = str(amount_wei) if operation == "DEPOSIT" else "0"
    return bound_evm_wallet_rpc(
        asset=asset, wallet_id=wallet_id, wallet=wallet,
        tx={"from":wallet,"to":wrapped,"chainId":chain_id,
            "data":calldata,"value":value},
    )
