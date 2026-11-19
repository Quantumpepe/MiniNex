"""Privy multi-EVM executor is one-shot, user approved and quote bound."""
import asyncio
from datetime import datetime,timezone
import hashlib
import json
import time

import httpx
import pytest
from fastapi import HTTPException

from backend import shared_evm_privy_order_submit as s
from backend.shared_evm_execution import EvmPair,unsigned_exact_input_single
from backend.shared_evm_privy_user_policy import build_user_evm_policy_blueprint
from backend.shared_evm_swap_receipts import _pair

DID="did:privy:owner"
KEY="sig_abcdef01234567890123456789"
WALLET="0x"+"ab"*20
WALLET_ID="wallet_12345678"
TXHASH="0x"+"cd"*32


def evidence(asset="LINK",side="BUY",amount_atomic=1000000):
    now=int(time.time())
    stable="USDT" if asset=="BNB" else "USDC"
    pair=_pair(asset,stable,side)
    policy=build_user_evm_policy_blueprint(
        asset=asset,stable=stable,wallet=WALLET,
        owner_id="owner_12345678",executor_id="signer_12345678",
        buy_source_atomic_cap="1000000",
        sell_source_atomic_cap="1000000000000000000",
        native_wrap_atomic_cap=(
            "1000000000000000000" if asset in ("BNB","POL","AVAX") else None),
        expires_at=now+900,now=now)
    sha=hashlib.sha256(json.dumps(
        policy,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    record={
        "asset":asset,"stable":stable,"chain_id":pair["chain_id"],
        "wallet_address":WALLET,"wallet_id":WALLET_ID,
        "owner_id":"owner_12345678","signer_id":"signer_12345678",
        "candidate_json":policy,"policy_sha256":sha,
        "expires_at":datetime.fromtimestamp(now+900,timezone.utc),
        "status":"GRANT_VERIFIED","revoked_at":None,
    }
    tx=unsigned_exact_input_single(
        pair=EvmPair(
            asset,pair["chain_id"],pair["network"],
            pair["source_token"],pair["output_token"],pair["router"],500),
        wallet=WALLET,amount_in=amount_atomic,min_out=12345)
    prepared={
        "mode":"SHARED_EVM_OWNER_SWAP_PREPARATION",
        "asset":asset,"chain_id":pair["chain_id"],
        "network":pair["network"],"side":side,"stable":stable,
        "wallet_address":WALLET,"input_amount":"100",
        "source_token_address":pair["source_token"],
        "output_token_address":pair["output_token"],
        "unsigned_transaction":tx,
        "transaction_expires_at":now+40,
        "exact_swap_abi_and_amount_verified":True,
        "wallet_identity_verified":True,
        "source_funds_sufficient":True,
        "router_allowance_sufficient":True,
        "native_gas_available":True,
        "owner_wallet_signing_possible":True,
        "execution_enabled":False,"can_submit":False,
    }
    return record,prepared,stable


class DB:
    def __enter__(self):return self
    def __exit__(self,*a):return False
    def cursor(self,**kwargs):return self


def mocks(monkeypatch,*,asset="LINK",fail_remote=False,expired=False):
    record,prepared,stable=evidence(asset)
    if expired:
        prepared["transaction_expires_at"]=int(time.time())-1
    called=[]
    async def audit(did,a):
        called.append("remote_grant")
        flags={
            "asset":a,"owner_wallet_current":True,
            "exact_remote_policy_rules_verified":not fail_remote,
            "privy_additional_signer_scoped":True,
            "backend_quorum_current":True,
            "signed_coin_budget_intent_in_ledger":True,
        }
        return flags
    async def quote(did,**kwargs):
        called.append("verified_quote")
        return prepared
    def claim(cur,**kw):
        called.append("durable_claim")
        assert kw["identity"]["asset"]==asset
        assert kw["quote_sha256"]==s._fingerprint_prepared_quote(prepared)
    def signer(**kw):
        called.append("sign")
        return "auth_signature_12345678901234567890"
    async def post(**kw):
        called.append("privy_post")
        return TXHASH
    def record_result(**kw):
        called.append(("persist",kw["tx_hash"]))
        return {"state":"SUBMITTED" if kw["tx_hash"] else "RECONCILIATION_REQUIRED"}
    monkeypatch.setattr(s,"get_owner_evm_remote_grant_audit",audit)
    monkeypatch.setattr(s,"_read_grant_record",lambda did,a:record)
    monkeypatch.setattr(s,"prepare_owner_evm_swap",quote)
    monkeypatch.setattr(s,"_claim_authorized_evm_order_locked",claim)
    monkeypatch.setattr(s,"sign_privy_evm_swap_request",signer)
    monkeypatch.setattr(s,"post_claimed_privy_evm_rpc",post)
    monkeypatch.setattr(s,"record_internal_authorized_evm_send_result",record_result)
    monkeypatch.setattr(s,"_db_url",lambda:"postgres://example.invalid")
    monkeypatch.setattr(s,"_ensure_schema",lambda url:None)
    monkeypatch.setattr(s.psycopg,"connect",lambda *a,**kw:DB())
    return called,prepared,stable


async def submit(asset,stable):
    async with httpx.AsyncClient() as client:
        return await s.submit_internal_privy_evm_order(
            did=DID,asset=asset,intent_key=KEY,side="BUY",stable=stable,
            amount="100",budget_revision=5,client=client)


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI","ARB"])
def test_five_coins_only_one_post_after_persisted_claim(monkeypatch,asset):
    called,prepared,stable=mocks(monkeypatch,asset=asset)
    result=asyncio.run(submit(asset,stable))
    assert result["asset"]==asset
    assert result["state"]=="SUBMITTED"
    assert result["onchain_finalized"] is False
    assert result["native_trade_cycle_settled"] is False
    assert called==[
        "remote_grant","verified_quote","durable_claim",
        "sign","privy_post",("persist",TXHASH),
    ]


def test_unverified_privy_remote_grant_prevents_quote_or_rpc(monkeypatch):
    called,prepared,stable=mocks(monkeypatch,fail_remote=True)
    with pytest.raises(HTTPException):
        asyncio.run(submit("LINK",stable))
    assert called==["remote_grant"]


def test_expired_quote_refuses_claim_and_send(monkeypatch):
    called,prepared,stable=mocks(monkeypatch,expired=True)
    with pytest.raises(HTTPException):
        asyncio.run(submit("LINK",stable))
    assert called==["remote_grant","verified_quote"]


def test_ambiguous_rpc_result_persists_unknown_without_retry(monkeypatch):
    called,prepared,stable=mocks(monkeypatch)
    async def timeout(**kwargs):
        called.append("privy_post")
        raise HTTPException(503,"Uncertain Privy response")
    monkeypatch.setattr(s,"post_claimed_privy_evm_rpc",timeout)
    with pytest.raises(HTTPException):
        asyncio.run(submit("LINK",stable))
    assert called==[
        "remote_grant","verified_quote","durable_claim",
        "sign","privy_post",("persist",None),
    ]


def test_invalid_pinned_approval_blocks_transaction_before_send(monkeypatch):
    called,prepared,stable=mocks(monkeypatch)
    prepared["unsigned_transaction"]["from"]="0x"+"ef"*20
    with pytest.raises(HTTPException):
        asyncio.run(submit("LINK",stable))
    assert called==["remote_grant","verified_quote"]


def test_non_public_executor_does_not_run_without_owner_session_claim():
    import inspect
    from backend.app import app
    source=inspect.getsource(s.submit_internal_privy_evm_order)
    assert "_claim_authorized_evm_order_locked" in source
    assert all("submit-internal-privy-evm-order" not in r.path
               for r in app.routes)
