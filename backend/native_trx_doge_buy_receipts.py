"""Independent TRON/Dogecoin native BUY receipts for the original owner wallet.

These functions only read already-finalized chain transactions. A THORChain
outbound report is NOT sufficient to credit a user: verify the exact original
native owner, native txid, chain finality and atomic amount. No signing,
broadcasting, real profit inference, new limits on budgets, or fee collection.
"""
from __future__ import annotations

import asyncio
import re

import httpx

from .native_coin_balances import _decode_b58, _rpc_url, _valid_address, _BASE58

_HEX64=re.compile(r"^[0-9a-fA-F]{64}$")
_HEX42=re.compile(r"^41[0-9a-fA-F]{40}$")
_MAX_ATOMIC=10**45


def _validate_native_credit_input(asset:str,owner:str,hash_:str,amount:int):
    if (asset not in ("TRX","DOGE")
            or not _valid_address(asset,owner)
            or type(hash_) is not str or not _HEX64.fullmatch(hash_)
            or type(amount) is not int or not 0<amount<_MAX_ATOMIC):
        raise ValueError("Original native receiving chain, owner or quantity invalid")


def inspect_original_tron_credit(
    owner:str,hash_:str,amount:int,receipt:object,
)->dict:
    """TRON SolidityNode transfer+receipt, exact sun to owner (not TRC20)."""
    _validate_native_credit_input("TRX",owner,hash_,amount)
    if (type(receipt) is not dict
            or set(receipt)!={"transaction","info"}
            or type(receipt["transaction"]) is not dict
            or type(receipt["info"]) is not dict):
        raise ValueError("TRON solidified owner transaction and receipt unavailable")
    tx,info=receipt["transaction"],receipt["info"]
    if (str(tx.get("txID","")).lower()!=hash_.lower()
            or str(info.get("id","")).lower()!=hash_.lower()
            or type(info.get("blockNumber")) is not int
            or info["blockNumber"]<=0
            or type(tx.get("ret")) is not list
            or len(tx["ret"])!=1
            or type(tx["ret"][0]) is not dict
            or tx["ret"][0].get("contractRet")!="SUCCESS"
            or info.get("result") in ("FAILED","REVERT")
            or type(info.get("receipt",{})) is not dict
            or info.get("receipt",{}).get("result") not in (None,"SUCCESS")):
        raise ValueError("TRON original transfer not successful and solidified")
    raw=tx.get("raw_data")
    if type(raw) is not dict or type(raw.get("contract")) is not list:
        raise ValueError("TRON native transfer contract unavailable")
    contracts=raw["contract"]
    if len(contracts)!=1 or type(contracts[0]) is not dict:
        raise ValueError("Ambiguous TRON original payout")
    contract=contracts[0]
    value=contract.get("parameter",{}).get("value")
    if (contract.get("type")!="TransferContract"
            or type(value) is not dict
            or type(value.get("owner_address")) is not str
            or type(value.get("to_address")) is not str
            or not _HEX42.fullmatch(value["owner_address"])
            or not _HEX42.fullmatch(value["to_address"])):
        raise ValueError("Original payout is not an exact native TRX transfer")
    decoded=_decode_b58(owner,_BASE58)
    expected_to=decoded[:21].hex().lower() if decoded is not None else ""
    if (value["to_address"].lower()!=expected_to
            or value["owner_address"].lower()==expected_to
            or type(value.get("amount")) is not int
            or value["amount"]!=amount):
        raise ValueError("TRX original owner credit amount or source mismatches")
    return {
        "chain_height":info["blockNumber"],
        "chain_finality":"TRON_SOLIDIFIED",
        "credited_atomic":str(amount),
    }


def inspect_original_doge_credit(
    owner:str,hash_:str,amount:int,receipt:object,
)->dict:
    """DOGE mainnet confirmed UTXO output; complete inputs/outputs required."""
    _validate_native_credit_input("DOGE",owner,hash_,amount)
    if (type(receipt) is not dict
            or str(receipt.get("hash","")).lower()!=hash_.lower()
            or type(receipt.get("confirmations")) is not int
            or receipt["confirmations"]<6
            or type(receipt.get("block_height")) is not int
            or receipt["block_height"]<=0
            or type(receipt.get("block_hash")) is not str
            or not _HEX64.fullmatch(receipt["block_hash"])
            or receipt.get("double_spend") is not False):
        raise ValueError("DOGE original chain transaction not confirmed")
    ins,outs=receipt.get("inputs"),receipt.get("outputs")
    if (type(ins) is not list or type(outs) is not list
            or not ins or not outs
            or len(ins)>500 or len(outs)>500
            or type(receipt.get("vin_sz")) is not int
            or type(receipt.get("vout_sz")) is not int
            or receipt["vin_sz"]!=len(ins)
            or receipt["vout_sz"]!=len(outs)):
        raise ValueError("Incomplete DOGE original transaction inputs or outputs")
    credited=0
    for item in ins:
        if (type(item) is not dict
                or type(item.get("addresses")) is not list
                or not item["addresses"]
                or any(type(x) is not str for x in item["addresses"])):
            raise ValueError("DOGE input ownership not independently observable")
        if owner in item["addresses"]:
            raise ValueError("Owner DOGE inputs cannot be mislabelled as new credit")
    for output in outs:
        if (type(output) is not dict
                or type(output.get("addresses")) is not list
                or not output["addresses"]
                or any(type(x) is not str for x in output["addresses"])
                or type(output.get("value")) is not int
                or output["value"]<0):
            raise ValueError("DOGE output receipt malformed or truncated")
        if owner in output["addresses"]:
            if output["addresses"]!=[owner]:
                raise ValueError("Ambiguous multi-owner DOGE output")
            credited+=output["value"]
    if credited!=amount:
        raise ValueError("Exact confirmed original DOGE owner credit absent")
    return {
        "chain_height":receipt["block_height"],
        "chain_finality":"DOGE_SIX_CONFIRMED",
        "credited_atomic":str(amount),
    }


async def get_original_trx_doge_receipt(asset:str,hash_:str)->dict:
    """Fetch ONLY the original native txid from a fixed mainnet read API."""
    if asset not in ("TRX","DOGE") or type(hash_) is not str or not _HEX64.fullmatch(hash_):
        raise ValueError("Unsupported original TRON/DOGE transaction reference")
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10),follow_redirects=False,
    ) as client:
        if asset=="DOGE":
            url=_rpc_url("DOGE").rstrip("/")
            if not url.endswith("/v1/doge/main/addrs"):
                raise ValueError("DOGE explorer endpoint cannot be pinned to mainnet")
            url=url.removesuffix("/addrs")+"/txs/"+hash_.lower()
            response=await client.get(
                url,params={"limit":500},
                headers={"Accept":"application/json"})
            response.raise_for_status()
            if len(response.content)>450_000:
                raise ValueError("Oversized DOGE mainnet receipt")
            result=response.json()
            if type(result) is not dict:
                raise ValueError("Missing original Dogecoin transaction")
            return result
        base=_rpc_url("TRX").rstrip("/")
        if not base.endswith("/walletsolidity/getaccount"):
            raise ValueError("TRON SolidityNode must be pinned to mainnet")
        base=base.removesuffix("/getaccount")
        payload={"value":hash_.lower(),"visible":False}
        a,b=await asyncio.gather(
            client.post(base+"/gettransactionbyid",json=payload,
                headers={"Accept":"application/json",
                         "Content-Type":"application/json"}),
            client.post(base+"/gettransactioninfobyid",json=payload,
                headers={"Accept":"application/json",
                         "Content-Type":"application/json"}),
        )
        a.raise_for_status();b.raise_for_status()
        if len(a.content)>140_000 or len(b.content)>140_000:
            raise ValueError("Oversized solidified TRON transaction")
        return {"transaction":a.json(),"info":b.json()}
