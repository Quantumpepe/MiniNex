"""One owner-scoped, read-only, finalized ERC-20 swap verifier for six EVM assets.

ETH, LINK, UNI use Ethereum; BNB, POL, AVAX use their independently
configured Uniswap V3 deployment and chain. Only actual Transfer deltas,
quoted calldata, transaction owner, matching block and canonical finalized
checkpoint prove a successful ERC-20 swap leg.

This does NOT prove native wrap/unwrap, create an order, debit a budget,
assess a 5% fee, move funds or grant a Privy signer.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
import os
import re

import httpx
from fastapi import HTTPException

from .eth_router02_receipts import _actual_token_flows
from .eth_swaprouter02_preflight import (
    SWAPROUTER02_MAINNET, WETH_MAINNET, EXACT_INPUT_SINGLE_SELECTOR,
)
from .live_eth_quotes import STABLES
from .live_eth_receipts import _num
from .multicoin_evm_balances import _ETH_TOKENS, _RPC
from .native_evm_v3_quote import CHAINS
from .privy_wallet_verification import fetch_privy_user
from .wallet_balances import verified_addresses
from .shared_evm_execution import chain_for_asset

_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_CALL = re.compile(r"^0x[0-9a-fA-F]+$")


def _pair(asset: str, stable: str, side: str) -> dict:
    chain, network = chain_for_asset(asset)
    if type(side) is not str or side not in ("BUY","SELL"):
        raise ValueError("Invalid EVM swap side")
    if asset in ("ETH", "LINK", "UNI"):
        if stable not in STABLES:
            raise ValueError("Invalid Ethereum settlement token")
        traded = (WETH_MAINNET if asset=="ETH" else _ETH_TOKENS[asset])
        route = SWAPROUTER02_MAINNET
        stable_address = STABLES[stable]
        stable_decimals = 6
        fee_levels=(500,3000)
    else:
        item=CHAINS[asset]
        if (stable!=item.stable or item.chain_id!=chain
                or item.network!=network):
            raise ValueError("Unsupported on-chain settlement")
        traded=item.wrapped
        route=item.router
        stable_address=item.stable_contract
        stable_decimals=item.stable_decimals
        fee_levels=(500,3000)
    return {
        "asset":asset,"network":network,"chain_id":chain,
        "router":route,"side":side,"stable":stable,
        "source_token":stable_address if side=="BUY" else traded,
        "output_token":traded if side=="BUY" else stable_address,
        "source_decimals":stable_decimals if side=="BUY" else 18,
        "output_decimals":18 if side=="BUY" else stable_decimals,
        "fee_levels":fee_levels,
    }


def inspect_finalized_evm_swap(
    tx_hash: str, *, asset: str, stable: str, side: str, wallet: str,
    chain_id: object, transaction: object, receipt: object,
    latest: object, finalized: object,
) -> dict:
    """Verify actual owner input/output token transfers across any EVM chain.

    'Included' does not mean finalized, and a successful EVM receipt without
    matching wallet token deltas is never credited as a confirmed swap.
    """
    if (type(tx_hash) is not str or not _HASH.fullmatch(tx_hash)
            or type(wallet) is not str or not _ADDR.fullmatch(wallet)):
        raise ValueError("Bad EVM wallet or transaction hash")
    pair=_pair(asset,stable,side)
    if _num(chain_id)!=pair["chain_id"]:
        raise ValueError("RPC responded from another network")
    head=_num(latest)
    result={
        "mode":"SHARED_EVM_FINALIZED_SWAP_VERIFICATION",
        "asset":asset,"network":pair["network"],"chain_id":pair["chain_id"],
        "tx_hash":tx_hash.lower(),"side":side,"stable":stable,
        "transaction_seen":False,"transaction_finalized":False,
        "swap_erc20_leg_verified":False,
        "native_coin_cycle_complete":False,
        "source_spent_amount":None,"destination_received_amount":None,
        "network_fee_native":None,"state":"NOT_FOUND",
        "confirmations":0,"real_order_recorded":False,
        "high_water_mark_updated":False,"performance_fee_assessed":False,
        "execution_enabled":False,"can_submit":False,
    }
    if transaction is None:
        if receipt is not None:
            raise ValueError("Receipt has no matching transaction")
        return result
    if (type(transaction) is not dict
            or str(transaction.get("hash","")).lower()!=tx_hash.lower()
            or str(transaction.get("from","")).lower()!=wallet.lower()
            or str(transaction.get("to","")).lower()!=pair["router"].lower()
            or _num(transaction.get("chainId"))!=pair["chain_id"]
            or _num(transaction.get("value"))!=0):
        raise ValueError("EVM swap sender, chain, router or native value mismatch")
    raw=transaction.get("input")
    if (type(raw) is not str or not _CALL.fullmatch(raw)
            or len(raw)!=2+(4+7*32)*2
            or not raw.lower().startswith(EXACT_INPUT_SINGLE_SELECTOR)):
        raise ValueError("Not a single-hop V3 swap")
    words=[int(raw[10+i*64:10+(i+1)*64],16) for i in range(7)]
    ti,to,fee,recipient,quantity,minimum,limit=words
    if (ti!=int(pair["source_token"],16)
            or to!=int(pair["output_token"],16)
            or recipient!=int(wallet,16)
            or fee not in pair["fee_levels"]
            or limit!=0 or not 0<quantity<10**45
            or not 0<minimum<10**45):
        raise ValueError("Transaction calldata does not bind verified route")
    result["transaction_seen"]=True
    result["state"]="PENDING"
    if receipt is None:
        if transaction.get("blockHash") not in (None,"0x") or transaction.get("blockNumber") is not None:
            raise ValueError("Inconsistent pending transaction")
        return result
    if (type(receipt) is not dict
            or str(receipt.get("transactionHash","")).lower()!=tx_hash.lower()
            or str(receipt.get("from","")).lower()!=wallet.lower()
            or str(receipt.get("to","")).lower()!=pair["router"].lower()):
        raise ValueError("Receipt not bound to sender and router")
    bh=receipt.get("blockHash")
    if type(bh) is not str or not _HASH.fullmatch(bh):
        raise ValueError("Invalid receipt block hash")
    block=_num(receipt.get("blockNumber"))
    if (not 0<block<=head
            or bh.lower()!=str(transaction.get("blockHash","")).lower()
            or block!=_num(transaction.get("blockNumber"))):
        raise ValueError("Swap and receipt have different blocks")
    success=_num(receipt.get("status"))
    gas=_num(receipt.get("gasUsed"))
    price=_num(receipt.get("effectiveGasPrice"))
    if success not in (0,1) or not 0<gas<=30_000_000 or not 0<price<=10**12:
        raise ValueError("Invalid EVM success or network gas")
    result["confirmations"]=min(head-block+1,1_000_000)
    result["network_fee_native"]=format(Decimal(gas*price)/Decimal(10**18),"f")
    result["state"]="INCLUDED_SUCCESS" if success else "INCLUDED_REVERTED"
    if finalized is None:
        return result
    if type(finalized) is not dict:
        raise ValueError("Invalid chain finalized checkpoint")
    checkpoint=_num(finalized.get("number"))
    if checkpoint>head:
        raise ValueError("Future finalized checkpoint")
    # Confirm the finalized block itself is the same canonical block if the
    # RPC returns one at the included height, not just a height number.
    if checkpoint<block:
        return result
    result["transaction_finalized"]=True
    if not success:
        result["state"]="FINALIZED_REVERTED"
        return result
    spent,received=_actual_token_flows(
        receipt.get("logs"),wallet=wallet,
        source_token=pair["source_token"],output_token=pair["output_token"])
    if spent!=quantity or received<minimum:
        raise ValueError("On-chain token amounts fail exact calldata bounds")
    result.update(
        state="FINALIZED_ERC20_SWAP_LEG",
        swap_erc20_leg_verified=True,
        source_spent_amount=format(
            Decimal(spent)/Decimal(10**pair["source_decimals"]),"f"),
        destination_received_amount=format(
            Decimal(received)/Decimal(10**pair["output_decimals"]),"f"),
    )
    return result


async def _receipt_rpc(
    client: httpx.AsyncClient, url: str, method: str,
    params: list, req_id: int,
) -> object:
    """Strict read-only JSON-RPC that accepts transaction/receipt objects."""
    response=await client.post(
        url, json={"jsonrpc":"2.0","id":req_id,
                   "method":method,"params":params},
        headers={"Content-Type":"application/json",
                 "Accept":"application/json"},
    )
    response.raise_for_status()
    if len(response.content)>96_000:
        raise ValueError("Oversized EVM transaction receipt response")
    data=response.json()
    if (type(data) is not dict or data.get("jsonrpc")!="2.0"
            or data.get("id")!=req_id or "error" in data
            or "result" not in data):
        raise ValueError("Untrusted EVM receipt RPC response")
    return data["result"]


async def check_user_evm_swap(
    did: str, tx_hash: str, *, asset: str, side: str, stable: str,
) -> dict:
    if type(tx_hash) is not str or not _HASH.fullmatch(tx_hash):
        raise HTTPException(422,"Invalid EVM transaction hash")
    try:
        pair=_pair(asset,stable,side)
    except ValueError:
        raise HTTPException(422,"Unsupported EVM settlement") from None
    user=await fetch_privy_user(did)
    try:
        wallet=verified_addresses(user,did)["ETH"]
        if type(wallet) is not str or not _ADDR.fullmatch(wallet):
            raise ValueError("Verified Privy EVM address unavailable")
    except (ValueError,KeyError,TypeError):
        raise HTTPException(503,"Verified Privy wallet required") from None
    if asset in CHAINS:
        chain,network,fallback,env=_RPC[asset]
    else:
        chain,network,fallback,env=_RPC["ETH_TOKENS"]
    if chain!=pair["chain_id"] or network!=pair["network"]:
        raise HTTPException(503,"EVM RPC registry mismatch")
    url=os.getenv(env,"").strip() or fallback
    if not url.startswith("https://") or any(ch in url for ch in " \n\t#@"):
        raise HTTPException(503,"HTTPS EVM RPC required")
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(9),follow_redirects=False,
        ) as client:
            rpc=(
                _receipt_rpc(client,url,"eth_chainId",[],1),
                _receipt_rpc(client,url,"eth_getTransactionByHash",[tx_hash],2),
                _receipt_rpc(client,url,"eth_getTransactionReceipt",[tx_hash],3),
                _receipt_rpc(client,url,"eth_blockNumber",[],4),
            )
            chain_id,tx,receipt,head=await asyncio.gather(*rpc)
            finalized=(
                await _receipt_rpc(client,url,"eth_getBlockByNumber",["finalized",False],5)
                if receipt is not None else None)
        return inspect_finalized_evm_swap(
            tx_hash,asset=asset,stable=stable,side=side,wallet=wallet,
            chain_id=chain_id,transaction=tx,receipt=receipt,
            latest=head,finalized=finalized)
    except (httpx.HTTPError,ValueError,TypeError,OverflowError):
        raise HTTPException(502,"EVM swap receipt not independently verified") from None
