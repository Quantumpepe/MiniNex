"""Actual WETH9 native-cycle state transitions do not replay wallet sends."""
import asyncio
import pytest
from fastapi import HTTPException
from backend import eth_auto_native_cycle as n

DID="did:privy:live-user"
SOURCE="sig_abcdef0123456789012345"
HASH="0x"+"a"*64

def test_native_dedup_key_per_owner_and_operation():
    a=n.native_intent_key(DID,SOURCE,"WITHDRAW")
    assert a.startswith("native_")
    assert len(a)==63
    assert a==n.native_intent_key(DID,SOURCE,"WITHDRAW")
    assert a!=n.native_intent_key(DID,SOURCE,"DEPOSIT")

@pytest.mark.parametrize("amount,expected",[
 ("0.1",10**17),("0.000000000000000001",1),("1",10**18),
])
def test_exact_native_wei(amount,expected):
    assert n._atomic_wei(amount)==expected

@pytest.mark.parametrize("amount",["0","-1","NaN","0.0000000000000000001"])
def test_invalid_native_quantity(amount):
    with pytest.raises(ValueError):n._atomic_wei(amount)

def test_finalize_buy_weth_to_real_eth(monkeypatch):
    sent=[]
    async def receipt(did,hash,*,operation):
        assert operation=="WITHDRAW"
        return {"state":"FINALIZED_WETH9_CONVERSION",
                "transaction_finalized":True}
    def validated(**kwargs):
        return {"source_amount":"0.2","tx_hash":HASH}
    monkeypatch.setattr(n,"check_user_weth9_receipt",receipt)
    monkeypatch.setattr(n,"validated_finalized_leg",validated)
    monkeypatch.setattr(n,"save_verified_leg",
                        lambda did,leg:sent.append("record"))
    monkeypatch.setattr(n,"_finalize_native_record",
                        lambda *args:sent.append("confirmed"))
    r=asyncio.run(n.finalize_native_step({
        "privy_did":DID,"source_key":SOURCE,
        "operation":"WITHDRAW","stable":"USDC",
        "amount_wei":2*10**17,"tx_hash":HASH}))
    assert r=="CONFIRMED"
    assert sent==["record","confirmed"]

def test_included_native_is_not_posted_as_finished(monkeypatch):
    async def receipt(*args,**kwargs):
        return {"state":"INCLUDED_SUCCESS","transaction_finalized":False}
    monkeypatch.setattr(n,"check_user_weth9_receipt",receipt)
    monkeypatch.setattr(n,"save_verified_leg",
                        lambda *args:pytest.fail("not finalized"))
    r=asyncio.run(n.finalize_native_step({
        "privy_did":DID,"source_key":SOURCE,
        "operation":"WITHDRAW","stable":"USDT",
        "amount_wei":10**17,"tx_hash":HASH}))
    assert r=="PENDING"

def test_reverted_native_does_not_credit_eth(monkeypatch):
    async def receipt(*args,**kwargs):
        return {"state":"FINALIZED_REVERTED","transaction_finalized":True}
    monkeypatch.setattr(n,"check_user_weth9_receipt",receipt)
    monkeypatch.setattr(n,"save_verified_leg",
                        lambda *args:pytest.fail("no fill after revert"))
    finals=[]
    monkeypatch.setattr(n,"_finalize_native_record",
                        lambda *args:finals.append(args[-1]))
    r=asyncio.run(n.finalize_native_step({
        "privy_did":DID,"source_key":SOURCE,
        "operation":"DEPOSIT","stable":"USDC",
        "amount_wei":10**17,"tx_hash":HASH}))
    assert r=="REVERTED" and finals==["REVERTED"]
