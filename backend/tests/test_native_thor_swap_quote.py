"""Destination-bound real THORNode route evidence for three native coins."""
import asyncio
from copy import deepcopy
import time

import pytest
from fastapi import HTTPException

from backend import native_thor_swap_quote as m
from backend.manual_swap_live_quotes import THOR_ASSETS

DID="did:privy:native_owner"
ETH="0x"+"a"*40
BTC="bc1q"+"q"*38
SOL="7"*44
XRP="r"+"a"*33


def route(asset,side="SELL",stable="USDC",amount="1",now=1803000000):
    native={"BTC":BTC,"SOL":SOL,"XRP":XRP}[asset]
    src=stable if side=="BUY" else asset
    dst=asset if side=="BUY" else stable
    target=native if side=="BUY" else ETH
    quote={
        "inbound_address":"0x"+"b"*40 if side=="BUY" else native,
        "expiry":now+120,
        "memo":"=:"+THOR_ASSETS[dst][0]+":"+target+"/"+(ETH if side=="BUY" else native)+":0/1/0",
        "expected_amount_out":"123456700",
        "recommended_min_amount_in":"10000000",
        "fees":{"total":"12345","outbound":"10000","liquidity":"2345"},
    }
    chain="ETH" if side=="BUY" else asset
    inbound={
        "chain":chain,"address":quote["inbound_address"],
        "halted":False,"global_trading_paused":False,
        "chain_trading_paused":False,
        **({"router":"0x"+"c"*40} if side=="BUY" else {}),
    }
    return quote,[inbound],native,target


@pytest.mark.parametrize("asset",["BTC","SOL","XRP"])
def test_destination_bound_native_sell_uses_actual_owner_and_active_vault(asset):
    now=1803000000
    quote,inbounds,native,target=route(asset,now=now)
    result=m.validate_thornode_route_terms(
        asset=asset,side="SELL",stable="USDC",amount="1",
        native_owner=native,eth_owner=ETH,
        quote=quote,inbounds=inbounds,now=now)
    assert result["mode"]=="THORCHAIN_DESTINATION_BOUND_READ_ONLY"
    assert result["source_asset"]==asset
    assert result["destination_asset"]=="USDC"
    assert result["destination_wallet"]==ETH
    assert result["original_vault_verified"] is True
    assert result["required_chain_memo_verified"] is True
    assert result["transaction_encoding_verified"] is False
    assert result["privy_native_signer_verified"] is False
    assert result["source_transaction_sent"] is False
    assert result["can_submit"] is False
    assert result["refund_path_verified"] is True


@pytest.mark.parametrize("asset",["BTC","SOL","XRP"])
def test_destination_bound_native_buy_identifies_eth_erc20_router_requirement(asset):
    now=1803000000
    quote,inbounds,native,target=route(asset,side="BUY",now=now)
    result=m.validate_thornode_route_terms(
        asset=asset,side="BUY",stable="USDC",amount="1",
        native_owner=native,eth_owner=ETH,
        quote=quote,inbounds=inbounds,now=now)
    assert result["source_asset"]=="USDC"
    assert result["destination_asset"]==asset
    assert result["source_wallet"]==ETH
    assert result["destination_wallet"]==native
    assert result["needs_erc20_router"] is True
    assert result["inbound_router"]=="0x"+"c"*40
    assert result["refund_path_verified"] is True
    assert result["execution_enabled"] is False


@pytest.mark.parametrize("mutation",[
    lambda q,ins:q.update(inbound_address="0x"+"0"*40),
    lambda q,ins:ins[0].update(chain="DOGE"),
    lambda q,ins:ins[0].update(halted=True),
    lambda q,ins:ins[0].update(global_trading_paused=True),
    lambda q,ins:ins[0].update(chain_trading_paused=True),
    lambda q,ins:q.update(memo="=:BTC.BTC:bc1qrqbad:0/1/0"),
    lambda q,ins:q.update(memo=q["memo"].replace(ETH,"0x"+"b"*40)),
    lambda q,ins:q.update(memo=q["memo"].replace("/"+SOL,"/xrp_fake_source")),
    lambda q,ins:q.update(memo=q["memo"].replace("/"+SOL,"")),
    lambda q,ins:q.update(expiry=1803000000),
    lambda q,ins:q.update(expiry=1803001000),
    lambda q,ins:q.update(expected_amount_out="0"),
    lambda q,ins:q.update(recommended_min_amount_in="200000000"),
    lambda q,ins:q.update(fees={"total":"NaN"}),
])
def test_stale_forged_or_paused_vault_quote_never_becomes_order(mutation):
    now=1803000000
    quote,inbounds,native,target=route("SOL",now=now)
    mutation(quote,inbounds)
    with pytest.raises(ValueError):
        m.validate_thornode_route_terms(
            asset="SOL",side="SELL",stable="USDC",amount="1",
            native_owner=native,eth_owner=ETH,
            quote=quote,inbounds=inbounds,now=now)


def test_buy_without_real_ethereum_router_is_not_an_executable_quote():
    now=1803000000
    quote,inbounds,native,target=route("BTC",side="BUY",now=now)
    inbounds[0].pop("router")
    with pytest.raises(ValueError):
        m.validate_thornode_route_terms(
            asset="BTC",side="BUY",stable="USDC",amount="1",
            native_owner=native,eth_owner=ETH,
            quote=quote,inbounds=inbounds,now=now)


def test_xrp_does_not_accept_wrong_eth_settlement_stablecoin():
    now=1803000000
    quote,inbounds,native,target=route("XRP",stable="USDT",now=now)
    with pytest.raises(ValueError):
        m.validate_thornode_route_terms(
            asset="XRP",side="SELL",stable="USDT",amount="1",
            native_owner=native,eth_owner=ETH,
            quote=quote,inbounds=inbounds,now=now)


def test_btc_memo_longer_than_standard_op_return_is_not_marked_encoded():
    now=1803000000
    quote,inbounds,native,target=route("BTC",now=now)
    quote["memo"]+=":"+"x"*90
    result=m.validate_thornode_route_terms(
        asset="BTC",side="SELL",stable="USDC",amount="1",
        native_owner=native,eth_owner=ETH,
        quote=quote,inbounds=inbounds,now=now)
    assert result["needs_extended_btc_memo"] is True
    assert result["transaction_encoding_verified"] is False


def test_owner_rpc_preflight_rejects_spending_unverified_native_coin():
    with pytest.raises(ValueError):
        m._read_owner_balance(
            "SOL","SELL","USDC","1",
            {"wallet_ownership_verified":True},
            {"wallet_ownership_verified":True,
             "assets":{"SOL":{"status":"unavailable",
                              "network":"solana-mainnet","balance":None}}})


def test_privy_btc_external_wallet_never_counts_as_owned_source():
    user={"id":DID,"linked_accounts":[
        {"type":"wallet","chain_type":"ethereum",
         "address":ETH,"wallet_client_type":"privy"},
        {"type":"wallet","chain_type":"bitcoin-segwit",
         "address":BTC,"wallet_client_type":"external"},
    ]}
    with pytest.raises(ValueError):
        m._exact_owner_wallets(user,DID,"BTC")


def test_private_thornode_quote_checks_real_owner_destination_before_rpc(monkeypatch):
    now=int(time.time())
    native=BTC
    quote={
        "inbound_address":BTC,
        "expiry":now+120,
        "memo":"=:"+THOR_ASSETS["USDC"][0]+":"+ETH+"/"+BTC+":0/1/0",
        "expected_amount_out":"120000000",
        "recommended_min_amount_in":"10000000",
        "fees":{"total":"10000"},
    }
    vaults=[{"chain":"BTC","address":BTC,"halted":False,
             "global_trading_paused":False,"chain_trading_paused":False}]
    wallet={"id":DID,"linked_accounts":[
        {"type":"wallet","chain_type":"bitcoin-segwit",
         "address":BTC,"wallet_client_type":"privy"},
        {"type":"wallet","chain_type":"ethereum",
         "address":ETH,"wallet_client_type":"privy"},
    ]}
    async def user(did):
        assert did==DID
        return wallet
    async def evm(did):
        return {"wallet_ownership_verified":True,
                "balances":{"BTC":{"status":"verified","balance":"2"},
                            "USDC":{"status":"verified","balance":"100"}}}
    monkeypatch.setattr(m,"fetch_privy_user",user)
    monkeypatch.setattr(m,"get_wallet_balances",evm)
    calls=[]
    class Response:
        def __init__(self,obj):
            self.obj=obj
            self.content=b"{}"
        def raise_for_status(self):
            return None
        def json(self):
            return self.obj
    class Client:
        def __init__(self,**kwargs):
            assert kwargs["follow_redirects"] is False
        async def __aenter__(self):return self
        async def __aexit__(self,*args):return False
        async def get(self,url,**kwargs):
            calls.append((url,kwargs))
            if url.endswith("/quote/swap"):
                return Response(quote)
            return Response(vaults)
    monkeypatch.setattr(m.httpx,"AsyncClient",Client)
    result=asyncio.run(m.get_private_native_thornode_terms(
        DID,asset="BTC",side="SELL",stable="USDC",amount="1"))
    assert result["original_vault_verified"] is True
    assert result["can_submit"] is False
    assert len(calls)==2
    assert calls[0][1]["params"]["destination"]==ETH
    assert calls[0][1]["params"]["refund_address"]==BTC
    assert calls[0][1]["params"]["from_asset"]=="BTC.BTC"
    assert calls[0][1]["params"]["amount"]=="100000000"
    assert calls[1][0].endswith("/inbound_addresses")


def test_eth_buy_unexpected_router_address_never_claims_prepared_transaction():
    now=1803000000
    quote,inbounds,native,target=route("SOL",side="BUY",now=now)
    inbounds[0]["router"]="unknown_router"
    with pytest.raises(ValueError):
        m.validate_thornode_route_terms(
            asset="SOL",side="BUY",stable="USDC",amount="1",
            native_owner=native,eth_owner=ETH,
            quote=quote,inbounds=inbounds,now=now)


def test_case_sensitive_native_vault_must_match_original_quote():
    now=1803000000
    quote,inbounds,native,target=route("SOL",now=now)
    quote["inbound_address"]="a"+"7"*43
    inbounds[0]["address"]="A"+"7"*43
    with pytest.raises(ValueError):
        m.validate_thornode_route_terms(asset="SOL",side="SELL",stable="USDC",
            amount="1",native_owner=native,eth_owner=ETH,
            quote=quote,inbounds=inbounds,now=now)


def test_tron_sell_route_binds_original_wallet_and_active_tron_vault():
    from backend.native_trx_user_signed_send import _tron_address_from_public_key
    from eth_keys import keys
    native=_tron_address_from_public_key(keys.PrivateKey(bytes([1])*32).public_key.to_bytes())
    vault=_tron_address_from_public_key(keys.PrivateKey(bytes([2])*32).public_key.to_bytes())
    now=1803000000
    q,ins,_,_=route("SOL",stable="USDT",now=now)
    q["inbound_address"]=vault
    q["memo"]="=:"+THOR_ASSETS["USDT"][0]+":"+ETH+"/"+native+":0/1/0"
    ins[0].update(chain="TRON",address=vault)
    args=dict(asset="TRX",side="SELL",stable="USDT",amount="1",
              native_owner=native,eth_owner=ETH,quote=q,inbounds=ins,now=now)
    result=m.validate_thornode_route_terms(**args)
    assert result["from_network"]=="tron-mainnet"
    assert result["to_network"]=="ethereum-mainnet"
    assert result["source_wallet"]==native
    assert result["inbound_vault"]==vault
    assert result["inbound_router"] is None
    assert result["can_submit"] is False
    ins[0]["router"]=vault
    with pytest.raises(ValueError):
        m.validate_thornode_route_terms(**args)
