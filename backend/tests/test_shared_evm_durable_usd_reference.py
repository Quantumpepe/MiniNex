"""A historical BUY price remains exact after market-chart expiry."""
import asyncio
from decimal import Decimal

import httpx
import pytest

from backend import shared_evm_durable_usd_reference as durable
from backend.storage import SCHEMA
from backend.shared_evm_swap_receipts import _pair

TX="0x"+"aa"*32
BLOCK_HASH="0x"+"bb"*32


def original(asset="LINK",stable="USDC",side="BUY",time=1800000):
    pair=_pair(asset,stable,side)
    return dict(
        mode="INDEPENDENT_FINALIZED_EVM_EXECUTION",
        onchain_finalized=True,asset=asset,stable=stable,side=side,
        chain_id=pair["chain_id"],network=pair["network"],
        tx_hash=TX,block_hash=BLOCK_HASH,
        block_number=123456,block_unix=time)


def row(proof, quoted, price):
    return dict(
        asset=proof["asset"],chain_id=proof["chain_id"],
        tx_hash=TX,quoted_asset=quoted,
        block_number=proof["block_number"],block_hash=BLOCK_HASH,
        block_unix=proof["block_unix"],
        observed_unix=proof["block_unix"]+12,
        usd_price=Decimal(price),provider="COINGECKO_MARKET_CHART_RANGE")


@pytest.mark.parametrize("asset,quoted,price",[
    ("LINK","USDC","0.9997"),("LINK","ETH","2800.25"),
    ("UNI","USDT","1.002"),("BNB","BNB","650.2"),
    ("ARB","USDC","1.0001"),("ARB","ETH","2950.10"),
])
def test_persisted_quoted_price_bound_to_actual_original_block(asset,quoted,price):
    proof=original(asset,stable=("USDT" if asset=="BNB" or quoted=="USDT" else "USDC"))
    value=durable.validate_bound_historical_price(
        proof=proof,row=row(proof,quoted,price),quoted_asset=quoted)
    assert Decimal(value["usd_price"])==Decimal(price)
    assert value["durably_recorded_on_finalized_owner_trade"] is True
    assert value["fifo_cost_basis_proved_by_this_rate"] is False


@pytest.mark.parametrize("damage",[
    lambda d:d.update(chain_id=137),
    lambda d:d.update(block_hash="0x"+"ff"*32),
    lambda d:d.update(block_unix=1806000),
    lambda d:d.update(observed_unix=1810000),
    lambda d:d.update(usd_price=Decimal("-1")),
    lambda d:d.update(usd_price=Decimal("NaN")),
    lambda d:d.update(quoted_asset="USDT"),
    lambda d:d.update(provider="MOCK_QUOTES"),
])
def test_mined_block_identity_and_historical_prices_are_immutable(damage):
    proof=original()
    data=row(proof,"USDC","1")
    damage(data)
    with pytest.raises(ValueError):
        durable.validate_bound_historical_price(
            proof=proof,row=data,quoted_asset="USDC")


def test_cached_original_usd_reference_used_after_recent_api_window(monkeypatch):
    proof=original()
    existing=durable.validate_bound_historical_price(
        proof=proof,row=row(proof,"USDC","0.9999"),quoted_asset="USDC")
    def cached(did,p,quoted):
        assert p is proof and quoted=="USDC"
        return existing
    monkeypatch.setattr(durable,"_read_immutable_price",cached)
    async def run():
        async with httpx.AsyncClient() as client:
            return await durable.historical_usd_for_verified_fill(
                did="did:privy:account12345",proof=proof,quoted_asset="USDC",
                client=client,now=proof["block_unix"]+86400*50)
    assert asyncio.run(run())==existing


def test_never_silently_use_current_quote_for_old_missing_buy(monkeypatch):
    monkeypatch.setattr(durable,"_read_immutable_price",
                        lambda *_:None)
    proof=original()
    async def run():
        async with httpx.AsyncClient() as client:
            return await durable.historical_usd_for_verified_fill(
                did="did:privy:account12345",proof=proof,quoted_asset="USDC",
                client=client,now=proof["block_unix"]+86400*20)
    with pytest.raises(ValueError):
        asyncio.run(run())


def test_new_historic_rate_schema_is_immutable_owner_and_tx_bound():
    schema="\n".join(SCHEMA)
    assert "CREATE TABLE IF NOT EXISTS mininex_evm_immutable_fill_usd_prices" in schema
    assert "PRIMARY KEY(privy_did,asset,tx_hash,quoted_asset)" in schema
    assert "REFERENCES mininex_live_evm_verified_fills(chain_id,tx_hash)" in schema
