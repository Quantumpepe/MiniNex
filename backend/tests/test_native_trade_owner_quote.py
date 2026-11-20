"""Native BTC/SOL/XRP LIVE preparation: real owner balances, no fake sends."""
import asyncio
import pytest
from fastapi import HTTPException

from backend import native_trade_owner_quote as quote

DID="did:privy:native_owner_123"


@pytest.fixture
def sources(monkeypatch):
    sent=[]
    async def native(did):
        assert did==DID
        return {
            "mode":"NATIVE_MAINNET_BALANCES_READ_ONLY",
            "wallet_ownership_verified":True,
            "assets":{
                "SOL":{"network":"solana-mainnet","status":"verified","balance":"4.75"},
                "XRP":{"network":"xrpl-mainnet","status":"verified","balance":"150"},
            },
        }
    async def settlement(did):
        assert did==DID
        return {
            "wallet_ownership_verified":True,
            "wallets":{"ETH":"0x"+"c"*40},
            "balances":{
                "USDC":{"status":"verified","balance":"500"},
                "USDT":{"status":"verified","balance":"200"},
            }
        }
    async def venue(source,target,amount):
        sent.append((source,target,amount))
        return {"mode":"NATIVE_MULTICHAIN_REAL_MARKET_QUOTE_ONLY",
                "source_asset":source,"destination_asset":target,
                "input_amount":amount,"estimated_output_amount":"2.5000",
                "quote_checked_at":1805000000,"quote_expires_at":1805000030,
                "quote_provider":"THORCHAIN_PUBLIC_MCP",
                "execution_enabled":False,"can_submit":False}
    monkeypatch.setattr(quote,"get_native_balances",native)
    monkeypatch.setattr(quote,"get_wallet_balances",settlement)
    monkeypatch.setattr(quote,"get_native_swap_quote",venue)
    return sent


@pytest.mark.parametrize("asset,side,stable,amount",[
    ("SOL","SELL","USDC","1.25"),
    ("SOL","BUY","USDT","110"),
    ("XRP","SELL","USDC","30"),
    ("XRP","BUY","USDC","75"),
])
def test_owner_bound_live_quote_verifies_actual_source_and_stable_wallet(
    sources,asset,side,stable,amount,
):
    result=asyncio.run(quote.quote_owner_native_trade(
        DID,asset=asset,side=side,stable=stable,amount=amount))
    source=stable if side=="BUY" else asset
    target=asset if side=="BUY" else stable
    assert sources==[(source,target,amount)]
    assert result["native_wallet_balance_verified"] is True
    assert result["ethereum_settlement_wallet_verified"] is True
    assert result["source_chain_balance_verified"] is True
    assert result["estimated_output_amount"]=="2.5000"
    assert result["source_asset"]==source
    assert result["destination_asset"]==target
    assert result["signed_source_transaction_available"] is False
    assert result["destination_settlement_finalized"] is False
    assert result["network_fee_and_xrpl_reserve_spendability_verified"] is False
    assert result["funds_transferred"] is False
    assert result["can_submit"] is False


def test_xrp_ticker_does_not_authorize_wrong_network_stablecoin(sources):
    with pytest.raises(HTTPException) as err:
        asyncio.run(quote.quote_owner_native_trade(
            DID,asset="XRP",side="BUY",stable="USDT",amount="10"))
    assert err.value.status_code==422 and sources==[]


def test_missing_sol_wallet_cannot_use_eth_funds_as_sol(monkeypatch,sources):
    async def wallet(_):
        return {"assets":{"SOL":{"network":"solana-mainnet","status":"wallet_missing","balance":None}}}
    monkeypatch.setattr(quote,"get_native_balances",wallet)
    with pytest.raises(HTTPException) as err:
        asyncio.run(quote.quote_owner_native_trade(
            DID,asset="SOL",side="SELL",stable="USDC",amount="0.2"))
    assert err.value.status_code==409 and sources==[]


def test_xrp_sell_cannot_overspend_confirmed_balance(sources):
    with pytest.raises(HTTPException) as err:
        asyncio.run(quote.quote_owner_native_trade(
            DID,asset="XRP",side="SELL",stable="USDC",amount="151"))
    assert err.value.status_code==409 and sources==[]


def test_sol_buy_cannot_use_btc_or_unverified_stablecoins(monkeypatch,sources):
    async def settlement(_):
        return {"wallet_ownership_verified":True,"wallets":{"ETH":"0x"+"c"*40},
                "balances":{"USDC":{"status":"unavailable","balance":None}}}
    monkeypatch.setattr(quote,"get_wallet_balances",settlement)
    with pytest.raises(HTTPException) as err:
        asyncio.run(quote.quote_owner_native_trade(
            DID,asset="SOL",side="BUY",stable="USDC",amount="1"))
    assert err.value.status_code==409 and sources==[]


@pytest.mark.parametrize("amount",["0","-1","1e8","NaN","Infinity","",None,True])
def test_bad_native_amount_never_reaches_venue(sources,amount):
    with pytest.raises(HTTPException) as err:
        asyncio.run(quote.quote_owner_native_trade(
            DID,asset="XRP",side="SELL",stable="USDC",amount=amount))
    assert err.value.status_code==422 and sources==[]


@pytest.mark.parametrize("change",[
    {"can_submit":True},
    {"execution_enabled":True},
    {"source_asset":"USDC"},
    {"destination_asset":"BTC"},
    {"mode":"EXECUTED"},
])
def test_native_provider_cannot_forge_filled_order(monkeypatch,sources,change):
    async def forged(source,target,amount):
        row={"mode":"NATIVE_MULTICHAIN_REAL_MARKET_QUOTE_ONLY",
             "source_asset":source,"destination_asset":target,
             "input_amount":amount,"estimated_output_amount":"3",
             "execution_enabled":False,"can_submit":False}
        row.update(change)
        return row
    monkeypatch.setattr(quote,"get_native_swap_quote",forged)
    with pytest.raises(HTTPException) as err:
        asyncio.run(quote.quote_owner_native_trade(
            DID,asset="SOL",side="SELL",stable="USDC",amount="1"))
    assert err.value.status_code==502 and sources==[]
