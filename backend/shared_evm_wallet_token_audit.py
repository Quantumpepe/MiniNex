"""On-chain ERC20 wallet-inventory audit for LINK and UNI FIFO closures.

Read-only scan of *all* Transfer logs affecting the authenticated wallet in
the original trade interval. A historically zero initial balance and complete
matching of every inbound/outbound token transfer to a verified MiniNex order
are needed before crediting the lot inventory. This is not sufficient by
itself to prove gas, native ETH transfers, all historical USD costs or HWM.

Native BNB/POL/AVAX cannot use this ERC20-only audit as proof of their
underlying native wallet flows.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import os
import re

import httpx
from fastapi import HTTPException

from .multicoin_evm_balances import _RPC
from .shared_evm_swap_receipts import _pair, _receipt_rpc

_HASH = re.compile(r"^0x[0-9a-f]{64}$")
_ADDR = re.compile(r"^0x[0-9a-f]{40}$")
_TRANSFER_TOPIC = ("0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")
_ASSETS = frozenset(("LINK", "UNI"))
_WINDOW_BLOCKS = 1500  # RPC query window, NOT a trade/day/capital limit


def _quantity_wei(amount: object) -> int:
    if type(amount) is not str or len(amount) > 80 or "e" in amount.lower():
        raise ValueError("Exact positive native token quantity required")
    try:
        raw = Decimal(amount)
        atomic = raw * Decimal(10)**18
    except (InvalidOperation, ArithmeticError):
        raise ValueError("Invalid EVM token quantity") from None
    if (not raw.is_finite() or raw <= 0 or raw >= Decimal("1e22")
            or atomic != atomic.to_integral_value()):
        raise ValueError("Unrepresentable onchain ERC20 quantity")
    return int(atomic)


def _parse_wallet_transfer(log: object, *,token: str, wallet: str,
                           direction: str, first: int, last: int) -> tuple:
    if type(log) is not dict or direction not in ("IN", "OUT"):
        raise ValueError("Untrusted token Transfer event")
    topics = log.get("topics")
    if (type(topics) is not list or len(topics) != 3
            or any(type(t) is not str or not _HASH.fullmatch(t.lower())
                   for t in topics)
            or topics[0].lower() != _TRANSFER_TOPIC
            or type(log.get("address")) is not str
            or log["address"].lower() != token
            or log.get("removed") is not False
            or type(log.get("transactionHash")) is not str
            or not _HASH.fullmatch(log["transactionHash"].lower())
            or type(log.get("blockHash")) is not str
            or not _HASH.fullmatch(log["blockHash"].lower())
            or type(log.get("data")) is not str
            or not _HASH.fullmatch(log["data"].lower())):
        raise ValueError("Invalid original ERC20 event source")
    try:
        height = int(log["blockNumber"],16)
        index = int(log["logIndex"],16)
        tx_index = int(log["transactionIndex"],16)
        units = int(log["data"],16)
    except (ValueError, KeyError, TypeError):
        raise ValueError("Invalid ERC20 event block or atomic units") from None
    sender = "0x" + topics[1][-40:].lower()
    receiver = "0x" + topics[2][-40:].lower()
    if (not first <= height <= last or index < 0 or tx_index < 0
            or units <= 0 or units >= 10**45
            or sender == receiver
            or (direction == "IN" and receiver != wallet)
            or (direction == "OUT" and sender != wallet)):
        raise ValueError("External or self token event cannot be ignored")
    return log["transactionHash"].lower(), index, direction, units


def validate_owner_token_transfer_audit(
    *,asset: str, wallet: str, fills: object, start_block: int, end_block: int,
    pre_balance_wei: int, ending_balance_wei: int,
    incoming: object, outgoing: object,
) -> dict:
    """Reject even one unexplained transfer or an unknown beginning balance."""
    if (asset not in _ASSETS or type(wallet) is not str
            or not _ADDR.fullmatch(wallet)
            or type(fills) is not list or not fills
            or type(incoming) is not list or type(outgoing) is not list
            or type(start_block) is not int or type(end_block) is not int
            or not 1 < start_block <= end_block
            or type(pre_balance_wei) is not int or pre_balance_wei != 0
            or type(ending_balance_wei) is not int or ending_balance_wei < 0):
        raise ValueError("Incomplete actual wallet inventory provenance")
    token = _pair(asset, "USDC", "BUY")["output_token"].lower()
    expected = {}
    running = 0
    for fill in fills:
        if (type(fill) is not dict
                or fill.get("mode") != "INDEPENDENT_FINALIZED_EVM_EXECUTION"
                or fill.get("onchain_finalized") is not True
                or fill.get("asset") != asset
                or fill.get("wallet_address") != wallet
                or type(fill.get("block_number")) is not int
                or not start_block <= fill["block_number"] <= end_block
                or type(fill.get("tx_hash")) is not str
                or not _HASH.fullmatch(fill["tx_hash"])):
            raise ValueError("Unproven persisted owner trade")
        side = fill.get("side")
        if side not in ("BUY", "SELL"):
            raise ValueError("Unrecognized ERC20 asset change")
        direction = "IN" if side == "BUY" else "OUT"
        amount = _quantity_wei(
            fill.get("destination_amount") if side == "BUY"
            else fill.get("source_amount"))
        ident = (fill["tx_hash"], direction, amount)
        if ident in expected:
            raise ValueError("Duplicate original trade transfer identity")
        expected[ident] = True
        running += amount if direction == "IN" else -amount
        if running < 0:
            raise ValueError("Nonexistent owner inventory cannot be sold")
    if running != ending_balance_wei:
        raise ValueError("Owner token balances differ from complete fills")
    found = set()
    log_positions = set()
    for direction, logs in (("IN", incoming), ("OUT", outgoing)):
        for log in logs:
            tx, idx, actual_dir, amount = _parse_wallet_transfer(
                log, token=token, wallet=wallet,
                direction=direction, first=start_block, last=end_block)
            position = (tx, idx)
            if position in log_positions:
                raise ValueError("Repeated or self-directed ERC20 event")
            log_positions.add(position)
            ident = (tx,actual_dir,amount)
            if ident not in expected or ident in found:
                raise ValueError("Unaccounted external token transfer")
            found.add(ident)
    if set(expected) != found:
        raise ValueError("Real onchain token movement missing from RPC log audit")
    return {
        "mode": "INDEPENDENT_ONCHAIN_ASSET_TRANSFER_AUDIT",
        "asset": asset, "wallet_address": wallet,
        "start_block": start_block, "end_block": end_block,
        "zero_initial_asset_balance_verified": True,
        "all_asset_movements_match_verified_orders": True,
        "external_inventory_reconciled": True,
        "no_other_execution_costs_verified": False,
        "matched_mininex_swap_transfers": len(found),
        "hwm_updated": False, "fee_assessed": False,
    }


async def audit_owner_erc20_token_inventory(
    *,asset: str,wallet: str,fills: list[dict],client: httpx.AsyncClient,
) -> dict:
    """Provider-bounded eth_getLogs pages and archived onchain balanceOf."""
    if (asset not in _ASSETS or type(wallet) is not str
            or not _ADDR.fullmatch(wallet)
            or type(client) is not httpx.AsyncClient
            or type(fills) is not list or not fills):
        raise ValueError("Unsupported ERC20 wallet audit input")
    positions = [f.get("block_number") if type(f) is dict else None for f in fills]
    if any(type(p) is not int or p <= 1 for p in positions):
        raise ValueError("Cannot identify original wallet history period")
    start,end = min(positions),max(positions)
    chain,network,fallback,env = _RPC["ETH_TOKENS"]
    route = _pair(asset,"USDC","BUY")
    if chain != route["chain_id"] or network != route["network"]:
        raise ValueError("Configured token audit network differs")
    url = os.getenv(env,"").strip() or fallback
    if not url.startswith("https://") or any(c in url for c in " \r\n\t#@"):
        raise HTTPException(503,"Historical EVM ERC20 RPC unavailable")
    token = route["output_token"].lower()
    word = f"{int(wallet,16):064x}"
    balance_data = "0x70a08231" + word
    chain_id = await _receipt_rpc(client,url,"eth_chainId",[],71)
    if int(chain_id,16) != chain:
        raise ValueError("Wrong source chain for owner token inventory")
    finalized = await _receipt_rpc(
        client,url,"eth_getBlockByNumber",["finalized",False],72)
    if type(finalized) is not dict or int(finalized["number"],16) < end:
        raise ValueError("EVM transfers not yet finalized")
    incoming = []
    outgoing = []
    req_id = 80
    for low in range(start,end+1,_WINDOW_BLOCKS):
        high = min(end,low+_WINDOW_BLOCKS-1)
        for direction,filter_topics in (
            ("IN",[_TRANSFER_TOPIC,None,"0x"+word]),
            ("OUT",[_TRANSFER_TOPIC,"0x"+word,None]),
        ):
            logs = await _receipt_rpc(
                client,url,"eth_getLogs",[{
                    "address":token,"fromBlock":hex(low),"toBlock":hex(high),
                    "topics":filter_topics,
                }],req_id)
            req_id += 1
            if type(logs) is not list:
                raise ValueError("Incomplete owner transfer log page")
            (incoming if direction == "IN" else outgoing).extend(logs)
    pre = await _receipt_rpc(
        client,url,"eth_call",[{"to":token,"data":balance_data},hex(start-1)],req_id)
    post = await _receipt_rpc(
        client,url,"eth_call",[{"to":token,"data":balance_data},hex(end)],req_id+1)
    if (type(pre) is not str or type(post) is not str
            or not pre.startswith("0x") or not post.startswith("0x")):
        raise ValueError("Archived owner token balances unavailable")
    return validate_owner_token_transfer_audit(
        asset=asset,wallet=wallet,fills=fills,
        start_block=start,end_block=end,
        pre_balance_wei=int(pre,16),ending_balance_wei=int(post,16),
        incoming=incoming,outgoing=outgoing)
