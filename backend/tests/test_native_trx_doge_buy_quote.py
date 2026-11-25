"""Real private THORChain BUY quotes for owner-owned TRX/DOGE wallets.

This covers ETH USDC/USDT source and precise destination-bound calldata, NOT
TRON/DOGE sell signers, fees, a user grant, an order or any live activation.
"""
import asyncio
from datetime import datetime,timezone
from decimal import Decimal
import time

import pytest
from fastapi import HTTPException

from backend import native_thor_swap_quote as q
from backend import native_eth_thor_deposit_tx as e
from backend import native_eth_native_buy_preflight as p
from backend import native_thor_buy_approval_plan as a
from backend.native_coin_balances import _checksum,_valid_address
from backend.native_live_broadcast_transport import _b58
from backend.manual_swap_live_quotes import THOR_ASSETS

DID="did:privy:extended_owner"
ETH="0x"+"a"*40
ROUTER="0x"+"c"*40
VAULT="0x"+"b"*40


def owner_coin(asset):
    version=0x41 if asset=="TRX" else 0x1e
    payload=bytes([version])+bytes.fromhex("ab"*20)
    addr=_b58(payload+_checksum(payload))
    assert _valid_address(asset,addr)
    return addr


def original(asset,now=1803000000,stable="USDC",amount="50"):
    dest=owner_coin(asset)
    raw={
        "inbound_address":VAULT,
        "expiry":now+120,
        "memo":"=:"+THOR_ASSETS[asset][0]+":"+dest+"/"+ETH+":0/1/0",
        "expected_amount_out":"90000000",
        "recommended_min_amount_in":"10000000",
        "fees":{"total":"10000"},
    }
    inbounds=[{
        "chain":"ETH","address":VAULT,"router":ROUTER,
        "halted":False,"global_trading_paused":False,
        "chain_trading_paused":False,
    }]
    report=q.validate_thornode_route_terms(
        asset=asset,side="BUY",stable=stable,amount=amount,
        native_owner=dest,eth_owner=ETH,quote=raw,
        inbounds=inbounds,now=now)
    return report,raw,inbounds


@pytest.mark.parametrize("asset",["TRX"])
def test_private_native_buy_quote_and_calldata_bound_to_original_wallet(asset):
    now=1803000000
    quoted,_,_=original(asset,now=now)
    assert quoted["source_asset"]=="USDC"
    assert quoted["destination_asset"]==asset
    assert quoted["to_network"]==(
        "tron-mainnet" if asset=="TRX" else "dogecoin-mainnet")
    assert quoted["destination_wallet"]==owner_coin(asset)
    assert quoted["inbound_router"]==ROUTER
    assert quoted["execution_enabled"] is False
    unsigned=e.encode_original_eth_native_buy_deposit(
        quoted,asset=asset,stable="USDC",amount="50",now=now)
    assert unsigned["target_network"]==quoted["to_network"]
    assert unsigned["destination_wallet"]==owner_coin(asset)
    assert unsigned["unsigned_transaction"]["to"]==ROUTER
    assert unsigned["source_amount_atomic"]=="50000000"
    assert unsigned["source_transaction_sent"] is False
    assert unsigned["can_submit"] is False


@pytest.mark.parametrize("asset",["TRX"])
def test_extended_first_buy_financing_and_approval_are_unsigned(asset):
    now=1803000000
    quoted,_,_=original(asset,now=now)
    unsigned=e.encode_original_eth_native_buy_deposit(
        quoted,asset=asset,stable="USDC",amount="50",now=now)
    result=a.inspect_original_thor_buy_allowance(
        quote=quoted,unsigned=unsigned,asset=asset,stable="USDC",
        amount="50",now=now,chain_id="0x1",
        token_balance="0x"+f"{100_000_000:064x}",
        token_allowance="0x"+f"{0:064x}",
        eth_balance=hex(10**17))
    assert result["owner_wallet_needs_erc20_approval"] is True
    assert result["required_sequential_approval_receipts"]==1
    assert result["exact_owner_approval_transaction"]["to"]==result[
        "official_stable_token_address"]
    assert result["real_token_approval_sent"] is False
    assert result["can_submit"] is False


@pytest.mark.parametrize("asset",["TRX"])
def test_native_buy_source_preflight_reuses_same_verified_quote(asset):
    from backend.tests.test_native_eth_native_buy_preflight import inspect
    now=1803000000
    quote,_,_=original(asset,now=now)
    unsigned=e.encode_original_eth_native_buy_deposit(
        quote,asset=asset,stable="USDC",amount="50",now=now)
    proof=inspect(unsigned=unsigned,original_quote=quote,now=now)
    assert proof["asset"]==asset
    assert proof["original_router_allowance_verified"] is True
    assert proof["ready_to_sign"] is False
    assert proof["execution_enabled"] is False


@pytest.mark.parametrize("asset",["TRX"])
def test_private_buy_never_accepts_an_external_or_invalid_receiver(asset):
    now=1803000000
    quoted,raw,ins=original(asset,now=now)
    with pytest.raises(ValueError):
        q.validate_thornode_route_terms(
            asset=asset,side="BUY",stable="USDC",amount="50",
            native_owner="not-a-native-wallet",eth_owner=ETH,
            quote=raw,inbounds=ins,now=now)
    with pytest.raises(ValueError):
        q.validate_thornode_route_terms(
            asset=asset,side="SELL",stable="USDC",amount="1",
            native_owner=owner_coin(asset),eth_owner=ETH,
            quote=raw,inbounds=ins,now=now)


@pytest.mark.parametrize("asset",["TRX"])
def test_original_privy_native_wallet_used_as_thor_destination(asset,monkeypatch):
    target=owner_coin(asset)
    user={"id":DID,"linked_accounts":[
        {"type":"wallet","chain_type":"ethereum",
         "address":ETH,"wallet_client_type":"privy"},
        {"type":"wallet","chain_type":(
             "tron" if asset=="TRX" else "dogecoin"),
         "address":target,"wallet_client_type":"privy"},
    ]}
    assert q._exact_owner_wallets(user,DID,asset)==(target,ETH)
    async def fetch(did):return user
    async def evm(did):return {
        "wallet_ownership_verified":True,
        "balances":{"USDC":{"status":"verified","balance":"100"},
                    "USDT":{"status":"verified","balance":"100"}}}
    async def forbidden(*args,**kwargs):
        raise AssertionError("Empty receiving wallet balance is NOT source funding")
    monkeypatch.setattr(q,"fetch_privy_user",fetch)
    monkeypatch.setattr(q,"get_wallet_balances",evm)
    monkeypatch.setattr(q,"get_native_balances",forbidden)
    now=int(time.time())
    _,raw,ins=original(asset,now=now)
    class Response:
        def __init__(self,payload):
            self.payload=payload
            self.content=b"{}"
        def raise_for_status(self):pass
        def json(self):return self.payload
    class Client:
        def __init__(self,**kw):
            assert kw["follow_redirects"] is False
        async def __aenter__(self):return self
        async def __aexit__(self,*args):return False
        async def get(self,url,**kw):
            if url.endswith("/quote/swap"):
                assert kw["params"]["destination"]==target
                assert kw["params"]["refund_address"]==ETH
                return Response(raw)
            return Response(ins)
    monkeypatch.setattr(q.httpx,"AsyncClient",Client)
    result=asyncio.run(q.get_private_native_thornode_terms(
        DID,asset=asset,side="BUY",stable="USDC",amount="50"))
    assert result["destination_wallet"]==target
    assert result["can_submit"] is False
    async def empty_native(did):
        assert did==DID
        return {"wallet_ownership_verified":True,"assets":{
            asset:{"status":"verified","network":"tron-mainnet","balance":"0"}}}
    monkeypatch.setattr(q,"get_native_balances",empty_native)
    with pytest.raises(HTTPException) as err:
        asyncio.run(q.get_private_native_thornode_terms(
            DID,asset=asset,side="SELL",stable="USDT",amount="1"))
    assert err.value.status_code==409


@pytest.mark.parametrize("asset",["TRX"])
def test_extended_original_ethereum_buy_journal_and_privy_body_share_owner(asset):
    from backend import native_live_order_journal as ledger
    from backend import native_eth_buy_privy_dispatch as dispatcher
    from backend.tests.test_native_eth_native_buy_preflight import inspect
    now=1803000000
    terms,_,_=original(asset,now=now)
    intent=ledger.immutable_native_order_identity(
        did=DID,asset=asset,side="BUY",stable="USDC",
        source_wallet=ETH,destination_wallet=owner_coin(asset),
        amount="50",quote=terms,quote_expires_at=terms["quote_expires_at"],
        strategy_run_id="strategy_tron_doge_001",
        signal_event_id="signal_initial_buy_001",
        budget_revision=2,
        owner_consent_sha256="a"*64,privy_grant_sha256="b"*64)
    assert intent["chain"]=="ethereum-mainnet"
    assert intent["destination_chain"]==terms["to_network"]
    assert intent["privy_did"]==DID
    assert len(intent["payload_sha256"])==64
    unsigned=e.encode_original_eth_native_buy_deposit(
        terms,asset=asset,stable="USDC",amount="50",now=now)
    preflight=inspect(unsigned=unsigned,original_quote=terms,now=now)
    url,body,nonce,digest=dispatcher.bound_original_native_buy_rpc(
        {**intent,"status":"PREPARED","input_amount":Decimal("50")},
        terms,preflight,wallet_id="extended_native_wallet_001",now=now)
    assert url=="https://api.privy.io/v1/wallets/extended_native_wallet_001/rpc"
    assert body["caip2"]=="eip155:1"
    assert body["params"]["transaction"]["to"]==ROUTER.lower()
    assert body["params"]["transaction"]["data"]==unsigned[
        "unsigned_transaction"]["data"]
    assert nonce==42 and len(digest)==64
    with pytest.raises(ValueError):
        ledger.immutable_native_order_identity(
            did=DID,asset=asset,side="SELL",stable="USDC",
            source_wallet=owner_coin(asset),destination_wallet=ETH,
            amount="50",quote=terms,quote_expires_at=terms["quote_expires_at"],
            strategy_run_id="strategy_tron_doge_001",
            signal_event_id="signal_initial_sell_001",
            budget_revision=2,
            owner_consent_sha256="a"*64,privy_grant_sha256="b"*64)


@pytest.mark.parametrize("asset",["TRX"])
def test_extended_ethereum_source_nonce_recovery_cannot_fake_native_fill(asset):
    from backend import native_eth_buy_unknown_reconcile as unknown
    from backend import native_live_order_status as history
    from backend import native_eth_buy_privy_dispatch as dispatcher
    from backend.tests.test_native_eth_native_buy_preflight import inspect
    now=1803000000
    terms,_,_=original(asset,now=now)
    unsigned=e.encode_original_eth_native_buy_deposit(
        terms,asset=asset,stable="USDC",amount="50",now=now)
    preflight=inspect(unsigned=unsigned,original_quote=terms,now=now)
    order={
        "asset":asset,"side":"BUY","stable":"USDC",
        "chain":"ethereum-mainnet","status":"PREPARED",
        "source_wallet":ETH,"destination_wallet":owner_coin(asset),
        "input_amount":Decimal("50"),
        "quote_expires_at":terms["quote_expires_at"],
        "immutable_quote_sha256":dispatcher._original_quote_digest(terms),
        "payload_sha256":"c"*64,
    }
    _,_,nonce,digest=dispatcher.bound_original_native_buy_rpc(
        order,terms,preflight,wallet_id="extended_native_wallet_001",now=now)
    recorded={**order,
        "privy_did":DID,"intent_key":"native_"+"a"*64,
        "status":"RECONCILIATION_REQUIRED","source_tx_hash":None,
        "source_eth_pending_nonce":Decimal(nonce),
        "source_rpc_request_sha256":digest,
        "source_eth_observed_block_number":Decimal(100),
        "source_eth_scan_next_block":None}
    tx={"from":ETH,"to":ROUTER.lower(),
        "hash":"0x"+"d"*64,
        "input":unsigned["unsigned_transaction"]["data"],
        "nonce":hex(nonce),"chainId":"0x1","value":"0x0",
        "blockNumber":"0x66","blockHash":"0x"+"e"*64}
    observed=unknown._original_eth_buy_candidate(
        recorded,tx,wallet_id="extended_native_wallet_001",
        block_number=102,block_hash="0x"+"e"*64)
    assert observed=="0x"+"d"*64
    # An Ethereum deposit proves only a source, never a TRX/DOGE receipt.
    pending={**recorded,"status":"SUBMITTED","source_tx_hash":observed,
             "budget_revision":2,"created_at":datetime.now(timezone.utc),
             "updated_at":datetime.now(timezone.utc)}
    progress=history._original_native_order_row(pending,asset)
    assert progress["source_tx_hash"]==observed
    assert progress["destination_wallet_credited"] is False
    assert progress["performance_fee_assessed"] is False
    assert progress["automatic_retry_allowed"] is False


@pytest.mark.parametrize("asset",["TRX"])
def test_trx_doge_buy_source_reconciles_without_synthetic_native_payout(asset):
    from backend import native_live_source_finality as finality
    from backend import native_thor_transaction_status as protocol
    # The Ethereum-funded BUY is eligible for the shared source finalizer,
    # TRX SELL can now reconcile exact independently confirmed sources;
    # DOGE remains buy-only. A BUY receipt cannot be relabeled as SELL.
    assert asset in finality._CHAIN
    assert asset not in finality._BUY_ONLY
    assert asset in protocol._CHAINS
    assert asset not in protocol._BUY_ONLY
    assert "asset IN ('BTC','SOL','XRP','TRX','DOGE')" in (
        __import__("inspect").getsource(finality._pending_original_eth_buy_submissions))
    owner=owner_coin(asset)
    original_row={
        "privy_did":DID,"asset":asset,"side":"BUY",
        "status":"SOURCE_FINALIZED","stable":"USDC",
        "source_wallet":ETH,"destination_wallet":owner,
        "source_tx_hash":"0x"+"e"*64,
        "intent_key":"native_"+"a"*64,
    }
    observed={
        "tx":{
            "id":"E"*64,"chain":"ETH","from_address":ETH,
            "memo":"=:"+THOR_ASSETS[asset][0]+":"+owner+"/"+ETH+":0/1/0",
            "coins":[{"asset":THOR_ASSETS["USDC"][0],"amount":"5000000000"}],
        },
        "stages":{
            "inbound_observed":{"completed":True},
            "inbound_finalised":{"completed":True},
            "swap_finalised":{"completed":True},
        },
        "out_txs":[{
            "id":"F"*64,"chain":("TRON" if asset=="TRX" else "DOGE"),
            "to_address":owner,
            "coins":[{"asset":THOR_ASSETS[asset][0],"amount":"2000000000"}],
        }],
    }
    checked=protocol.inspect_original_thorchain_status(original_row,observed)
    assert checked["protocol_outbound_hashes"]==["f"*64]
    assert checked["destination_owner_bound"] is True
    assert checked["destination_chain_receipts_verified"] is False
    assert checked["fee_assessed"] is False
    assert checked["new_transactions_sent"]==0
    with pytest.raises(ValueError):
        protocol.inspect_original_thorchain_status(
            {**original_row,"side":"SELL"},observed)
    with pytest.raises(ValueError):
        protocol.inspect_original_thorchain_status(
            original_row,{**observed,"out_txs":[{
                **observed["out_txs"][0],"to_address":ETH}]})
