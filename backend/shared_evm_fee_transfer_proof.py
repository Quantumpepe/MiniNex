"""Owner-authorized ERC-20 fee payment: exact calldata and finalized receipt.

Owner signs in their own Privy wallet. There is deliberately NO additional
backend signer or generic transaction permission. One immutable obligation
may be credited only by one uniquely finalized onchain ERC-20 transfer.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING
import os
import re

import httpx
from fastapi import HTTPException

from .shared_evm_swap_receipts import _pair, _receipt_rpc
from .multicoin_evm_balances import _RPC
from .treasury import validate_treasury_address

_ASSETS=frozenset(("BNB","POL","AVAX","LINK","UNI"))
_HASH=re.compile(r"^0x[0-9a-f]{64}$")
_ADDR=re.compile(r"^0x[0-9a-f]{40}$")
_TRANSFER_TOPIC=("0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")
_TRANSFER_SELECTOR="0xa9059cbb"
_NETWORK_TREASURY={
    1:"MININEX_TREASURY_WALLET",
    56:"MININEX_TREASURY_BNB_WALLET",
    137:"MININEX_TREASURY_POL_WALLET",
    43114:"MININEX_TREASURY_AVAX_WALLET",
}


def owner_fee_treasury(*,asset:str,stable:str)->str:
    chain=_pair(asset,stable,"BUY")["chain_id"]
    setting=_NETWORK_TREASURY[chain]
    value=os.getenv(setting,"").strip()
    if not value:
        raise HTTPException(503,"Explicit chain treasury address not configured")
    try:
        return validate_treasury_address(value).lower()
    except ValueError:
        raise HTTPException(503,"Invalid chain treasury address") from None


def exact_fee_token_units(*,fee_due_usd:object,stable_usd:object,decimals:int)->int:
    """Compute exact stablecoin atomic amount with no implicit USD parity.

    A single atomic unit is the maximum possible upward rounding, never
    an arbitrary platform trading cap or assumed one-dollar stablecoin.
    """
    if type(decimals) is not int or not 1<=decimals<=18:
        raise ValueError("Unverified stablecoin decimals")
    try:
        due=Decimal(str(fee_due_usd))
        rate=Decimal(str(stable_usd))
    except (InvalidOperation,ValueError,TypeError):
        raise ValueError("Fee USD and exact market conversion required") from None
    if (not due.is_finite() or not rate.is_finite()
            or not Decimal(0)<due<Decimal("1e22")
            or not Decimal("0.95")<=rate<=Decimal("1.05")):
        raise ValueError("Unverified fee or stablecoin USD rate")
    atomic=(due/rate*Decimal(10)**decimals).to_integral_value(
        rounding=ROUND_CEILING)
    if not 0<atomic<10**45:
        raise ValueError("Fee cannot be represented by chain token")
    return int(atomic)


def owner_fee_transfer_draft(
    *,asset:str,stable:str,wallet:str,treasury:str,
    fee_due_usd:object,stable_usd:object,
)->dict:
    pair=_pair(asset,stable,"BUY")
    if (asset not in _ASSETS or type(wallet) is not str
            or not _ADDR.fullmatch(wallet)
            or type(treasury) is not str or not _ADDR.fullmatch(treasury)
            or wallet==treasury):
        raise ValueError("Unverified fee owner or treasury")
    amount=exact_fee_token_units(
        fee_due_usd=fee_due_usd,stable_usd=stable_usd,
        decimals=pair["source_decimals"])
    calldata=(_TRANSFER_SELECTOR+f"{int(treasury,16):064x}"
              +f"{amount:064x}")
    return {
        "mode":"OWNER_MANUALLY_SIGNED_MININEX_FEE_ONLY",
        "asset":asset,"chain_id":pair["chain_id"],
        "network":pair["network"],"token_symbol":stable,
        "token_address":pair["source_token"].lower(),
        "decimals":pair["source_decimals"],
        "amount_atomic":str(amount),
        "fee_due_usd":str(fee_due_usd),
        "stablecoin_usd_rate":str(stable_usd),
        "treasury_wallet":treasury,
        "owner_wallet":wallet,
        "unsigned_transaction":{
            "from":wallet,"to":pair["source_token"].lower(),
            "chainId":pair["chain_id"],"value":"0x0","data":calldata,
        },
        "requires_owner_privy_signature":True,
        "wallet_signed":False,"paid_onchain":False,
        "backend_can_autosend":False,
    }


def verified_owner_fee_transfer(
    *,draft:dict,tx_hash:str,transaction:object,
    receipt:object,finalized_block:object,mined_block:object,
)->dict:
    """Accept only a canonical finalized, exact ERC-20 Transfer to treasury."""
    if (type(draft) is not dict
            or draft.get("mode")!="OWNER_MANUALLY_SIGNED_MININEX_FEE_ONLY"
            or type(tx_hash) is not str or not _HASH.fullmatch(tx_hash)
            or type(transaction) is not dict or type(receipt) is not dict
            or type(finalized_block) is not dict
            or type(mined_block) is not dict):
        raise ValueError("Exact finalized onchain fee proof required")
    to=draft["token_address"]
    owner=draft["owner_wallet"]
    treasury=draft["treasury_wallet"]
    amount=int(draft["amount_atomic"])
    tx=draft["unsigned_transaction"]
    if (type(transaction.get("input")) is not str
            or transaction["input"].lower()!=tx["data"].lower()
            or transaction.get("hash")!=tx_hash
            or transaction.get("from","").lower()!=owner
            or transaction.get("to","").lower()!=to
            or transaction.get("value") not in ("0x0","0x00","0x0000")
            or receipt.get("transactionHash")!=tx_hash
            or receipt.get("status") not in ("0x1","0x01")
            or type(receipt.get("logs")) is not list):
        raise ValueError("Onchain fee transaction does not match draft")
    try:
        block=int(receipt["blockNumber"],16)
        mined=int(mined_block["number"],16)
        finalized=int(finalized_block["number"],16)
    except (KeyError,ValueError,TypeError):
        raise ValueError("No canonical fee receipt block proof") from None
    if (block<=0 or block!=mined or finalized<block
            or receipt.get("blockHash")!=mined_block.get("hash")
            or transaction.get("blockHash")!=mined_block.get("hash")):
        raise ValueError("Fee transaction not yet canonically finalized")
    owner_topic="0x"+f"{int(owner,16):064x}"
    treasury_topic="0x"+f"{int(treasury,16):064x}"
    matches=[]
    for log in receipt["logs"]:
        if type(log) is not dict or log.get("address","").lower()!=to:
            continue
        topics=log.get("topics")
        if (type(topics) is list and len(topics)==3
                and all(type(t) is str for t in topics)
                and topics[0].lower()==_TRANSFER_TOPIC
                and topics[1].lower()==owner_topic
                and topics[2].lower()==treasury_topic):
            try:
                raw=log.get("data")
                if (type(raw) is not str or not raw.startswith("0x")
                        or len(raw)!=66 or int(raw,16)!=amount):
                    raise ValueError("Exact fee token transfer amount differs")
            except (ValueError,TypeError):
                raise ValueError("Fee transfer event invalid") from None
            matches.append(log)
    if len(matches)!=1:
        raise ValueError("No unique matching verified ERC20 fee transfer event")
    return {
        "mode":"FINALIZED_OWNER_SIGNED_EVM_FEE_PAYMENT",
        "asset":draft["asset"],"chain_id":draft["chain_id"],
        "tx_hash":tx_hash,"wallet_address":owner,
        "treasury_wallet":treasury,"token_address":to,
        "amount_atomic":str(amount),"onchain_finalized":True,
        "paper":False,
    }


async def fetch_owner_fee_transfer_proof(
    *,asset:str,stable:str,draft:dict,tx_hash:str,
)->dict:
    """Use only original verified chain registry and read-only HTTPS RPC."""
    if (asset not in _ASSETS or type(draft) is not dict
            or draft.get("asset")!=asset
            or draft.get("token_symbol")!=stable
            or type(tx_hash) is not str or not _HASH.fullmatch(tx_hash)):
        raise ValueError("Invalid EVM fee settlement request")
    pair=_pair(asset,stable,"BUY")
    key=asset if asset in _RPC else "ETH_TOKENS"
    chain,network,fallback,env=_RPC[key]
    if (chain!=pair["chain_id"] or network!=pair["network"]
            or draft.get("chain_id")!=chain):
        raise ValueError("Wrong EVM fee network")
    url=os.getenv(env,"").strip() or fallback
    if not url.startswith("https://") or any(c in url for c in " \n\t\r#@"):
        raise HTTPException(503,"Fee network RPC unavailable")
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(9),follow_redirects=False,
        ) as client:
            chain_id=await _receipt_rpc(client,url,"eth_chainId",[],1)
            if int(chain_id,16)!=chain:
                raise ValueError("Fee receipt connected to wrong chain")
            transaction=await _receipt_rpc(
                client,url,"eth_getTransactionByHash",[tx_hash],2)
            receipt=await _receipt_rpc(
                client,url,"eth_getTransactionReceipt",[tx_hash],3)
            if transaction is None or receipt is None:
                return {"state":"AWAITING_FINALITY","onchain_finalized":False}
            mined=await _receipt_rpc(client,url,"eth_getBlockByNumber",[
                receipt["blockNumber"],False],4)
            finalized=await _receipt_rpc(client,url,"eth_getBlockByNumber",[
                "finalized",False],5)
        return verified_owner_fee_transfer(
            draft=draft,tx_hash=tx_hash,transaction=transaction,
            receipt=receipt,finalized_block=finalized,mined_block=mined)
    except (httpx.HTTPError,KeyError,ValueError,TypeError,OverflowError):
        raise HTTPException(502,"Verified fee transfer receipt unavailable") from None
