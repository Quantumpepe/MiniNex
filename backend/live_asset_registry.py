"""Authoritative MiniNex LIVE asset registry and global readiness gate.

The twelve user-facing assets include native coins and Ethereum tokens.
Only independently verified native routes/token contracts may execute. Stablecoin tickers do NOT imply that an issuer supports
the same asset on every chain. All trading stays disabled until individually
verified native routes and one separately authenticated operator rollout.

This module does not create wallets, keys, deposits, approvals or orders.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import os

from fastapi import HTTPException

# BTC/ETH remain the only PAPER engines; all real execution is still disabled.
ASSET_ORDER = ("BTC", "ETH", "BNB", "SOL", "XRP", "TRX", "POL",
               "AVAX", "LINK", "DOGE", "UNI", "ADA")

# All capabilities listed as "candidate" here need real venue, issuer,
# address and decimals verification before any coin is eligible to trade.
@dataclass(frozen=True)
class CoinNetwork:
    symbol: str
    name: str
    blockchain: str
    execution_family: str
    chain_id: int | None
    wallet_approach: str
    stablecoin_candidates: tuple[str, ...]
    settlement_route: str
    special_requirements: str


_REGISTRY_UNSORTED = (
    CoinNetwork(
        "BTC", "Bitcoin", "bitcoin-mainnet", "bitcoin-utxo", None,
        "Privy bitcoin-segwit wallet: owner and automated signer capabilities must be verified",
        ("USDC", "USDT"), "external-provider-required",
        "Native BTC has no native ERC-20 USDC/USDT; validate cross-chain custodian,"
        " finality, settlement network, fees and refunds before trading",
    ),
    CoinNetwork(
        "ETH", "Ethereum", "ethereum-mainnet", "evm", 1,
        "Privy embedded Ethereum wallet; bounded additional signer policy required",
        ("USDC", "USDT"), "same-chain-candidate",
        "Uniswap quote and wallet checks exist, but unsigned calldata verification,"
        " grants, approvals, nonce locks and receipts remain incomplete",
    ),
    CoinNetwork(
        "BNB", "BNB", "bsc-mainnet", "evm", 56,
        "Use a separately authorized Privy EVM signer on chain 56",
        ("USDT",), "same-chain-token-validation-required",
        "BNB Smart Chain USDT representations need a verified token contract;"
        " Circle-native USDC must never be assumed on BSC",
    ),
    CoinNetwork(
        "POL", "Polygon", "polygon-pos-mainnet", "evm", 137,
        "Privy EVM wallet on Polygon PoS (chain 137); independent wallet authorization required",
        ("USDC",), "same-chain-token-validation-required",
        "POL is the native gas token. Only Circle-native Polygon USDC is"
        " a candidate; do not substitute bridged USDC.e. Validate Polygon"
        " routers, token contracts, fees, gas and transaction receipts"
        " before allowing any POL trade; Polygon USDT is not enabled yet",
    ),
    CoinNetwork(
        "SOL", "Solana", "solana-mainnet", "solana", None,
        "Dedicated user-owned Solana wallet with confirmed signer capabilities",
        ("USDC", "USDT"), "same-chain-token-validation-required",
        "Verify SPL token mint, Jupiter/DEX quote, blockhash expiry, signatures,"
        " gas and finalized token balances",
    ),
    CoinNetwork(
        "XRP", "XRP", "xrpl-mainnet", "xrpl", None,
        "Dedicated XRPL wallet; exact Privy signing compatibility to be tested",
        ("USDC",), "issuer-and-trustline-validation-required",
        "Verify XRPL issued USDC issuer, trustlines, reserve requirements,"
        " DEX offer quality and ledger finality",
    ),
    CoinNetwork(
        "ADA", "Cardano", "cardano-mainnet", "cardano-eutxo", None,
        "Dedicated Cardano wallet and reviewed automation signing mechanism",
        ("USDCx",), "bridge-or-local-token-validation-required",
        "USDCx is Cardano's USDC-backed asset, NOT native Circle USDC;"
        " token policy, local DEX, UTXO fees and bridging need independent checks",
    ),
    CoinNetwork(
        "AVAX", "Avalanche", "avalanche-c-mainnet", "evm", 43114,
        "Privy Ethereum-format EVM wallet on Avalanche C-Chain; independent chain authorization",
        ("USDC", "USDT"), "same-chain-token-validation-required",
        "Validate C-Chain token issuers, router, gas, slippage and settlement before any swap",
    ),
    CoinNetwork(
        "LINK", "Chainlink", "ethereum-mainnet", "evm-erc20", 1,
        "Privy embedded Ethereum wallet; LINK ERC-20 uses ETH gas",
        ("USDC", "USDT"), "ethereum-erc20-validation-required",
        "Verify Ethereum LINK token address, decimals, allowances, fee and route",
    ),
    CoinNetwork(
        "DOGE", "Dogecoin", "dogecoin-mainnet", "dogecoin-utxo", None,
        "Dedicated user-owned native Dogecoin wallet and separate signer required",
        ("USDC", "USDT"), "external-provider-required",
        "No native stablecoin settlement on Dogecoin mainnet; independently vet "
        "cross-chain venue, confirmations, fees and refund mechanism",
    ),
    CoinNetwork(
        "UNI", "Uniswap", "ethereum-mainnet", "evm-erc20", 1,
        "Privy embedded Ethereum wallet; UNI ERC-20 uses ETH gas",
        ("USDC", "USDT"), "ethereum-erc20-validation-required",
        "Verify Ethereum UNI token contract, decimals, allowances, price impact and receipts",
    ),
    CoinNetwork(
        "TRX", "TRON", "tron-mainnet", "tron", None,
        "Dedicated TRON wallet and independently tested automated signer",
        ("USDT",), "trc20-validation-required",
        "Verify official TRC-20 USDT, energy/bandwidth, contract calls and"
        " receipts; Circle deprecated USDC on TRON, so do not offer it",
    ),
)
REGISTRY = tuple(sorted(_REGISTRY_UNSORTED, key=lambda coin: ASSET_ORDER.index(coin.symbol)))
BY_SYMBOL = {coin.symbol: coin for coin in REGISTRY}

# Deliberately no hidden feature flag/env switch can bypass these controls.
# Later LIVE enabling requires independently reviewed policy + DB rollout,
# a separated operator identity, signed requests and testable release gates.
_LIVE_ENGINE_ENABLED = False
_OPERATOR_ACTIVATION_ROUTE_AVAILABLE = False
_MANDATORY_SAFETY_GATES = (
    "verified_user_wallet",
    "restricted_wallet_signer",
    "approved_trade_venue",
    "verified_settlement_token",
    "real_executable_quote",
    "atomic_capital_reservation",
    "no_duplicate_order_nonce",
    "transaction_simulation_and_calldata_checks",
    "broadcast_receipt_reconciliation",
    "emergency_stop_and_stable_exit",
    "tax_grade_net_accounting",
    "independent_mainnet_acceptance",
)


def get_asset(symbol: str) -> CoinNetwork:
    if symbol not in BY_SYMBOL:
        raise HTTPException(404, "Unsupported LIVE asset")
    return BY_SYMBOL[symbol]


def _coin_readiness(coin: CoinNetwork) -> dict:
    # Existing ETH price quote work is still only read-only preflight.
    # No other chain claims executable quotes without actual integration.
    quote_prepared = bool(os.getenv("MININEX_UNISWAP_API_KEY", "").strip()) if coin.symbol == "ETH" else False
    safety_gates = {key: False for key in _MANDATORY_SAFETY_GATES}
    return {
        **asdict(coin),
        "stablecoin_candidates": list(coin.stablecoin_candidates),
        "readonly_eth_quote_provider_configured": quote_prepared,
        "safety_gates": safety_gates,
        "live_execution_ready": False,
        "allow_user_trading": False,
        "mode": "LIVE_PREPARATION_ONLY",
    }


def get_global_live_readiness() -> dict:
    rows = [_coin_readiness(coin) for coin in REGISTRY]
    return {
        "mode": "MULTICHAIN_LIVE_PREPARATION",
        "ordered_assets": list(ASSET_ORDER),
        "stablecoins_are_separate_settlement_assets": True,
        "assets": {row["symbol"]: row for row in rows},
        "global_trading_enabled": False,
        "all_chains_ready": False,
        "operator_go_live_available": _OPERATOR_ACTIVATION_ROUTE_AVAILABLE,
        "one_click_activation_enabled": False,
        "global_activation_blocked": True,
        "required_checks_per_asset": list(_MANDATORY_SAFETY_GATES),
        "description": (
            "BTC and ETH can be prepared independently for LIVE only after all"
            " per-asset security and settlement checks pass. Ten other"
            " coins remain display-only; no auto-capital rotation."
        ),
    }


def require_live_engine(symbol: str) -> None:
    """Single fail-closed dispatch guard for all future adapters.

    Even a forged enabled front-end or environment flag cannot invoke a
    real swap through this route. There is no current LIVE submit endpoint.
    """
    get_asset(symbol)
    if not _LIVE_ENGINE_ENABLED:
        raise HTTPException(503, "LIVE trading not enabled")
    raise HTTPException(503, "No verified execution adapter is available")
