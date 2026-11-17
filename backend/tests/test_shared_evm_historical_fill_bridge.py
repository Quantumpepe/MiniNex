"""A journal row is not by itself a trusted net-profit or historical fill."""
from decimal import Decimal

import pytest

from backend.shared_evm_historical_fill_bridge import join_owner_persisted_evm_fill
from backend.shared_evm_swap_receipts import _pair

WALLET = "0x" + "ab"*20
TX = "0x" + "12"*32


def evidence(asset):
    stable = "USDT" if asset == "BNB" else "USDC"
    pair = _pair(asset, stable, "SELL")
    base = {
        "asset": asset, "side": "SELL", "stable": stable,
        "chain_id": pair["chain_id"], "network": pair["network"],
        "wallet_address": WALLET, "tx_hash": TX,
        "intent_key": "sig_original_0123456789",
        "onchain_finalized": True,
    }
    row = dict(base, spent_amount="1.5",
               received_amount="200.25", network_fee_native="0.00001")
    proof = dict(base, mode="INDEPENDENT_FINALIZED_EVM_EXECUTION",
                 source_amount="1.5", destination_amount="200.25",
                 network_fee_native="0.00001", block_number=1000,
                 block_unix=1800000, transaction_index=2)
    return row, proof


@pytest.mark.parametrize("asset", ["BNB", "POL", "AVAX", "LINK", "UNI"])
def test_owner_fill_is_anchored_to_original_finalized_execution(asset):
    row, proof = evidence(asset)
    result = join_owner_persisted_evm_fill(row,proof)
    assert result["intent_key"] == row["intent_key"]
    assert result["block_unix"] == 1800000
    assert result["external_inventory_reconciled"] is False
    assert result["historic_usd_priced"] is False
    assert result["native_cycle_complete"] is False
    assert result["fee_assessed"] is False


@pytest.mark.parametrize("mutate", [
    lambda r,p: p.update(tx_hash="0x"+"34"*32),
    lambda r,p: p.update(wallet_address="0x"+"ef"*20),
    lambda r,p: p.update(chain_id=56),
    lambda r,p: p.update(source_amount="1.4"),
    lambda r,p: p.update(destination_amount="201"),
    lambda r,p: p.update(network_fee_native="0"),
    lambda r,p: p.update(onchain_finalized=False),
    lambda r,p: p.update(block_number=0),
    lambda r,p: p.update(transaction_index=-1),
    lambda r,p: r.update(onchain_finalized=False),
    lambda r,p: r.update(intent_key="bad"),
])
def test_mismatch_between_persisted_and_reverified_receipt_refuses_pnl(mutate):
    row, proof = evidence("LINK")
    mutate(row, proof)
    with pytest.raises(ValueError):
        join_owner_persisted_evm_fill(row, proof)


def test_original_block_prices_are_fetched_without_trusting_current_quote(monkeypatch):
    import asyncio
    import httpx
    from backend import shared_evm_historical_fill_bridge as bridge

    row, proof = evidence("LINK")
    proof["block_unix"] = 1800000
    async def reread(**kwargs):
        assert kwargs["tx_hash"] == TX
        return proof
    monkeypatch.setattr(bridge, "read_owner_finalized_evm_swap_block", reread)
    requests = []
    async def coingecko(req):
        requests.append(req.url.path)
        assert req.url.host == "api.coingecko.com"
        rate = "1" if "usd-coin" in req.url.path else "2500"
        return httpx.Response(200, json={
            "prices": [[1800000*1000, rate]],
        })
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(coingecko)) as client:
            return await bridge.independently_price_stored_evm_fill(
                did="did:privy:test_account", row=row,client=client,
                now=1800100)
    result = asyncio.run(execute())
    assert result["historic_usd_priced"] is True
    assert result["stable_usd_proof"]["usd_price"] == "1"
    assert result["native_usd_proof"]["usd_price"] == "2500"
    assert result["fee_assessed"] is False
    assert result["external_inventory_reconciled"] is False
    assert len(requests) == 2
