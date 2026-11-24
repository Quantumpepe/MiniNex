"""Authoritative metadata for MiniNex's twelve supported coin views.

This registry is NOT a trade-authorization gate. Concrete owner-authorized
execution lives in the ETH, shared EVM and native source dispatchers,
where current Privy consent, source funds, nonce and finality are checked.
An unauthenticated asset catalog cannot evaluate twelve users' live
transaction checks or require a separate operator rollout.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import os

from fastapi import HTTPException
from .ethereum_settlement_plan import settlement_route

# UI order for all twelve independent owner wallets and real-market engines.
ASSET_ORDER = ("BTC", "ETH", "BNB", "SOL", "XRP", "TRX", "POL",
               "AVAX", "LINK", "QNT", "UNI", "ARB")

# Human-readable network metadata; actual venue/token details come from
# exact source adapters rather than an additional global approval flag.
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
        "ARB", "Arbitrum", "arbitrum-one-mainnet", "evm-erc20", 42161,
        "Privy EVM wallet on Arbitrum One; ARB is an ERC-20 token, ETH pays network gas",
        ("USDC", "USDT"), "same-chain-token-validation-required",
        "ARB contract 0x912CE59144191C1204E64559FE8253a0e49E6548 "
        "(18 decimals); validate router, ETH gas, slippage and settlement before any swap",
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
        "QNT", "Quant", "ethereum-mainnet", "evm-erc20", 1,
        "Privy embedded Ethereum wallet; QNT ERC-20 uses ETH gas",
        ("USDC", "USDT"), "ethereum-erc20-validation-required",
        "Verify Ethereum QNT token contract, decimals, allowances, fee and route",
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

# Runtime transaction checks belong to each owner wallet and its actual
# source/receipt adapter, never to a fixed global list of False values.
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

# Concrete modules already provide these source paths. This is interface
# metadata, NOT a claim that an owner's Privy policy or native SELL signer
# has been verified. The native BUY route is funded from the user's ETH
# USDC/USDT wallet via THORChain; refunds/receipts are chain-specific.
_EVM_SOURCES = frozenset(("ETH","BNB","POL","AVAX","LINK","UNI","ARB","QNT"))
_NATIVE_SOURCES = frozenset(("BTC","SOL","XRP","TRX"))


def get_asset(symbol: str) -> CoinNetwork:
    if symbol not in BY_SYMBOL:
        raise HTTPException(404,"Unsupported LIVE asset")
    return BY_SYMBOL[symbol]


def _coin_readiness(coin: CoinNetwork) -> dict:
    """Show implemented source routes, without inventing owner approval.

    The owner-only readiness, authenticated session and transaction receipt
    endpoints evaluate actual chain data when the user requests them. A
    public registry must NOT manufacture a dozen permanent failed checks
    that accidentally override the user's valid Privy grant.
    """
    asset=coin.symbol
    return {
        **asdict(coin),
        "stablecoin_candidates":list(coin.stablecoin_candidates),
        # Legacy adapter candidates remain separate from the desired central
        # settlement topology. Neither is proof of live execution.
        "ethereum_settlement_candidates":["USDC","USDT"],
        "ethereum_settlement_chain_id":1,
        "ethereum_settlement_family":settlement_route(asset,"BUY","USDC")["family"],
        "source_route":(
            "OWNER_ETH_UNISWAP_ROUTER02" if asset=="ETH"
            else "OWNER_SCOPED_ROUTER02" if asset in _EVM_SOURCES
            else "OWNER_ETH_STABLE_THORCHAIN_NATIVE_BUY"
        ),
        "native_sell_status":(
            "REQUIRES_SIGNED_NATIVE_SELL_PATH" if asset in _NATIVE_SOURCES
            else "EVM_OWNER_GRANT_REQUIRED"
        ),
        # None means not evaluated for this user's real transaction, NOT
        # a permanent False gate or permission bypass.
        "safety_gates":{key:None for key in _MANDATORY_SAFETY_GATES},
        "checks_evaluated_at":"OWNER_AUTHORIZED_SEND_AND_RECEIPT",
        "live_execution_ready":None,
        "allow_user_trading":None,
        "mode":"PER_OWNER_EXECUTION_CAPABILITIES",
    }


def get_global_live_readiness() -> dict:
    rows=[_coin_readiness(coin) for coin in REGISTRY]
    return {
        "mode":"PER_OWNER_EXECUTION_CAPABILITIES",
        "ordered_assets":list(ASSET_ORDER),
        "stablecoins_are_separate_settlement_assets":True,
        "assets":{row["symbol"]:row for row in rows},
        # A public coin catalog has no owner/Privy token and therefore
        # cannot decide whether that user's trading session is ready.
        "global_trading_enabled":None,
        "all_chains_ready":None,
        "operator_go_live_available":None,
        "one_click_activation_enabled":None,
        "global_activation_blocked":False,
        "required_checks_per_asset":list(_MANDATORY_SAFETY_GATES),
        "description":(
            "Coin routes are not blocked by registry or operator flags. "
            "Actual execution uses the signed owner's current per-coin "
            "session, Privy grant, source balances and confirmed receipts."
        ),
    }


def require_live_engine(symbol:str)->None:
    """Legacy adapter lookup, not a separate operator/global kill switch.

    This MUST NOT be treated as a wallet grant. Every real submit must call
    its dedicated owner-authenticated, nonce-locked native/EVM executor.
    """
    get_asset(symbol)
