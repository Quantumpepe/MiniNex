"""Independent finalized swap verification for all six EVM MiniNex assets."""
from decimal import Decimal
import asyncio

import pytest
from eth_utils import keccak
from fastapi import HTTPException

from backend.shared_evm_swap_receipts import (
    _pair, inspect_finalized_evm_swap, check_user_evm_swap,
)

WALLET="0x"+"ab"*20
SENDER="0x"+"cd"*20
POOL="0x"+"ef"*20
HASH="0x"+"11"*32
BLOCK="0x"+"22"*32
TRANSFER="0x"+keccak(text="Transfer(address,address,uint256)").hex()


def topic(address):
    return "0x"+format(int(address,16),"064x")


def transfer(token,sender,recipient,amount):
    return {"address":token,"topics":[TRANSFER,topic(sender),topic(recipient)],
            "data":"0x"+format(amount,"064x")}


def witness(asset,side,stable):
    pair=_pair(asset,stable,side)
    quantity=10**pair["source_decimals"]
    output=2*10**pair["output_decimals"]
    words=(
        int(pair["source_token"],16),int(pair["output_token"],16),
        3000,int(WALLET,16),quantity,output-1,0,
    )
    tx={
        "hash":HASH,"from":WALLET,"to":pair["router"],
        "chainId":hex(pair["chain_id"]),"value":"0x0",
        "input":"0x04e45aaf"+"".join(format(x,"064x") for x in words),
        "blockHash":BLOCK,"blockNumber":"0x64",
    }
    receipt={
        "transactionHash":HASH,"from":WALLET,"to":pair["router"],
        "blockHash":BLOCK,"blockNumber":"0x64","status":"0x1",
        "gasUsed":"0x186a0","effectiveGasPrice":"0x3b9aca00",
        "logs":[
            transfer(pair["source_token"],WALLET,POOL,quantity),
            transfer(pair["output_token"],POOL,WALLET,output),
        ],
    }
    return pair,tx,receipt


@pytest.mark.parametrize("asset,stable",[
    ("ETH","USDC"),("ETH","USDT"),("LINK","USDC"),
    ("UNI","USDT"),("BNB","USDT"),("POL","USDC"),("AVAX","USDC"),
])
@pytest.mark.parametrize("side",["BUY","SELL"])
def test_six_assets_two_sides_verify_real_finalized_owner_erc20_flows(
    asset,stable,side
):
    pair,tx,receipt=witness(asset,side,stable)
    result=inspect_finalized_evm_swap(
        HASH,asset=asset,stable=stable,side=side,wallet=WALLET,
        chain_id=hex(pair["chain_id"]),transaction=tx,receipt=receipt,
        latest="0x70",finalized={"number":"0x68"},
        canonical={"number":"0x64","hash":BLOCK},
    )
    assert result["asset"]==asset
    assert result["wallet_address"]==WALLET.lower()
    assert result["state"]=="FINALIZED_ERC20_SWAP_LEG"
    assert result["swap_erc20_leg_verified"] is True
    assert result["transaction_finalized"] is True
    assert Decimal(result["source_spent_amount"])==1
    assert Decimal(result["destination_received_amount"])==2
    assert Decimal(result["network_fee_native"])>0
    assert result["native_coin_cycle_complete"] is False
    assert result["performance_fee_assessed"] is False
    assert result["can_submit"] is False


@pytest.mark.parametrize("asset,stable",[("BNB","USDT"),("POL","USDC"),("AVAX","USDC"),("LINK","USDC")])
def test_wrong_network_cannot_credit_a_different_chain(asset,stable):
    pair,tx,receipt=witness(asset,"BUY",stable)
    with pytest.raises(ValueError):
        inspect_finalized_evm_swap(
            HASH,asset=asset,stable=stable,side="BUY",wallet=WALLET,
            chain_id="0x1" if pair["chain_id"]!=1 else "0x38",
            transaction=tx,receipt=receipt,latest="0x70",
            finalized={"number":"0x68"},
        canonical={"number":"0x64","hash":BLOCK},
        )


@pytest.mark.parametrize("mutation",["router","calldata","receipt","transfer","min_output"])
def test_unowned_or_inconsistent_token_fills_are_rejected(mutation):
    pair,tx,receipt=witness("BNB","BUY","USDT")
    if mutation=="router":
        tx["to"]=POOL
    elif mutation=="calldata":
        tx["input"]="0x04e45aaf"+format(int(POOL,16),"064x")+tx["input"][74:]
    elif mutation=="receipt":
        receipt["blockHash"]="0x"+"33"*32
    elif mutation=="transfer":
        receipt["logs"][0]["data"]="0x"+format(1,"064x")
    else:
        raw=tx["input"]
        # Replace minOut (sixth uint256 word) with more than delivered.
        at=10+5*64
        tx["input"]=raw[:at]+format(3*10**18,"064x")+raw[at+64:]
    with pytest.raises(ValueError):
        inspect_finalized_evm_swap(
            HASH,asset="BNB",stable="USDT",side="BUY",wallet=WALLET,
            chain_id="0x38",transaction=tx,receipt=receipt,latest="0x70",
            finalized={"number":"0x68"},
        canonical={"number":"0x64","hash":BLOCK},
        )


def test_pending_and_included_not_treated_as_finalized_swap():
    pair,tx,receipt=witness("POL","SELL","USDC")
    result=inspect_finalized_evm_swap(
        HASH,asset="POL",stable="USDC",side="SELL",wallet=WALLET,
        chain_id="0x89",transaction=tx,receipt=receipt,
        latest="0x65",finalized={"number":"0x63"},
        canonical=None,
    )
    assert result["state"]=="INCLUDED_SUCCESS"
    assert result["swap_erc20_leg_verified"] is False
    assert result["source_spent_amount"] is None


def test_reverted_transaction_never_credited_to_user():
    pair,tx,receipt=witness("AVAX","SELL","USDC")
    receipt["status"]="0x0"
    result=inspect_finalized_evm_swap(
        HASH,asset="AVAX",stable="USDC",side="SELL",wallet=WALLET,
        chain_id="0xa86a",transaction=tx,receipt=receipt,
        latest="0x70",finalized={"number":"0x68"},
        canonical={"number":"0x64","hash":BLOCK},
    )
    assert result["state"]=="FINALIZED_REVERTED"
    assert result["swap_erc20_leg_verified"] is False


def test_canonical_chain_hash_required_for_finalized_erc20_swap():
    pair,tx,receipt=witness("LINK","BUY","USDC")
    fixed=dict(asset="LINK",stable="USDC",side="BUY",wallet=WALLET,
               chain_id=hex(pair["chain_id"]),transaction=tx,receipt=receipt,
               latest="0x70",finalized={"number":"0x68"})
    report=inspect_finalized_evm_swap(HASH,**fixed,canonical=None)
    assert report["state"]=="INCLUDED_SUCCESS"
    assert report["transaction_finalized"] is False
    with pytest.raises(ValueError):
        inspect_finalized_evm_swap(
            HASH,**fixed,canonical={"number":"0x64",
                                    "hash":"0x"+"ef"*32})


def test_unverified_settlement_is_rejected_before_network_access():
    with pytest.raises(ValueError):
        _pair("BNB","USDC","BUY")
    with pytest.raises(ValueError):
        _pair("DOGE","USDC","BUY")


def test_api_checks_privy_user_and_never_signs():
    import inspect
    from backend.app import app
    r=next(x for x in app.routes
           if x.path=="/api/live/evm/finalized-swap/{asset}/{tx_hash}")
    body=inspect.getsource(r.endpoint)
    assert "verify_privy_bearer" in body
    assert "check_user_evm_swap(" in body
    assert "broadcast_privy" not in body


def test_owner_manual_evm_leg_only_records_finalized_canonical_token_transfers():
    from backend.manual_evm_verified_legs import validated_manual_evm_leg
    pair, tx, receipt = witness("QNT", "BUY", "USDC")
    proof = inspect_finalized_evm_swap(
        HASH, asset="QNT", stable="USDC", side="BUY", wallet=WALLET,
        chain_id=hex(pair["chain_id"]), transaction=tx, receipt=receipt,
        latest="0x70", finalized={"number": "0x68"},
        canonical={"number": "0x64", "hash": BLOCK},
    )
    leg = validated_manual_evm_leg(
        asset="QNT", tx_hash=HASH, side="BUY", stable="USDC", receipt=proof
    )
    assert leg["tx_hash"] == HASH
    assert leg["wallet_address"] == WALLET.lower()
    assert leg["source_amount"] == "1"
    assert leg["destination_amount"] == "2"
    assert Decimal(leg["network_fee_native"]) > 0
    for change in (
        {"state": "INCLUDED_SUCCESS"},
        {"transaction_finalized": False},
        {"wallet_address": "0x" + "bb"*20},
        {"side": "SELL"},
        {"network": "polygon-pos-mainnet"},
        {"destination_received_amount": "NaN"},
        {"performance_fee_assessed": True},
    ):
        with pytest.raises(ValueError):
            validated_manual_evm_leg(
                asset="QNT", tx_hash=HASH, side="BUY", stable="USDC",
                receipt={**proof, **change},
            )


def test_manual_evm_record_requires_backend_privy_owner_receipt_not_client_proof(monkeypatch):
    from backend import manual_evm_verified_legs as manual
    pair, tx, receipt = witness("BNB", "SELL", "USDT")
    proof = inspect_finalized_evm_swap(
        HASH, asset="BNB", stable="USDT", side="SELL", wallet=WALLET,
        chain_id=hex(pair["chain_id"]), transaction=tx, receipt=receipt,
        latest="0x70", finalized={"number": "0x68"},
        canonical={"number": "0x64", "hash": BLOCK},
    )
    observed = []
    async def trusted(did, tx_hash, *, asset, side, stable):
        observed.append((did, tx_hash, asset, side, stable))
        return proof
    def persist(did, leg):
        observed.append(("saved", did, leg["asset"], leg["tx_hash"]))
        return {"recorded": True, **leg}
    monkeypatch.setattr(manual, "check_user_evm_swap", trusted)
    monkeypatch.setattr(manual, "save_manual_evm_leg", persist)
    result = asyncio.run(manual.record_manual_evm_leg(
        "did:privy:verified", asset="BNB", tx_hash=HASH, side="SELL", stable="USDT"
    ))
    assert result["recorded"]
    assert observed == [
        ("did:privy:verified", HASH, "BNB", "SELL", "USDT"),
        ("saved", "did:privy:verified", "BNB", HASH),
    ]
    with pytest.raises(HTTPException) as exc:
        asyncio.run(manual.record_manual_evm_leg(
            "did:privy:verified", asset="BNB", tx_hash=HASH, side="SELL", stable="USDC"
        ))
    assert exc.value.status_code == 422
