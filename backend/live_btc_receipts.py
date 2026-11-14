"""Independently account-scoped Bitcoin UTXO transaction observation.

This observes a REAL Bitcoin transaction for the user's verified Privy
bitcoin-segwit address. It is not a THORChain settlement reconciliation:
public Bitcoin confirmations alone do NOT prove an ETH stablecoin deposit,
a correct swap rate, an authorized spender or a completed MiniNex trade.
No signing, UTXO spending, order state transitions or ledger writes occur.
"""
from __future__ import annotations

from decimal import Decimal
import re

import httpx
from fastapi import HTTPException

from .blockchain_preview import BTC_API, BTC_RE
from .privy_wallet_verification import fetch_privy_user
from .wallet_balances import verified_addresses

_TXID=re.compile(r"^[a-fA-F0-9]{64}$")
_HEX64=re.compile(r"^[a-fA-F0-9]{64}$")
_SATS_LIMIT=2_100_000_000_000_000
_RECOMMENDED_CONFIRMATIONS=6


def _satoshis(value: object) -> int:
    if type(value) is not int or value<0 or value>_SATS_LIMIT:
        raise ValueError("Invalid Esplora satoshi value")
    return value


def _tip_height(raw: object) -> int:
    if type(raw) is not str or len(raw)>12 or not re.fullmatch(r"[1-9][0-9]*",raw):
        raise ValueError("Unverified Bitcoin tip height")
    height=int(raw)
    if height>10_000_000:
        raise ValueError("Bitcoin height out of range")
    return height


def inspect_native_btc_transaction(
    txid: str, *, owner_address: str, row: object, tip: int,
) -> dict:
    if (type(txid) is not str or not _TXID.fullmatch(txid)
            or type(owner_address) is not str
            or not BTC_RE.fullmatch(owner_address)
            or type(tip) is not int or tip<1 or tip>10_000_000):
        raise ValueError("Invalid Bitcoin wallet or chain checkpoint")
    if type(row) is not dict or str(row.get("txid","")).lower()!=txid.lower():
        raise ValueError("Bitcoin transaction identity mismatch")
    inputs,outputs=row.get("vin"),row.get("vout")
    if (type(inputs) is not list or not 1<=len(inputs)<=500
            or type(outputs) is not list or not 1<=len(outputs)<=500):
        raise ValueError("Invalid Bitcoin UTXO transaction")
    # A real explorer receipt includes every input/output. An output must
    # have both a script/address entry and an exact satoshi quantity.
    credited=spent=0
    for txin in inputs:
        if type(txin) is not dict:
            raise ValueError("Malformed Bitcoin input")
        previous=txin.get("prevout")
        if previous is None:
            # Coinbase (or an incomplete explorer response) gives no proof
            # of user-owned spending. Never infer an outgoing amount.
            if txin.get("is_coinbase") is not True:
                raise ValueError("Cannot verify Bitcoin input source")
            continue
        if type(previous) is not dict:
            raise ValueError("Malformed Bitcoin prevout")
        value=_satoshis(previous.get("value"))
        if previous.get("scriptpubkey_address")==owner_address:
            spent+=value
    for txout in outputs:
        if type(txout) is not dict:
            raise ValueError("Malformed Bitcoin output")
        value=_satoshis(txout.get("value"))
        if txout.get("scriptpubkey_address")==owner_address:
            credited+=value
    if credited==0 and spent==0:
        raise ValueError("Transaction has no proven connection to owned BTC wallet")

    fee=_satoshis(row.get("fee"))
    status=row.get("status")
    if type(status) is not dict or type(status.get("confirmed")) is not bool:
        raise ValueError("Unverified Bitcoin chain status")
    confirmed=status["confirmed"]
    if confirmed:
        height=status.get("block_height")
        block_hash=status.get("block_hash")
        if (type(height) is not int or not 1<=height<=tip
                or type(block_hash) is not str or
                not _HEX64.fullmatch(block_hash)):
            raise ValueError("Invalid confirmed Bitcoin block")
        confirmations=tip-height+1
    else:
        if status.get("block_height") is not None or status.get("block_hash") is not None:
            raise ValueError("Unconfirmed UTXO has incompatible block evidence")
        confirmations=0
    net=credited-spent
    return {
        "mode":"BTC_MAINNET_WALLET_TX_OBSERVATION",
        "asset":"BTC","network":"bitcoin-mainnet",
        "txid":txid.lower(),
        "owned_address_proved_by_privy":True,
        "owner_input_observed":spent>0,
        "owner_output_observed":credited>0,
        "wallet_input_sats":str(spent),
        "wallet_output_sats":str(credited),
        "net_wallet_movement_btc":format(
            Decimal(net)/Decimal(100_000_000),"f"),
        "transaction_fee_btc":format(
            Decimal(fee)/Decimal(100_000_000),"f"),
        "confirmations_observed":confirmations,
        "explorer_reports_confirmed":confirmed,
        "recommended_confirmation_threshold_met":(
            confirmations>=_RECOMMENDED_CONFIRMATIONS),
        "bitcoin_spv_proof_verified":False,
        "crosschain_swap_fill_verified":False,
        "ethereum_stablecoin_settlement_verified":False,
        "refund_route_verified":False,
        "privy_policy_verified":False,
        "trade_order_recorded":False,
        "execution_enabled":False,
        "can_submit":False,
        "wallet_signed":False,
    }


async def _get_tx(client: httpx.AsyncClient, txid: str) -> dict:
    response=await client.get(BTC_API+"/tx/"+txid,
        headers={"Accept":"application/json"})
    response.raise_for_status()
    if len(response.content)>300_000:
        raise ValueError("Oversized Bitcoin transaction")
    value=response.json()
    if type(value) is not dict:
        raise ValueError("Malformed Bitcoin transaction response")
    return value


async def _get_tip(client: httpx.AsyncClient) -> int:
    response=await client.get(
        BTC_API+"/blocks/tip/height",
        headers={"Accept":"text/plain"})
    response.raise_for_status()
    if len(response.content)>32:
        raise ValueError("Oversized Bitcoin checkpoint")
    return _tip_height(response.text.strip())


async def check_native_btc_transaction(privy_did: str, txid: str) -> dict:
    if type(txid) is not str or not _TXID.fullmatch(txid):
        raise HTTPException(422,"Invalid Bitcoin transaction hash")
    user=await fetch_privy_user(privy_did)
    try:
        address=verified_addresses(user,privy_did)["BTC"]
        if type(address) is not str or not BTC_RE.fullmatch(address):
            raise ValueError("No uniquely verified BTC wallet")
    except (ValueError,KeyError,TypeError):
        raise HTTPException(503,"Verified native Bitcoin wallet unavailable") from None
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8), follow_redirects=False,
        ) as client:
            tx=await _get_tx(client,txid.lower())
            tip=await _get_tip(client)
        return inspect_native_btc_transaction(
            txid, owner_address=address,row=tx,tip=tip)
    except httpx.HTTPStatusError as err:
        if err.response.status_code==404:
            raise HTTPException(404,"Bitcoin transaction not found") from None
        raise HTTPException(502,"Bitcoin mainnet transaction unavailable") from None
    except (httpx.HTTPError,ValueError,KeyError,TypeError,OverflowError):
        raise HTTPException(502,"Bitcoin transaction cannot be verified") from None
