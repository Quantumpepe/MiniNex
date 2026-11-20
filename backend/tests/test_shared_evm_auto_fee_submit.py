"""Real Privy fees cannot broadcast without user's specific remote grant."""
import asyncio
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException

from backend import shared_evm_auto_fee_submit as autosend

DID="did:privy:account_12345678"
KEY="sig_123456789abc"
TX="0x"+"dd"*32
WALLET="0x"+"ab"*20
TREASURY="0x"+"cd"*20


def setup(monkeypatch,valid=True,fail_post=False):
    calls=[]
    monkeypatch.setattr(
        autosend,"automatic_fee_transfer_worker_enabled",lambda:True)
    async def grant(**kw):
        calls.append("remote-get")
        return {
            "asset":"LINK","stable":"USDC",
            "treasury_wallet":TREASURY,
            "owner_fee_grant_remotely_verified":valid,
            "signer_quorum_remotely_verified":valid,
        }
    monkeypatch.setattr(
        autosend,"get_effective_owner_fee_grant",grant)
    monkeypatch.setattr(autosend,"owner_fee_treasury",lambda **kw:TREASURY)
    async def prices(*a,**kw):
        return {}
    monkeypatch.setattr(autosend,"fetch_evm_policy_reference_rates",prices)
    monkeypatch.setattr(autosend,"_price",lambda *_:Decimal("1"))
    def original(**kw):
        calls.append("sql-claim")
        assert kw["treasury"]==TREASURY
        return {
            "url":"https://api.privy.io/v1/wallets/wallet_12345678/rpc",
            "body":{"method":"eth_sendTransaction",
                    "caip2":"eip155:1",
                    "params":{"transaction":{
                        "to":"0x"+"ef"*20,
                        "value":"0","data":"0xa9059cbb"}}},
            "idempotency_key":"fee_"+"1"*60,
            "request_digest":"a"*64,
        }
    monkeypatch.setattr(autosend,"claim_one_owner_authorized_fee",original)
    def sign(**kw):
        calls.append("sign")
        return "signature"
    monkeypatch.setattr(autosend,"sign_privy_rpc_payload",sign)
    async def post(**kw):
        calls.append("POST")
        if fail_post:
            raise HTTPException(503,"Unknown wallet RPC")
        return TX
    monkeypatch.setattr(autosend,"post_claimed_privy_evm_rpc",post)
    def saved(**kw):
        calls.append(("record",kw["tx_hash"]))
        return {"state":"SUBMITTED" if kw["tx_hash"] else
                "RECONCILIATION_REQUIRED"}
    monkeypatch.setattr(autosend,"record_original_auto_fee_send",saved)
    return calls


def execute():
    async def run():
        async with httpx.AsyncClient() as client:
            return await autosend.submit_original_owner_auto_fee_once(
                did=DID,asset="LINK",execution_id=KEY,client=client)
    return asyncio.run(run())


def test_exact_per_wallet_fee_grant_makes_one_privy_transaction(monkeypatch):
    calls=setup(monkeypatch)
    result=execute()
    assert calls==[
        "remote-get","sql-claim","sign","POST",("record",TX)]
    assert result["state"]=="SUBMITTED"
    assert result["onchain_finalized"] is False
    assert result["automatic_retry_allowed"] is False


def test_unapproved_fee_owner_never_claims_or_posts(monkeypatch):
    calls=setup(monkeypatch,valid=False)
    with pytest.raises(HTTPException):
        execute()
    assert calls==["remote-get"]


def test_timeout_never_posts_fee_a_second_time(monkeypatch):
    calls=setup(monkeypatch,fail_post=True)
    with pytest.raises(HTTPException):
        execute()
    assert calls==[
        "remote-get","sql-claim","sign","POST",("record",None)]


def test_feature_flag_disabled_never_fetches_user_wallet(monkeypatch):
    calls=setup(monkeypatch)
    monkeypatch.setattr(
        autosend,"automatic_fee_transfer_worker_enabled",lambda:False)
    with pytest.raises(HTTPException):
        execute()
    assert calls==[]
