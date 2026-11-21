"""No 5 percent performance fee from current quotes or fake old gas USD."""
import asyncio
import json
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException

from backend.shared_evm_historical_usd import (
    pick_historical_usd_observation,fetch_recent_historical_usd_observation,
)

NOW=1800000
BLOCK=NOW-600
ASSETS=["ETH","BNB","POL","AVAX","LINK","UNI","USDC","USDT"]


@pytest.mark.parametrize("asset",ASSETS)
def test_real_nearest_historical_sample_used_for_price_not_today(asset):
    raw={"prices":[[(BLOCK-240)*1000,2.50],
                   [(BLOCK-45)*1000,2.25],
                   [(BLOCK+120)*1000,2.35]]}
    if asset in ("USDC","USDT"):
        raw={"prices":[[(BLOCK-45)*1000,0.998]]}
    r=pick_historical_usd_observation(
        asset=asset,block_unix=BLOCK,now=NOW,payload=raw)
    assert r["rate_age_seconds"]==45
    assert r["rate_observed_unix"]==BLOCK-45
    assert r["onchain_execution_finality_verified_by_this_rate"] is False
    assert r["fifo_cost_basis_proved_by_this_rate"] is False


@pytest.mark.parametrize("payload",[
    {"prices":[]},
    {"prices":[[(BLOCK-3600)*1000,2.50]]},
    {"prices":[[BLOCK*1000,"NaN"]]},
    {"prices":[[BLOCK*1000,"-1"]]},
    {"prices":[[BLOCK*1000,"0"]]},
    {"prices":[["nonsense",1.0]]},
])
def test_missing_stale_or_forged_historical_reference_cannot_price_fee(payload):
    with pytest.raises(ValueError):
        pick_historical_usd_observation(
            asset="BNB",block_unix=BLOCK,now=NOW,payload=payload)


@pytest.mark.parametrize("rate",["0.75","1.4","0","NaN"])
def test_offpeg_stablecoin_never_valued_at_automatic_usd_rate(rate):
    with pytest.raises(ValueError):
        pick_historical_usd_observation(
            asset="USDT",block_unix=BLOCK,now=NOW,
            payload={"prices":[[BLOCK*1000,rate]]})


def test_historical_query_uses_only_independent_coingecko_provider():
    async def do_get(req):
        assert req.url.scheme=="https"
        assert req.url.host=="api.coingecko.com"
        assert req.url.path=="/api/v3/coins/binancecoin/market_chart/range"
        assert req.url.params["vs_currency"]=="usd"
        assert int(req.url.params["from"])<=BLOCK
        assert int(req.url.params["to"])>=BLOCK
        return httpx.Response(200,json={"prices":[[BLOCK*1000,300.5]]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(do_get)) as client:
            return await fetch_recent_historical_usd_observation(
                asset="BNB",block_unix=BLOCK,client=client,now=NOW)
    result=asyncio.run(run())
    assert Decimal(result["usd_price"])==Decimal("300.5")
    assert result["provider_asset_id"]=="binancecoin"


def test_delayed_receipt_uses_original_execution_window_not_current_spot():
    old=NOW-100000
    async def handle(req):
        assert int(req.url.params["from"])<=old
        assert int(req.url.params["to"])>=old
        assert int(req.url.params["to"])<NOW-90000
        return httpx.Response(200,json={"prices":[[old*1000,300.5]]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await fetch_recent_historical_usd_observation(
                asset="BNB",block_unix=old,client=client,now=NOW)
    report=asyncio.run(run())
    assert Decimal(report["usd_price"])==Decimal("300.5")
    assert report["execution_block_unix"]==old


def test_delayed_receipt_rejects_coarse_historical_prices():
    old=NOW-100000
    with pytest.raises(ValueError):
        pick_historical_usd_observation(
            asset="BNB",block_unix=old,now=NOW,
            payload={"prices":[[(old+3600)*1000,300.5]]})


def test_historical_pricing_rejects_very_old_out_of_scope_data():
    old=NOW-91*86400
    async def run():
        async with httpx.AsyncClient() as client:
            return await fetch_recent_historical_usd_observation(
                asset="BNB",block_unix=old,client=client,now=NOW)
    with pytest.raises(ValueError):
        asyncio.run(run())

def test_historical_lookup_uses_paid_key_after_basic_or_analyst_upgrade(monkeypatch):
    monkeypatch.setenv("MININEX_COINGECKO_PRO_API_KEY", "existing-nexus-plan-key")
    async def handle(req):
        assert req.url.host == "pro-api.coingecko.com"
        assert req.headers["x-cg-pro-api-key"] == "existing-nexus-plan-key"
        assert "existing-nexus-plan-key" not in str(req.url)
        return httpx.Response(200, json={"prices":[[BLOCK * 1000, 300.5]]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await fetch_recent_historical_usd_observation(
                asset="BNB", block_unix=BLOCK, client=client, now=NOW)
    assert Decimal(asyncio.run(run())["usd_price"]) == Decimal("300.5")
