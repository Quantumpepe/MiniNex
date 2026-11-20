"""Single owner-authenticated original BTC/SOL/XRP source readiness summary.

Read-only integration of the separately implemented native SELL and
Ethereum stablecoin BUY preflights. No unsigned/signed PSBT, Solana message,
XRPL Payment, remote policy identifier, vault deposit instruction, or private
quote memo is returned to a user interface. The detailed internal builders
remain independent and never grant wallet authorization.
"""
from __future__ import annotations

from fastapi import HTTPException

from .native_btc_original_preflight import prepare_original_btc_unsigned_psbt
from .native_sol_owner_preflight import prepare_original_sol_unsigned_owner_transfer
from .native_xrp_owner_preflight import prepare_original_xrp_unsigned_owner_payment
from .native_eth_native_buy_preflight import preflight_original_native_eth_buy
from .native_privy_owner_signer import get_original_native_privy_signer_status
from .storage import _safe_did

_SETTLEMENTS = {"BTC": ("USDC", "USDT"),
                "SOL": ("USDC", "USDT"),
                "XRP": ("USDC",)}


async def read_original_native_source_readiness(
    did: str, *, asset: str, side: str, stable: str, amount: str,
) -> dict:
    """Prove an exact original source route; distinguish from LIVE permission."""
    did = _safe_did(did)
    if (asset not in _SETTLEMENTS or side not in ("BUY", "SELL")
            or stable not in _SETTLEMENTS.get(asset, ())
            or type(amount) is not str or not amount or len(amount) > 55):
        raise HTTPException(422, "Unsupported original native source request")
    owner = await get_original_native_privy_signer_status(did, asset=asset)
    if (type(owner) is not dict
            or owner.get("asset") != asset
            or owner.get("original_wallet_link_verified") is not True
            or owner.get("can_submit") is not False
            or owner.get("execution_enabled") is not False):
        raise HTTPException(409, "Original native Privy owner wallet not available")

    if side == "BUY":
        source = await preflight_original_native_eth_buy(
            did, asset=asset, stable=stable, amount=amount)
        if (source.get("mode") !=
                "THORCHAIN_ETH_ORIGINAL_NATIVE_BUY_NETWORK_PREFLIGHT"
                or source.get("chain_id_verified") is not True
                or source.get("original_router_allowance_verified") is not True
                or source.get("source_funds_and_gas_verified") is not True
                or source.get("pending_nonce_and_gas_estimate_verified") is not True
                or source.get("asset") != asset
                or source.get("stable") != stable):
            raise HTTPException(409, "Original Ethereum stablecoin source unverified")
        source_network = "ethereum-mainnet"
        target_network = owner["network"]
        checked = {
            "ethereum_chain_id_verified": True,
            "source_balance_and_network_fee_verified": True,
            "current_source_nonce_verified": True,
            "original_router_allowance_verified": True,
            "original_source_transaction_unsigned": True,
        }
    elif asset == "BTC":
        source = await prepare_original_btc_unsigned_psbt(
            did, stable=stable, amount=amount)
        unsigned = source.get("unsigned_psbt")
        if (source.get("mode") != "ORIGINAL_BTC_PSBT_OWNER_PREFLIGHT_READ_ONLY"
                or type(unsigned) is not dict
                or unsigned.get("psbt_created") is not True
                or source.get("current_esplora_unspent_reported") is not True
                or source.get("bip143_signature_digest_preview", {}).get(
                    "signature_requests_performed") is not False):
            raise HTTPException(409, "Original BTC source not independently verified")
        source_network = "bitcoin-mainnet"
        target_network = "ethereum-mainnet"
        checked = {
            "original_funding_outpoints_verified": True,
            "source_balance_and_network_fee_verified": True,
            "unsigned_bip174_psbt_compiled": True,
            "bip143_message_digests_verified": True,
        }
    elif asset == "SOL":
        source = await prepare_original_sol_unsigned_owner_transfer(
            did, stable=stable, amount=amount)
        if (source.get("mode") != "ORIGINAL_SOL_SOURCE_UNSIGNED_MAINNET_PREFLIGHT"
                or source.get("solana_mainnet_genesis_verified") is not True
                or source.get("current_owner_blockhash_and_balance_verified") is not True
                or source.get("real_message_fee_verified") is not True):
            raise HTTPException(409, "Original Solana source not independently verified")
        source_network = "solana-mainnet"
        target_network = "ethereum-mainnet"
        checked = {
            "solana_mainnet_genesis_verified": True,
            "source_balance_and_network_fee_verified": True,
            "recent_original_blockhash_verified": True,
            "original_unsigned_transfer_compiled": True,
        }
    else:
        source = await prepare_original_xrp_unsigned_owner_payment(
            did, amount=amount)
        if (source.get("mode") != "ORIGINAL_XRP_SOURCE_UNSIGNED_MAINNET_PREFLIGHT"
                or source.get("xrpl_mainnet_id_verified") is not True
                or source.get("source_account_and_fee_verified") is not True):
            raise HTTPException(409, "Original XRP source not independently verified")
        source_network = "xrpl-mainnet"
        target_network = "ethereum-mainnet"
        checked = {
            "xrpl_mainnet_id_verified": True,
            "source_balance_and_network_fee_verified": True,
            "account_sequence_and_reserve_verified": True,
            "original_unsigned_payment_compiled": True,
        }

    # The source builders must not set an execution flag and must still
    # refer to the independently checked native owner's exact coin.
    if (source.get("asset") != asset
            or source.get("source_transaction_sent") is not False
            or source.get("execution_enabled") is not False
            or source.get("can_submit") is not False
            or source.get("wallet_signed") is not False):
        raise HTTPException(409, "Original native route unexpectedly executable")
    if side == "SELL":
        # Native owner and quote identity agree only after Privy readback;
        # the original source builders already verify it against Privy.
        if source.get("source_wallet") is None:
            raise HTTPException(409, "Original native source wallet unavailable")
    else:
        if source.get("destination_wallet") is None:
            raise HTTPException(409, "Original native destination wallet unavailable")

    return {
        "mode": "OWNER_NATIVE_SOURCE_READINESS_ONLY",
        "asset": asset, "side": side, "stable": stable,
        "input_amount": amount, "source_network": source_network,
        "destination_network": target_network,
        "original_native_owner_wallet_link_verified": True,
        "original_native_wallet_remote_owner_verified": (
            owner.get("original_wallet_remote_owner_verified") is True),
        "native_privy_signer_reference_observed": (
            owner.get("additional_signer_reference_present") is True),
        "original_source_preflight_verified": True,
        **checked,
        "exact_native_privy_policy_contents_verified": False,
        "user_signed_coin_budget_ledger_verified": False,
        "durable_original_order_claim_verified": False,
        "source_chain_broadcast_verified": False,
        "crosschain_swap_filled": False,
        "destination_settlement_finalized": False,
        "performance_fee_assessed": False,
        "wallet_signed": False,
        "execution_enabled": False, "can_submit": False,
    }
