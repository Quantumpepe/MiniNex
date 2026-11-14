"""Real Ethereum SwapRouter02 receipt and ERC20 token-delta verification.

The older Universal Router receipt observer knows only that a transaction
succeeded; it cannot independently claim traded token quantities. This
separate, owner-scoped reader verifies the exact single-hop V3 swap calldata
and actual ERC20 Transfer events after the Ethereum finalized checkpoint.

It proves a SINGLE stable/WETH leg, not a completed native ETH cycle, a
Privy-signed MiniNex order, or an HWM/fee settlement. No ledger writes, wallet
signing, token approvals, polling, broadcasts or policy changes occur.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
import re

import httpx
from eth_utils import keccak
from fastapi import HTTPException

from .eth_swaprouter02_preflight import (
    EXACT_INPUT_SINGLE_SELECTOR, SWAPROUTER02_MAINNET, WETH_MAINNET,
    inspect_swaprouter02_exact_input,
)
from .live_eth_quotes import STABLES
from .live_eth_receipts import _rpc, _num
from .privy_wallet_verification import fetch_privy_user
from .wallet_balances import verified_addresses

_TX_HASH=re.compile(r"^0x[0-9a-fA-F]{64}$")
_ADDRESS=re.compile(r"^0x[0-9a-fA-F]{40}$")
_HEX_64=re.compile(r"^0x[0-9a-fA-F]{64}$")
_CALL=re.compile(r"^0x[0-9a-fA-F]+$")
_TRANSFER_TOPIC="0x"+keccak(text="Transfer(address,address,uint256)").hex()


def _read_call(raw: object, *, owner: str, stable: str,
               side: str) -> tuple[dict,int,int]:
    if (type(raw) is not str or not _CALL.fullmatch(raw)
            or len(raw)!=2+(4+7*32)*2
            or not raw.lower().startswith(EXACT_INPUT_SINGLE_SELECTOR)):
        raise ValueError("Unrecognized Router02 transaction calldata")
    if (type(owner) is not str or not _ADDRESS.fullmatch(owner)
            or side not in ("BUY","SELL") or stable not in STABLES):
        raise ValueError("Invalid router route identity")
    words=[int(raw[10+i*64:10+(i+1)*64],16) for i in range(7)]
    source,received,fee,recipient,amount_in,min_out,limit=words
    if (source >= 2**160 or received >= 2**160 or recipient >= 2**160
            or limit!=0 or not 0<amount_in<10**45 or not 0<min_out<10**45):
        raise ValueError("Non-canonical V3 swap calldata")
    tx={"from":owner,"to":SWAPROUTER02_MAINNET,
        "chainId":1,"value":"0","data":raw}
    inspected=inspect_swaprouter02_exact_input(
        tx,wallet=owner,stable=stable,side=side,
        input_atomic=str(amount_in),minimum_output_atomic=str(min_out))
    if not all(inspected.get(field) is True for field in (
        "token_pair_verified","recipient_verified","quote_minimum_verified",
    )):
        raise ValueError("Router02 call not restricted to user")
    return inspected,amount_in,min_out


def _topic_address(topic: object) -> str:
    if type(topic) is not str or not _HEX_64.fullmatch(topic):
        raise ValueError("Malformed ERC20 Transfer topic")
    value=int(topic,16)
    if value>=2**160:
        raise ValueError("Non-canonical ERC20 Transfer address")
    return "0x"+value.to_bytes(20,"big").hex()


def _actual_token_flows(logs: object, *, wallet: str,
                        source_token: str, output_token: str
                        ) -> tuple[int,int]:
    if type(logs) is not list or len(logs)>400:
        raise ValueError("Invalid Ethereum receipt event count")
    source_spent=output_received=0
    input_matches=output_matches=0
    for entry in logs:
        if type(entry) is not dict:
            raise ValueError("Malformed ERC20 event")
        contract=entry.get("address")
        if type(contract) is not str or not _ADDRESS.fullmatch(contract):
            raise ValueError("Ethereum event contract identity unavailable")
        if contract.lower() not in (source_token.lower(),output_token.lower()):
            continue
        topics=entry.get("topics")
        if type(topics) is not list or not topics:
            raise ValueError("Allowed token event is missing topics")
        # Ignore ERC20 Approval and unrelated non-Transfer events.
        if type(topics[0]) is not str or topics[0].lower()!=_TRANSFER_TOPIC:
            continue
        if len(topics)!=3:
            raise ValueError("Unrecognized ERC20 Transfer event ABI")
        sender=_topic_address(topics[1])
        recipient=_topic_address(topics[2])
        raw=entry.get("data")
        if type(raw) is not str or not _HEX_64.fullmatch(raw):
            raise ValueError("Invalid ERC20 transfer quantity")
        units=int(raw,16)
        if not 0<units<10**45:
            raise ValueError("Unbounded ERC20 transfer quantity")
        if contract.lower()==source_token.lower() and sender==wallet.lower():
            input_matches+=1
            source_spent+=units
        if contract.lower()==output_token.lower() and recipient==wallet.lower():
            output_matches+=1
            output_received+=units
    # A single-hop router swap must produce one wallet debit and one wallet
    # credit. Ambiguous extra transfers are not reliable fills.
    if input_matches!=1 or output_matches!=1:
        raise ValueError("No unambiguous owner token input/output")
    return source_spent,output_received


def inspect_eth_router02_receipt(
    tx_hash: str, *, wallet: str, stable: str, side: str,
    chain_id: object, transaction: object, receipt: object,
    latest: object, finalized: object,
) -> dict:
    if (type(tx_hash) is not str or not _TX_HASH.fullmatch(tx_hash)
            or type(wallet) is not str or not _ADDRESS.fullmatch(wallet)
            or stable not in STABLES or side not in ("BUY","SELL")):
        raise ValueError("Invalid Ethereum router receipt request")
    if _num(chain_id)!=1:
        raise ValueError("Wrong Ethereum RPC network")
    head=_num(latest)
    result={
        "mode":"ETH_ROUTER02_ERC20_FILL_OBSERVATION",
        "asset":"ETH","network":"ethereum-mainnet",
        "tx_hash":tx_hash.lower(),"side":side,"stable":stable,
        "swap_output_asset":"WETH" if side=="BUY" else stable,
        "transaction_seen":False,"transaction_finalized":False,
        "swap_erc20_leg_verified":False,
        "native_eth_cycle_complete":False,
        "source_spent_amount":None,"destination_received_amount":None,
        "network_fee_eth":None,"confirmations":0,
        "state":"NOT_FOUND",
        "real_order_recorded":False,"privy_signer_authorized":False,
        "execution_enabled":False,"can_submit":False,
    }
    if transaction is None:
        if receipt is not None:
            raise ValueError("Receipt exists without a transaction")
        return result
    if (type(transaction) is not dict
            or str(transaction.get("hash","")).lower()!=tx_hash.lower()
            or str(transaction.get("from","")).lower()!=wallet.lower()
            or str(transaction.get("to","")).lower()!=SWAPROUTER02_MAINNET.lower()
            or _num(transaction.get("chainId"))!=1 or
            _num(transaction.get("value"))!=0):
        raise ValueError("Unowned or unexpected Ethereum swap transaction")
    _,amount_in,min_out=_read_call(
        transaction.get("input"),owner=wallet,side=side,stable=stable)
    result["transaction_seen"]=True
    result["state"]="PENDING"
    if receipt is None:
        if transaction.get("blockHash") not in (None,"0x") or transaction.get("blockNumber") is not None:
            raise ValueError("Pending transaction has inconsistent block fields")
        return result
    if (type(receipt) is not dict
            or str(receipt.get("transactionHash","")).lower()!=tx_hash.lower()
            or str(receipt.get("from","")).lower()!=wallet.lower()
            or str(receipt.get("to","")).lower()!=SWAPROUTER02_MAINNET.lower()):
        raise ValueError("Unrelated Ethereum transaction receipt")
    block_hash=receipt.get("blockHash")
    if type(block_hash) is not str or not _TX_HASH.fullmatch(block_hash):
        raise ValueError("Invalid receipt block identifier")
    block=_num(receipt.get("blockNumber"))
    if not 0<block<=head or block_hash.lower()!=str(transaction.get("blockHash","")).lower() or _num(transaction.get("blockNumber"))!=block:
        raise ValueError("Receipt is not bound to transaction block")
    success=_num(receipt.get("status"))
    if success not in (0,1):
        raise ValueError("Invalid receipt status")
    gas=_num(receipt.get("gasUsed"))
    fee=_num(receipt.get("effectiveGasPrice"))
    if not 0<gas<=30_000_000 or not 0<fee<=10**12:
        raise ValueError("Unbounded receipt gas")
    result["confirmations"]=min(head-block+1,1_000_000)
    result["network_fee_eth"]=format(Decimal(gas*fee)/Decimal(10**18),"f")
    result["state"]="INCLUDED_SUCCESS" if success else "INCLUDED_REVERTED"
    if finalized is None:
        return result
    if type(finalized) is not dict:
        raise ValueError("Ethereum finalized checkpoint malformed")
    final_block=_num(finalized.get("number"))
    if final_block>head:
        raise ValueError("Finalization checkpoint beyond canonical head")
    if final_block<block:
        return result
    result["transaction_finalized"]=True
    if not success:
        result["state"]="FINALIZED_REVERTED"
        return result
    source_token=STABLES[stable] if side=="BUY" else WETH_MAINNET
    output_token=WETH_MAINNET if side=="BUY" else STABLES[stable]
    spent,received=_actual_token_flows(
        receipt.get("logs"),wallet=wallet,source_token=source_token,
        output_token=output_token)
    if spent!=amount_in or received<min_out:
        raise ValueError("Finalized swap amount inconsistent with calldata")
    result["state"]="FINALIZED_ERC20_SWAP_LEG"
    result["swap_erc20_leg_verified"]=True
    result["source_spent_amount"]=format(
        Decimal(spent)/Decimal(10**(6 if side=="BUY" else 18)),"f")
    result["destination_received_amount"]=format(
        Decimal(received)/Decimal(10**(18 if side=="BUY" else 6)),"f")
    return result


async def check_user_router02_receipt(did: str, tx_hash: str,
                                       *, side: str, stable: str) -> dict:
    if type(tx_hash) is not str or not _TX_HASH.fullmatch(tx_hash):
        raise HTTPException(422,"Invalid Ethereum receipt hash")
    if side not in ("BUY","SELL") or stable not in STABLES:
        raise HTTPException(422,"Invalid ETH swap direction")
    user=await fetch_privy_user(did)
    try:
        wallet=verified_addresses(user,did)["ETH"]
        if type(wallet) is not str or not _ADDRESS.fullmatch(wallet):
            raise ValueError("Missing verified ETH wallet")
    except (ValueError,TypeError,KeyError):
        raise HTTPException(503,"Verified Ethereum wallet unavailable") from None
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8),follow_redirects=False,
        ) as client:
            chain,tx,receipt,head=await asyncio.gather(
                _rpc(client,"eth_chainId",[],1),
                _rpc(client,"eth_getTransactionByHash",[tx_hash],2),
                _rpc(client,"eth_getTransactionReceipt",[tx_hash],3),
                _rpc(client,"eth_blockNumber",[],4),
            )
            finalized=(await _rpc(client,"eth_getBlockByNumber",
                                  ["finalized",False],5)
                       if receipt is not None else None)
        return inspect_eth_router02_receipt(
            tx_hash,wallet=wallet,stable=stable,side=side,
            chain_id=chain,transaction=tx,receipt=receipt,
            latest=head,finalized=finalized)
    except (httpx.HTTPError,ValueError,TypeError,OverflowError):
        raise HTTPException(502,"Ethereum swap token fill unavailable") from None
