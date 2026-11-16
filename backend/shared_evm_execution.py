"""Shared EVM exactInputSingle transaction encoding for MiniNex assets.

This is a *real reusable* transaction construction primitive. Execution still
requires verified chain-specific DEX deployment, price quote, wallet balance,
allowance, user policy and durable once-only submission. The ETH LIVE executor
uses this same primitive; other EVM chains never inherit Ethereum permissions.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from .live_asset_registry import get_asset

ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
EXACT_INPUT_SINGLE = "0x04e45aaf"
NETWORK_ASSETS = {
    "ethereum-mainnet": frozenset(("ETH", "LINK", "UNI")),
    "bsc-mainnet": frozenset(("BNB",)),
    "polygon-pos-mainnet": frozenset(("POL",)),
    "avalanche-c-mainnet": frozenset(("AVAX",)),
}
CHAIN_IDS = {
    "ethereum-mainnet": 1,
    "bsc-mainnet": 56,
    "polygon-pos-mainnet": 137,
    "avalanche-c-mainnet": 43114,
}

@dataclass(frozen=True)
class EvmPair:
    asset: str
    chain_id: int
    network: str
    token_in: str
    token_out: str
    router: str
    fee: int


def chain_for_asset(asset: str) -> tuple[int, str]:
    """Map one asset to its chain; never infer network from caller calldata."""
    info = get_asset(asset)
    network = info.blockchain
    if asset not in NETWORK_ASSETS.get(network, frozenset()):
        raise ValueError("Asset has no supported EVM execution family")
    if info.chain_id != CHAIN_IDS[network]:
        raise ValueError("Network registry chain mismatch")
    return info.chain_id, network


def _word(number: int) -> str:
    if type(number) is not int or not 0 <= number < 2**256:
        raise ValueError("Invalid EVM uint256 argument")
    return f"{number:064x}"


def unsigned_exact_input_single(*, pair: EvmPair, wallet: str,
                                amount_in: int, min_out: int) -> dict:
    """Reusable V3 exact-input encoding; no arbitrary spender is accepted.

    The pair must be supplied by the chain-verified server adapter. It is not
    a client-controlled endpoint and NEVER signs or broadcasts anything.
    """
    chain, network = chain_for_asset(pair.asset)
    if (type(pair.chain_id) is not int or pair.chain_id != chain
            or pair.network != network
            or type(wallet) is not str or not ADDRESS.fullmatch(wallet)
            or type(pair.router) is not str or not ADDRESS.fullmatch(pair.router)
            or type(pair.token_in) is not str or not ADDRESS.fullmatch(pair.token_in)
            or type(pair.token_out) is not str or not ADDRESS.fullmatch(pair.token_out)
            or pair.token_in.lower() == pair.token_out.lower()
            or type(pair.fee) is not int or pair.fee not in (100, 500, 3000, 10000)
            or type(amount_in) is not int or not 0 < amount_in < 10**45
            or type(min_out) is not int or not 0 < min_out < 10**45):
        raise ValueError("Unverified EVM swap parameters")
    data = EXACT_INPUT_SINGLE + "".join((
        _word(int(pair.token_in, 16)),
        _word(int(pair.token_out, 16)),
        _word(pair.fee),
        _word(int(wallet, 16)),
        _word(amount_in),
        _word(min_out),
        _word(0),
    ))
    return {
        "from": wallet, "to": pair.router, "chainId": chain,
        "value": "0", "data": data,
    }


WALLET_ID = re.compile(r"^[A-Za-z0-9_-]{6,160}$")
CALLDATA = re.compile(r"^0x(?:[0-9a-fA-F]{2})*$")


def bound_evm_wallet_rpc(*, asset: str, wallet_id: str, wallet: str,
                         tx: dict) -> tuple[str, dict]:
    """Construct the same Privy RPC envelope for all six EVM assets.

    This is only a TRANSPORT serializer, not authorization to execute any
    transaction. The verified, non-user-controlled chain-specific adapter
    must validate spender, token, exact amounts, slippage, user grant and
    claim its durable once-only order before calling any broadcaster.
    """
    chain_id, _ = chain_for_asset(asset)
    if (type(wallet_id) is not str or not WALLET_ID.fullmatch(wallet_id)
            or type(wallet) is not str or not ADDRESS.fullmatch(wallet)
            or type(tx) is not dict
            or type(tx.get("chainId")) is not int
            or tx["chainId"] != chain_id
            or type(tx.get("from")) is not str
            or tx["from"].lower() != wallet.lower()
            or type(tx.get("to")) is not str or not ADDRESS.fullmatch(tx["to"])
            or type(tx.get("data")) is not str
            or not CALLDATA.fullmatch(tx["data"])
            or type(tx.get("value")) is not str
            or not tx["value"].isdigit()
            or len(tx["value"]) > 79):
        raise ValueError("EVM Privy wallet transaction identity mismatch")
    # The server-validated transaction is the only source for the RPC body.
    return "https://api.privy.io/v1/wallets/" + wallet_id + "/rpc", {
        "method": "eth_sendTransaction",
        "caip2": f"eip155:{chain_id}",
        "params": {"transaction": {
            "to": tx["to"], "data": tx["data"], "value": tx["value"],
        }},
    }
