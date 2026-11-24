"""Read-only reconciliation of Ethereum-settled cross-chain swaps.

LI.FI locates the two transactions; the chain receipts establish ownership,
canonical finality and actual ERC20 net credit. Neither a provider DONE flag
nor a mined source transaction alone establishes a completed swap.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal, localcontext
import os
import re

import httpx
from fastapi import HTTPException

from .ethereum_settlement_quote import CROSS_EVM, _token_matches
from .ethereum_settlement_journal import load_quote, load_submission
from .ethereum_settlement_plan import settlement_route, check_evm_quote_route
from .ethereum_settlement_calldata import route_digest, inspect_across_v4
from .ethereum_settlement_bridge_proof import prove_across_pair
from .live_eth_quotes import ETH_NATIVE, STABLES, _quantity, _valid_units
from .live_eth_receipts import _num
from .multicoin_evm_balances import _RPC, _WRAPPED_GROUPS
from .privy_wallet_verification import fetch_privy_user
from .shared_evm_swap_receipts import _receipt_rpc
from .wallet_balances import verified_addresses

# Official LI.FI deployment manifests (mainnet, polygon, bsc, avalanche,
# arbitrum): https://github.com/lifinance/contracts/tree/main/deployments
DIAMOND = "0x1231deb6f5749ef6ce6943a275a1d3e7486f4eae"
STATUS_URL = "https://li.quest/v1/status"
_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_WORD = re.compile(r"^0x[0-9a-fA-F]{64}$")
_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _eq(a, b):
    return isinstance(a, str) and isinstance(b, str) and a.lower() == b.lower()


def _rpc_url(chain_id):
    entry = next((row for row in _RPC.values() if row[0] == chain_id), None)
    if entry is None:
        raise ValueError("Unknown settlement chain")
    _, _, fallback, env = entry
    url = os.getenv(env, "").strip() or fallback
    if not url.startswith("https://") or any(ch in url for ch in " \n\t#@"):
        raise ValueError("HTTPS settlement RPC required")
    return url


def _net_token_credit(logs, token, wallet):
    if type(logs) is not list or len(logs) > 400:
        raise ValueError("Invalid settlement logs")
    topic = "0x" + wallet[2:].lower().rjust(64, "0")
    total = 0
    for event in logs:
        if type(event) is not dict:
            raise ValueError("Invalid settlement log")
        if not _eq(event.get("address"), token):
            continue
        topics = event.get("topics")
        if not isinstance(topics, list) or not topics or not _eq(topics[0], _TRANSFER):
            continue
        if (event.get("removed") is True or len(topics) != 3
                or any(not isinstance(t, str) or not _WORD.fullmatch(t)
                       for t in topics)
                or any(t[2:26] != "0" * 24 for t in topics[1:])
                or not isinstance(event.get("data"), str)
                or not _WORD.fullmatch(event["data"])):
            raise ValueError("Invalid token transfer")
        value = int(event["data"], 16)
        if topics[2].lower() == topic:
            total += value
        if topics[1].lower() == topic:
            total -= value
    return total


def _net_native_credit(trace, wallet):
    """Sum real successful value transfers; exclude delegatecall and reverts.

    callTracer reports nested execution. A reverted parent reverts every
    descendant. A DELEGATECALL inherits msg.value but transfers no balance.
    Never infer native payout from an ERC20 event or a provider status.
    """
    total = 0
    stack = [trace]
    count = 0
    while stack:
        frame = stack.pop()
        count += 1
        if count > 2000 or type(frame) is not dict:
            raise ValueError("Invalid native settlement trace")
        if frame.get("error"):
            continue
        kind = frame.get("type")
        if kind not in ("CALL", "DELEGATECALL", "STATICCALL", "CALLCODE",
                        "CREATE", "CREATE2", "SELFDESTRUCT"):
            raise ValueError("Unknown native trace frame")
        children = frame.get("calls", [])
        if type(children) is not list:
            raise ValueError("Invalid native trace children")
        stack.extend(children)
        if kind not in ("CALL", "CREATE", "CREATE2", "SELFDESTRUCT"):
            continue
        sender, recipient = frame.get("from"), frame.get("to")
        if (not isinstance(sender, str) or not _ADDR.fullmatch(sender)
                or not isinstance(recipient, str) or not _ADDR.fullmatch(recipient)):
            raise ValueError("Invalid native transfer address")
        raw = frame.get("value", "0x0")
        if not isinstance(raw, str) or not re.fullmatch(r"0x[0-9a-fA-F]{1,64}", raw):
            raise ValueError("Invalid native transfer value")
        value = int(raw, 16)
        if _eq(recipient, wallet):
            total += value
        if _eq(sender, wallet):
            total -= value
    return total


async def _native_credit(client, chain, tx_hash, tx, wallet):
    """Read only from the same verified chain RPC as the final receipt."""
    try:
        trace = await _receipt_rpc(
            client, _rpc_url(chain), "debug_traceTransaction",
            [tx_hash, {"tracer": "callTracer", "timeout": "5s"}], 6)
    except (httpx.HTTPError, ValueError):
        # Not all public RPC services expose callTracer. Remain pending
        # instead of substituting an unverified provider payout.
        return None
    if (not isinstance(trace, dict) or trace.get("type") != "CALL"
            or trace.get("error") or not _eq(trace.get("from"), tx.get("from"))
            or not _eq(trace.get("to"), tx.get("to"))
            or _num(trace.get("value", "0x0")) != _num(tx.get("value", "0x0"))):
        raise ValueError("Native trace not bound to destination transaction")
    return _net_native_credit(trace, wallet)


async def _finalized_receipt(client, chain, tx_hash):
    url = _rpc_url(chain)
    chain_id, tx, receipt, final = await asyncio.gather(
        _receipt_rpc(client, url, "eth_chainId", [], 1),
        _receipt_rpc(client, url, "eth_getTransactionByHash", [tx_hash], 2),
        _receipt_rpc(client, url, "eth_getTransactionReceipt", [tx_hash], 3),
        _receipt_rpc(client, url, "eth_getBlockByNumber", ["finalized", False], 4),
    )
    if _num(chain_id) != chain:
        raise ValueError("Unexpected settlement chain")
    if not receipt or not tx or not final:
        return None
    block = _num(receipt.get("blockNumber"))
    if (not _eq(tx.get("hash"), tx_hash)
            or not _eq(receipt.get("transactionHash"), tx_hash)
            or not _eq(tx.get("blockHash"), receipt.get("blockHash"))
            or _num(tx.get("blockNumber")) != block):
        raise ValueError("Settlement transaction mismatch")
    if _num(final.get("number")) < block:
        return None
    canonical = await _receipt_rpc(
        client, url, "eth_getBlockByNumber", [hex(block), False], 5)
    if not canonical or not _eq(canonical.get("hash"), receipt.get("blockHash")):
        raise ValueError("Settlement receipt is not canonical")
    return tx, receipt


def _status_terms(payload, *, wallet, tx_hash, source_chain, dest_chain,
                  source_tokens, dest_token, source_decimals, dest_decimals,
                  units):
    if not isinstance(payload, dict):
        raise ValueError("Malformed bridge status")
    if payload.get("status") != "DONE":
        return None
    if (not _eq(payload.get("fromAddress"), wallet)
            or not _eq(payload.get("toAddress"), wallet)):
        raise ValueError("Bridge status belongs to a different wallet")
    if payload.get("substatus") in ("PARTIAL", "REFUNDED"):
        return {"state": "REFUND_OR_PARTIAL"}
    if payload.get("substatus") != "COMPLETED":
        return None
    sending, receiving = payload["sending"], payload["receiving"]
    if (not _eq(sending.get("txHash"), tx_hash)
            or sending.get("chainId") != source_chain
            or receiving.get("chainId") != dest_chain
            or sending.get("amount") != units
            or not any(_token_matches(sending.get("token"), source_chain, t,
                                      source_decimals) for t in source_tokens)
            or not _token_matches(receiving.get("token"), dest_chain,
                                  dest_token, dest_decimals)):
        raise ValueError("Bridge status differs from original route")
    destination_hash = receiving.get("txHash")
    if not isinstance(destination_hash, str) or not _HASH.fullmatch(destination_hash):
        raise ValueError("Missing destination transaction")
    return {"state": "PROVIDER_COMPLETED", "hash": destination_hash.lower(),
            "amount": _valid_units(receiving["amount"]),
            "source_token": sending["token"]["address"]}


async def check_ethereum_settlement(did, *, asset, side, stable, amount, tx_hash, quote_id):
    if (asset not in CROSS_EVM or side not in ("BUY", "SELL")
            or stable not in STABLES or not isinstance(tx_hash, str)
            or not _HASH.fullmatch(tx_hash)):
        raise HTTPException(422, "Invalid cross-chain settlement request")
    buy = side == "BUY"
    chain, coin_token = CROSS_EVM[asset]
    route=settlement_route(asset,side,stable)
    src,dst=route["source_chain_id"],route["destination_chain_id"]
    source_decimals,dest_decimals=route["source_decimals"],route["destination_decimals"]
    with localcontext() as context:
        context.prec=80
        _, units = _quantity(amount, source_decimals)
    source_tokens = [STABLES[stable]] if buy else [coin_token]
    if not buy and asset in _WRAPPED_GROUPS:
        source_tokens.append(_WRAPPED_GROUPS[asset][1])
    dest_token = coin_token if buy else STABLES[stable]
    quote = await asyncio.to_thread(load_quote, did, quote_id)
    try:
        check_evm_quote_route(quote)
    except ValueError:
        raise HTTPException(409,"Original quote is not Ethereum-settled") from None
    if (quote["asset"] != asset or quote["side"] != side or quote["stable"] != stable
            or quote["source_chain_id"] != src or quote["destination_chain_id"] != dst
            or quote["input_atomic"] != units
            or not any(_eq(quote["source_token"], token) for token in source_tokens)
            or not _eq(quote["destination_token"], dest_token)):
        raise HTTPException(409, "Settlement differs from original quote")
    source_tokens = [quote["source_token"]]
    submission=await asyncio.to_thread(load_submission,did,quote_id)
    if submission is not None and (submission["source_chain_id"]!=src
            or (submission["source_tx_hash"] is not None
                and not _eq(submission["source_tx_hash"],tx_hash))):
        raise HTTPException(409,"Transaction differs from original submitted order")
    bound=bool(submission and submission["state"]=="SUBMITTED"
               and _eq(submission["source_tx_hash"],tx_hash))
    # Manual hash is a candidate supplied by the owner, not trusted signer
    # evidence. It becomes valid only after the independent on-chain proof.
    from .ethereum_settlement_manual import load_manual_attempt, mark_verified_manual_outcome
    manual=await asyncio.to_thread(load_manual_attempt,did,quote_id)
    manual_bound=bool(manual and manual["state"]=="SUBMITTED"
        and manual["source_chain_id"]==src and _eq(manual["source_tx_hash"],tx_hash))
    if manual and manual["source_tx_hash"] and not manual_bound:
        raise HTTPException(409,"Manual source transaction differs")
    base = {"mode": "ETHEREUM_CROSS_CHAIN_SETTLEMENT", "asset": asset,
            "quote_id": quote_id, "side": side, "stable": stable, "source_chain_id": src,
            "destination_chain_id": dst, "source_tx_hash": tx_hash.lower(),
            "destination_tx_hash": None, "received_amount": None,
            "source_fee_native": None, "state": "PENDING",
            "original_submission_bound": bound,
            "manual_submission_candidate":manual_bound,
            "bridge_message_verified":False,
            "settlement_verified": False, "execution_enabled": False}
    try:
        user = await fetch_privy_user(did)
        wallet = verified_addresses(user, did)["ETH"]
        if (not isinstance(wallet, str) or not _ADDR.fullmatch(wallet)
                or not _eq(wallet, quote["wallet_address"])):
            raise ValueError("Owned wallet unavailable")
        headers = {"Accept": "application/json"}
        key = os.getenv("MININEX_LIFI_API_KEY", "").strip()
        if key:
            headers["x-lifi-api-key"] = key
        async with httpx.AsyncClient(timeout=httpx.Timeout(9),
                                     follow_redirects=False) as client:
            source = await _finalized_receipt(client, src, tx_hash)
            if source is None:
                return base
            tx, receipt = source
            if not _eq(tx.get("from"), wallet) or not _eq(tx.get("to"), DIAMOND):
                raise ValueError("Not an owned LI.FI source transaction")
            if quote.get("route_sha256") is not None:
                if route_digest({**tx,"chainId":src},src)!=quote["route_sha256"]:
                    raise ValueError("Mined calldata differs from original quote")
            elif bound:
                raise ValueError("Submitted route has no inspected calldata")
            gas = _num(receipt["gasUsed"]) * _num(receipt["effectiveGasPrice"])
            base["source_fee_native"] = format(Decimal(gas).scaleb(-18), "f")
            if _num(receipt["status"]) == 0:
                if manual_bound:
                    await asyncio.to_thread(mark_verified_manual_outcome,did,quote_id,tx_hash,"SOURCE_REVERTED")
                return {**base, "state": "SOURCE_REVERTED"}
            if _num(receipt["status"]) != 1:
                raise ValueError("Invalid source receipt status")
            response = await client.get(STATUS_URL, headers=headers, params={
                "txHash": tx_hash, "fromChain": src, "toChain": dst})
            response.raise_for_status()
            if len(response.content) > 512_000:
                raise ValueError("Oversized bridge status")
            terms = _status_terms(
                response.json(), wallet=wallet, tx_hash=tx_hash,
                source_chain=src, dest_chain=dst, source_tokens=source_tokens,
                dest_token=dest_token, source_decimals=source_decimals,
                dest_decimals=dest_decimals, units=units)
            if terms is None:
                return base
            if terms["state"] == "REFUND_OR_PARTIAL":
                return {**base, "state": "REFUND_OR_PARTIAL"}
            source_token = terms["source_token"]
            if _eq(source_token, ETH_NATIVE):
                if _num(tx.get("value")) < int(units):
                    raise ValueError("Native source amount not funded")
                debit=await _native_credit(client,src,tx_hash,tx,wallet)
                if debit is None:
                    return {**base,"state":"NATIVE_DEBIT_PROOF_REQUIRED"}
                if -debit!=int(units):
                    raise ValueError("Native source debit differs from original amount")
            elif -_net_token_credit(receipt["logs"], source_token, wallet) != int(units):
                raise ValueError("Source token debit differs from original amount")
            base["destination_tx_hash"] = terms["hash"]
            destination = await _finalized_receipt(client, dst, terms["hash"])
            if destination is None:
                return base
            dest_tx, dest_receipt = destination
            if _num(dest_receipt["status"]) != 1:
                raise ValueError("Destination transaction failed")
            if _eq(dest_token, ETH_NATIVE):
                received = await _native_credit(client, dst, terms["hash"], dest_tx, wallet)
                if received is None:
                    return {**base, "state": "NATIVE_CREDIT_PROOF_REQUIRED"}
            else:
                received = _net_token_credit(dest_receipt["logs"], dest_token, wallet)
            if received <= 0 or received != terms["amount"]:
                raise ValueError("Destination token credit not independently proven")
            if received < int(quote["minimum_output_atomic"]):
                return {**base, "state": "BELOW_QUOTED_MINIMUM",
                        "received_amount": format(Decimal(received).scaleb(-dest_decimals), "f")}
            proof=None
            if quote.get("unsigned_transaction") is not None:
                inspected=inspect_across_v4(quote["unsigned_transaction"],
                    terms=quote,now=quote["issued_at"])
                proof=prove_across_pair(receipt["logs"],dest_receipt["logs"],
                    src=src,dst=dst,wallet=wallet,route=inspected,received=received)
            # Only the original trusted submission plus matched bridge events
            # and verified wallet credit establish settlement. Accounting is
            # intentionally separate; this read-only endpoint never books PnL.
            verified=bool((bound or manual_bound) and proof)
            if manual_bound and verified:
                await asyncio.to_thread(mark_verified_manual_outcome,did,quote_id,tx_hash,"FINALIZED")
            with localcontext() as context:
                context.prec=80
                actual=format(Decimal(received).scaleb(-dest_decimals),"f")
            return {**base, "state":"FINALIZED" if verified else "CREDIT_OBSERVED",
                    "bridge_message_verified":bool(proof),
                    "settlement_verified":verified,"received_amount":actual,
                    "bridge_deposit_id":proof["deposit_id"] if proof else None}
    except (httpx.HTTPError, ValueError, TypeError, KeyError, ArithmeticError):
        raise HTTPException(502, "Cross-chain settlement not independently verified") from None
