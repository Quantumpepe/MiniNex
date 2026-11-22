"""One verified native EVM trade-cycle proof from separately finalized chain legs.

ETH/BNB/POL/AVAX need a SwapRouter02 ERC20 swap AND a different native
WETH9-style conversion transaction. LINK/UNI/ARB need only an ERC20 swap.
Actual on-chain ordering, quantities, network, owner and side are required.
This does not make or broadcast orders, book a position, or assess fees.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .shared_evm_swap_receipts import _pair

_NATIVE=frozenset(("ETH","BNB","POL","AVAX"))
_EVM=frozenset(("ETH","BNB","POL","AVAX","LINK","QNT","UNI","ARB"))


def _amount(value:object)->Decimal:
    if type(value) is not str or not 1<=len(value)<=75:
        raise ValueError("Actual finalized asset amount required")
    try:
        amount=Decimal(value)
    except InvalidOperation:
        raise ValueError("Invalid finalized asset quantity") from None
    if (not amount.is_finite() or not 0<amount<Decimal("1e25")
            or amount.as_tuple().exponent< -18):
        raise ValueError("Unverifiable finalized asset amount")
    return amount


def verify_evm_trade_cycle(
    *,asset:str,side:str,stable:str,wallet:str,
    swap_proof:object,native_proof:object=None,
)->dict:
    """Cryptographically observed receipts must agree on economic identity.

    Caller must obtain both receipts from independent owner-bound on-chain
    readers, not from arbitrary client JSON. This function only checks that
    their already verified facts form a coherent complete trade cycle.
    """
    if asset not in _EVM:
        raise ValueError("Unsupported EVM trading asset")
    pair=_pair(asset,stable,side)
    if (type(wallet) is not str
            or len(wallet)!=42 or not wallet.startswith("0x")
            or type(swap_proof) is not dict
            or swap_proof.get("mode")!="SHARED_EVM_FINALIZED_SWAP_VERIFICATION"
            or swap_proof.get("asset")!=asset
            or swap_proof.get("side")!=side
            or swap_proof.get("stable")!=stable
            or swap_proof.get("chain_id")!=pair["chain_id"]
            or swap_proof.get("network")!=pair["network"]
            or str(swap_proof.get("wallet_address","")).lower()!=wallet.lower()
            or swap_proof.get("state")!="FINALIZED_ERC20_SWAP_LEG"
            or swap_proof.get("transaction_finalized") is not True
            or swap_proof.get("swap_erc20_leg_verified") is not True
            or type(swap_proof.get("block_number")) is not int
            or swap_proof["block_number"]<=0
            or type(swap_proof.get("tx_hash")) is not str
            or len(swap_proof["tx_hash"])!=66):
        raise ValueError("Independently finalized owner swap proof required")
    source=_amount(swap_proof.get("source_spent_amount"))
    destination=_amount(swap_proof.get("destination_received_amount"))
    if asset in _NATIVE:
        if (type(native_proof) is not dict
                or native_proof.get("mode")!="SHARED_FINALIZED_NATIVE_EVM_CONVERSION"
                or native_proof.get("asset")!=asset
                or native_proof.get("chain_id")!=pair["chain_id"]
                or native_proof.get("network")!=pair["network"]
                or str(native_proof.get("wallet_address","")).lower()!=wallet.lower()
                or native_proof.get("state")!="FINALIZED_NATIVE_CONVERSION"
                or native_proof.get("transaction_finalized") is not True
                or native_proof.get("confirmed_native_conversion") is not True
                or native_proof.get("operation")!=(
                    "WITHDRAW" if side=="BUY" else "DEPOSIT")
                or type(native_proof.get("block_number")) is not int
                or native_proof["block_number"]<=0
                or type(native_proof.get("tx_hash")) is not str
                or len(native_proof["tx_hash"])!=66
                or native_proof["tx_hash"]==swap_proof["tx_hash"]):
            raise ValueError("Matching finalized native conversion required")
        # In the absence of verified intra-block transaction indices, these
        # operations must be in DIFFERENT BLOCKS in a valid ordering.
        if (side=="BUY" and not
                native_proof["block_number"]>swap_proof["block_number"]
                or side=="SELL" and not
                native_proof["block_number"]<swap_proof["block_number"]):
            raise ValueError("Native conversion and swap executed out of order")
        quantity=_amount(native_proof.get("amount_native"))
        if quantity!=(destination if side=="BUY" else source):
            raise ValueError("Native amount does not match wrapped swap quantity")
        native_hash=native_proof["tx_hash"]
        native_verified=True
    else:
        if native_proof is not None:
            raise ValueError("ERC20 asset must not consume a native conversion")
        native_hash=None
        native_verified=False
    return {
        "mode":"READ_ONLY_FINALIZED_EVM_TRADE_CYCLE_PROOF",
        "asset":asset,"side":side,"stable":stable,
        "network":pair["network"],"chain_id":pair["chain_id"],
        "wallet_address":wallet.lower(),
        "swap_tx_hash":swap_proof["tx_hash"],
        "native_conversion_tx_hash":native_hash,
        "swap_source_spent_amount":format(source,"f"),
        "swap_destination_received_amount":format(destination,"f"),
        "native_conversion_verified":native_verified,
        "required_transaction_legs_complete":True,
        "inventory_or_cost_basis_settled":False,
        "realized_net_profit_verified":False,
        "five_percent_fee_assessed":False,
        "wallet_signed_by_this_verifier":False,
        "live_execution_enabled_by_this_verifier":False,
    }
