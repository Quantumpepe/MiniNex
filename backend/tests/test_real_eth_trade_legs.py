"""Finalized blockchain legs become owner-bound durable activity, not Paper."""
import asyncio
import pytest
from fastapi import HTTPException
from backend import real_eth_trade_legs as eth

H="0x"+"a"*64
def swap():
    return dict(tx_hash=H,asset="ETH",network="ethereum-mainnet",
        transaction_finalized=True,execution_enabled=False,
        state="FINALIZED_ERC20_SWAP_LEG",
        swap_erc20_leg_verified=True,side="BUY",stable="USDC",
        swap_output_asset="WETH",source_spent_amount="10",
        destination_received_amount="0.02",network_fee_eth="0.0001")

def test_finalized_swap_yields_exact_persistable_fields():
    leg=eth.validated_finalized_leg(tx_hash=H,side="BUY",stable="USDC",
                                    kind="SWAP",receipt=swap())
    assert leg["source_amount"]=="10"
    assert leg["destination_amount"]=="0.02"
    assert leg["tx_hash"]==H

@pytest.mark.parametrize("field,value",[
  ("transaction_finalized",False),("state","PENDING"),
  ("swap_erc20_leg_verified",False),("side","SELL"),
  ("destination_received_amount","NaN"),("network","polygon-pos-mainnet"),
  ("tx_hash","0x"+"b"*64),("execution_enabled",True),
])
def test_bad_proof_never_records(field,value):
    evidence=swap();evidence[field]=value
    with pytest.raises(ValueError):
        eth.validated_finalized_leg(tx_hash=H,side="BUY",stable="USDC",
                                    kind="SWAP",receipt=evidence)

@pytest.mark.parametrize("kind,side,operation",[
    ("WRAP","SELL","DEPOSIT"),("UNWRAP","BUY","WITHDRAW")])
def test_native_weth9_conversion_is_separate_leg(kind,side,operation):
    receipt=dict(tx_hash=H,asset="ETH",network="ethereum-mainnet",
        transaction_finalized=True,execution_enabled=False,
        state="FINALIZED_WETH9_CONVERSION",confirmed_native_conversion=True,
        operation=operation,weth9_event_amount="0.3",
        network_fee_eth="0.00012")
    leg=eth.validated_finalized_leg(tx_hash=H,side=side,stable="USDC",
                                    kind=kind,receipt=receipt)
    assert leg["source_amount"]=="0.3"
    assert leg["kind"]==kind

def test_authenticated_receipt_is_required_before_db_write(monkeypatch):
    calls=[]
    async def fake(did,hash,*,side,stable):
        calls.append((did,hash,side,stable));return swap()
    monkeypatch.setattr(eth,"check_user_router02_receipt",fake)
    monkeypatch.setattr(eth,"save_verified_leg",lambda did,leg:
        {"recorded":True,"did":did,**leg})
    result=asyncio.run(eth.record_real_eth_leg("did:privy:verified",
        tx_hash=H,side="BUY",stable="USDC",kind="SWAP"))
    assert result["recorded"]
    assert calls==[("did:privy:verified",H,"BUY","USDC")]

def test_invalid_stage_rejected_before_chain_call():
    with pytest.raises(HTTPException) as e:
        asyncio.run(eth.record_real_eth_leg("did:privy:verified",
            tx_hash=H,side="SELL",stable="USDC",kind="UNWRAP"))
    assert e.value.status_code==422
