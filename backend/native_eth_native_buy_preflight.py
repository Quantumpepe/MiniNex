"""Ethereum-mainnet ERC20 source preflight for BTC/SOL/XRP THORChain BUY.

The backend can independently prove current ETH chain, token allowance to the
*active quoted* THORChain router, owner token/ETH balances, pending nonce, gas
estimate and network gas price. No generic allowance to another contract is
accepted. Gas/nonce/allowance checks do NOT authorize wallet signing or send.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time

import httpx
from eth_abi import decode
from fastapi import HTTPException

from .eth_router02_live_quote import _rpc
from .live_eth_quotes import STABLES
from .native_eth_thor_deposit_tx import encode_original_eth_native_buy_deposit, _SELECTOR
from .native_thor_swap_quote import get_private_native_thornode_terms
from .storage import _safe_did

_QUANTITY = re.compile(r"^0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)$")
_WORD = re.compile(r"^0x[0-9a-fA-F]{64}$")
_WALLET = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _int_quantity(value: object) -> int:
    if (type(value) is not str or len(value) > 70
            or not _QUANTITY.fullmatch(value)):
        raise ValueError("Invalid current Ethereum JSON-RPC quantity")
    result = int(value, 16)
    if result >= 2**256:
        raise ValueError("Ethereum RPC uint256 overflow")
    return result


def _allowance_data(owner: object, spender: object) -> str:
    if (type(owner) is not str or not _WALLET.fullmatch(owner)
            or type(spender) is not str or not _WALLET.fullmatch(spender)):
        raise ValueError("Original owner or THORChain router invalid")
    return "0xdd62ed3e" + owner[2:].lower().rjust(64, "0") + (
        spender[2:].lower().rjust(64, "0"))


def inspect_original_native_buy_evm_preflight(
    *, unsigned: object, original_quote: object, now: int, chain_id: object,
    token_allowance: object, eth_balance: object,
    token_balance: object, pending_nonce: object,
    estimate_gas: object, gas_price: object,
) -> dict:
    """Verify read-only RPC answers for the exact server-compiled BUY."""
    if (type(unsigned) is not dict
            or unsigned.get("mode") != "THORCHAIN_ETH_ORIGINAL_BUY_UNSIGNED_PREVIEW"
            or unsigned.get("asset") not in ("BTC", "SOL", "XRP")
            or unsigned.get("stable") not in STABLES
            or unsigned.get("source_wallet_fee_balance_verified") is not False
            or unsigned.get("token_allowance_or_permit_verified") is not False
            or unsigned.get("remote_privy_owner_grant_verified") is not False
            or unsigned.get("gas_and_nonce_current") is not False
            or unsigned.get("ready_to_sign") is not False
            or unsigned.get("execution_enabled") is not False
            or unsigned.get("can_submit") is not False
            or unsigned.get("wallet_signed") is not False
            or type(now) is not int or now <= 0):
        raise ValueError("Untrusted native Ethereum BUY draft")
    if (type(original_quote) is not dict
            or original_quote.get("mode") != "THORCHAIN_DESTINATION_BOUND_READ_ONLY"
            or original_quote.get("source_asset") != unsigned["stable"]
            or original_quote.get("destination_asset") != unsigned["asset"]
            or original_quote.get("from_network") != "ethereum-mainnet"
            or original_quote.get("to_network") != unsigned.get("target_network")
            or original_quote.get("source_wallet") != unsigned.get("source_wallet")
            or original_quote.get("destination_wallet") != unsigned.get("destination_wallet")
            or original_quote.get("inbound_vault") != unsigned.get("vault")
            or original_quote.get("inbound_router") != unsigned.get("router")
            or original_quote.get("original_vault_verified") is not True
            or original_quote.get("refund_path_verified") is not True
            or original_quote.get("required_chain_memo_verified") is not True
            or original_quote.get("exact_venue_fees_verified") is not True):
        raise ValueError("Original native BUY quote owner or router changed")
    tx = unsigned.get("unsigned_transaction")
    if (type(tx) is not dict or set(tx) != {
            "from", "to", "chainId", "value", "data"}
            or tx.get("from") != unsigned.get("source_wallet")
            or tx.get("to") != unsigned.get("router")
            or tx.get("chainId") != 1
            or tx.get("value") != "0x0"
            or type(tx.get("data")) is not str
            or not tx["data"].startswith("0x")):
        raise ValueError("Original THORChain router source transaction changed")
    if (type(unsigned.get("source_amount_atomic")) is not str
            or not unsigned["source_amount_atomic"].isascii()
            or not unsigned["source_amount_atomic"].isdecimal()
            or type(unsigned.get("router_expires_at")) is not int
            or unsigned["router_expires_at"] <= now + 60):
        raise ValueError("Original router source deadline invalid")
    # Decode the actual signed-call payload, never trust a caller-provided
    # label, router or token/recipient outside the original ERC20 ABI.
    data = tx["data"]
    if (not data.startswith("0x" + _SELECTOR.hex())
            or len(data) > 2000 or len(data) % 2):
        raise ValueError("Wrong THORChain ERC20 depositWithExpiry selector")
    try:
        vault, token, atomic, memo, router_expiry = decode(
            ["address", "address", "uint256", "string", "uint256"],
            bytes.fromhex(data[10:]),
        )
    except (ValueError, TypeError, OverflowError) as error:
        raise ValueError("Original Ethereum THORChain ABI cannot be decoded") from error
    memo_parts = memo.split(":")
    destination_and_refund = memo_parts[2].split("/") if len(memo_parts) >= 3 else []
    if (vault.lower() != unsigned.get("vault", "").lower()
            or token.lower() != STABLES[unsigned["stable"]].lower()
            or atomic != int(unsigned["source_amount_atomic"])
            or router_expiry != unsigned["router_expires_at"]
            or len(destination_and_refund) != 2
            or destination_and_refund[0] != unsigned["destination_wallet"]
            or destination_and_refund[1].lower() != unsigned["source_wallet"].lower()):
        raise ValueError("Original native BUY token/vault/amount/refund changed")
    original_expiry = unsigned.get("original_quote_expires_at")
    if type(original_expiry) is not str:
        raise ValueError("Original BUY quote deadline unavailable")
    from datetime import datetime
    try:
        expiry = datetime.fromisoformat(original_expiry)
    except ValueError:
        raise ValueError("Original Ethereum BUY quote expiry malformed") from None
    if (expiry.tzinfo is None or not now < expiry.timestamp()
            or original_quote.get("quote_expires_at") != expiry
            or original_quote.get("amount") is None
            or type(original_quote.get("amount")) is not str
            or memo != original_quote.get("memo")):
        raise ValueError("Original THORChain quote expired during RPC readback")
    # Bind the unsigned source deposit to the separately authenticated
    # private quote. A forged router and matching manipulated unsigned
    # metadata are insufficient without the original immutable quote digest.
    quote_amount = original_quote["amount"]
    if (not re.fullmatch(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$", quote_amount)
            or len(quote_amount) > 55):
        raise ValueError("Original native BUY exact amount malformed")
    from decimal import Decimal, InvalidOperation, localcontext
    try:
        with localcontext() as ctx:
            ctx.prec = 100
            amount_units = Decimal(quote_amount) * Decimal(10**6)
    except (InvalidOperation, ValueError):
        raise ValueError("Original native BUY quote amount unavailable") from None
    if (not amount_units.is_finite()
            or amount_units != amount_units.to_integral_value()
            or int(amount_units) != atomic):
        raise ValueError("Original native BUY source amount differs from quote")
    original_digest = hashlib.sha256(json.dumps(
        [unsigned["asset"], unsigned["stable"], quote_amount,
         unsigned["source_wallet"], unsigned["destination_wallet"],
         unsigned["vault"], unsigned["router"], memo,
         int(expiry.timestamp()), router_expiry, data],
        separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    if unsigned.get("unsigned_transaction_sha256") != original_digest:
        raise ValueError("Original native BUY quote-to-router fingerprint changed")
    if _int_quantity(chain_id) != 1:
        raise ValueError("Wrong source chain: ETH mainnet required")
    if type(token_allowance) is not str or not _WORD.fullmatch(token_allowance):
        raise ValueError("Actual router ERC20 allowance unavailable")
    allowance = int(token_allowance, 16)
    needed = int(unsigned["source_amount_atomic"])
    if needed <= 0 or allowance < needed:
        raise ValueError("Original owner ERC20 allowance missing or insufficient")
    eth_wei = _int_quantity(eth_balance)
    token_units = (int(token_balance, 16)
                   if type(token_balance) is str and _WORD.fullmatch(token_balance)
                   else -1)
    if token_units < needed:
        raise ValueError("Original source wallet stablecoin balance unavailable")
    nonce = _int_quantity(pending_nonce)
    gas = _int_quantity(estimate_gas)
    price = _int_quantity(gas_price)
    if not 21_000 <= gas <= 30_000_000 or price <= 0:
        raise ValueError("Current network source fee unavailable")
    gas_budget = gas * price
    if eth_wei < gas_budget:
        raise ValueError("Actual ETH fee balance does not cover transaction")
    tx_final = {**tx, "nonce": hex(nonce), "gas": hex(gas),
                "gasPrice": hex(price)}
    fingerprint = hashlib.sha256(json.dumps(
        [unsigned["unsigned_transaction_sha256"], tx_final,
         allowance, eth_wei, token_units, now],
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    return {
        "mode": "THORCHAIN_ETH_ORIGINAL_NATIVE_BUY_NETWORK_PREFLIGHT",
        "asset": unsigned["asset"], "side": "BUY",
        "stable": unsigned["stable"],
        "source_wallet": unsigned["source_wallet"],
        "destination_wallet": unsigned["destination_wallet"],
        "router": unsigned["router"],
        "vault": unsigned["vault"],
        "source_amount_atomic": unsigned["source_amount_atomic"],
        "source_allowance_atomic": str(allowance),
        "verified_stable_balance_atomic": str(token_units),
        "verified_eth_balance_wei": str(eth_wei),
        "estimated_network_fee_wei": str(gas_budget),
        "unsigned_transaction": tx_final,
        "unsigned_transaction_sha256": fingerprint,
        "network_checked_at_unix": now,
        "chain_id_verified": True,
        "original_router_allowance_verified": True,
        "source_funds_and_gas_verified": True,
        "pending_nonce_and_gas_estimate_verified": True,
        "independently_verified_privy_owner_grant": False,
        "durable_native_order_claim": False,
        "ready_to_sign": False,
        "wallet_signed": False,
        "source_transaction_sent": False,
        "execution_enabled": False, "can_submit": False,
    }


async def preflight_original_native_eth_buy(
    did: str, *, asset: str, stable: str, amount: str,
) -> dict:
    """Read original THORChain quote and source Ethereum RPC; never transact."""
    did = _safe_did(did)
    if asset not in ("BTC", "SOL", "XRP") or stable not in STABLES:
        raise HTTPException(422, "Native Ethereum BUY asset or stable unsupported")
    original = await get_private_native_thornode_terms(
        did, asset=asset, side="BUY", stable=stable, amount=amount,
    )
    return await _preflight_verified_original_eth_buy(
        did,asset=asset,stable=stable,amount=amount,original=original)


async def _preflight_verified_original_eth_buy(
    did:str,*,asset:str,stable:str,amount:str,original:dict,
)->dict:
    """Internal shared-quote source readback for the current strategy signal.

    Caller must obtain 'original' from get_private_native_thornode_terms
    under this verified DID. This helper neither delegates nor sends.
    """
    did=_safe_did(did)
    if (asset not in ("BTC","SOL","XRP") or stable not in STABLES
            or (asset=="XRP" and stable!="USDC")
            or type(original) is not dict):
        raise HTTPException(422,"Invalid original native source context")
    try:
        now = int(time.time())
        unsigned = encode_original_eth_native_buy_deposit(
            original, asset=asset, stable=stable, amount=amount, now=now)
        tx = unsigned["unsigned_transaction"]
        owner = tx["from"]
        router = tx["to"]
        token = STABLES[stable]
        owner_word = owner[2:].lower().rjust(64, "0")
        allowance = _allowance_data(owner, router)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(9), follow_redirects=False,
        ) as client:
            chain = await _rpc(client, method="eth_chainId", params=[], req_id=1)
            if _int_quantity(chain) != 1:
                raise ValueError("Wrong Ethereum chain for native BUY")
            # The quote is short-lived. Read independent account state
            # concurrently instead of waiting for six sequential public RPCs.
            # Pin all historical balance/allowance reads to the SAME block:
            # mixing "latest" across successive blocks can fabricate a
            # spendable allowance when the tokens have already moved.
            height = _int_quantity(await _rpc(
                client, method="eth_blockNumber", params=[], req_id=8))
            if height <= 0:
                raise ValueError("Ethereum source block unavailable")
            block_tag = hex(height)
            (balance, source, allowance_word, pending_nonce,
             gas, gas_price) = await asyncio.gather(
                _rpc(client, method="eth_getBalance",
                     params=[owner, block_tag], req_id=2),
                _rpc(client, method="eth_call",
                     params=[{"to": token, "data": "0x70a08231" + owner_word},
                             block_tag], req_id=3),
                _rpc(client, method="eth_call",
                     params=[{"to": token, "data": allowance}, block_tag],
                     req_id=4),
                _rpc(client, method="eth_getTransactionCount",
                     params=[owner, "pending"], req_id=5),
                _rpc(client, method="eth_estimateGas", params=[{
                    "from": owner, "to": router,
                    "data": tx["data"], "value": "0x0",
                }], req_id=6),
                _rpc(client, method="eth_gasPrice",
                     params=[], req_id=7),
            )
        return inspect_original_native_buy_evm_preflight(
            unsigned=unsigned, original_quote=original, now=int(time.time()),
            chain_id=chain, token_allowance=allowance_word,
            eth_balance=balance, token_balance=source,
            pending_nonce=pending_nonce, estimate_gas=gas,
            gas_price=gas_price,
        )
    except (ValueError, KeyError, TypeError, OverflowError,
            httpx.HTTPError):
        raise HTTPException(
            409, "Original ETH stablecoin BUY gas, allowance or quote unavailable",
        ) from None
