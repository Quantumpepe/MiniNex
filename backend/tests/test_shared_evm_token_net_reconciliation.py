"""Real token PnL coordinator must use full original DB owner history."""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from backend import shared_evm_token_net_reconciliation as token_net
from backend.shared_evm_swap_receipts import _pair

WALLET="0x"+"ab"*20
TX_BUY="0x"+"11"*32
TX_SELL="0x"+"22"*32


def proof(side, tx, block, amount, received, asset="LINK"):
    stable="USDC"
    pair=_pair(asset,stable,side)
    timestamp=1800000+block*10
    def rate(symbol,usd):
        return dict(
            mode="HISTORICAL_EXECUTION_USD_REFERENCE_ONLY",
            provider="COINGECKO_MARKET_CHART_RANGE",
            asset=symbol,execution_block_unix=timestamp,
            rate_observed_unix=timestamp,usd_price=str(usd))
    return dict(
        mode="INDEPENDENT_FINALIZED_EVM_EXECUTION",
        asset=asset,side=side,stable=stable,
        chain_id=pair["chain_id"],network=pair["network"],
        tx_hash=tx,wallet_address=WALLET,
        intent_key="sig_"+tx[2:36],
        block_number=block,transaction_index=1,block_unix=timestamp,
        source_amount=str(amount),destination_amount=str(received),
        network_fee_native="0.0001",
        stable_usd_proof=rate(stable,"1"),native_usd_proof=rate("ETH","2000"),
        native_conversion=None,native_cycle_complete=True,
        onchain_finalized=True,historic_usd_priced=True)


def audit():
    return dict(
        mode="INDEPENDENT_ONCHAIN_ASSET_TRANSFER_AUDIT",
        start_block=100,end_block=200,
        matched_mininex_swap_transfers=2,
        zero_initial_asset_balance_verified=True,
        all_asset_movements_match_verified_orders=True,
        external_inventory_reconciled=True,
        no_other_execution_costs_verified=False,
    )


@pytest.mark.parametrize("asset", ["LINK", "UNI", "ARB"])
def test_full_minined_token_history_real_net_without_auto_fee(monkeypatch,asset):
    rows=[
        dict(tx_hash=TX_SELL,wallet_address=WALLET),
        dict(tx_hash=TX_BUY,wallet_address=WALLET),
    ]
    by_tx={
        TX_BUY:proof("BUY",TX_BUY,100,"100","2",asset=asset),
        TX_SELL:proof("SELL",TX_SELL,200,"1","75",asset=asset),
    }
    monkeypatch.setattr(token_net, "_load_all_verified_owner_token_fills",
                        lambda did,coin:rows if coin==asset else [])
    async def priced(**kw):
        return by_tx[kw["row"]["tx_hash"]]
    monkeypatch.setattr(token_net,"independently_price_stored_evm_fill",priced)
    async def check_audit(**kw):
        assert [x["tx_hash"] for x in kw["fills"]]==[TX_BUY,TX_SELL]
        return audit()
    monkeypatch.setattr(token_net,"audit_owner_erc20_token_inventory",check_audit)
    async def run():
        async with httpx.AsyncClient() as client:
            return await token_net.read_owner_erc20_verified_realized_net(
                did="did:privy:account_12345",asset=asset,client=client,now=1805000)
    result=asyncio.run(run())
    assert result["closed_trades"][0]["intent_key"]==by_tx[TX_SELL]["intent_key"]
    assert Decimal(result["closed_trades"][0]["realized_net_usd"])==Decimal("24.7")
    assert result["hwm_updated"] is False
    assert result["fee_assessed"] is False
    assert result["auto_fee_transfer_enabled"] is False


@pytest.mark.parametrize("change",[
    lambda a:a.update(external_inventory_reconciled=False),
    lambda a:a.update(matched_mininex_swap_transfers=0),
    lambda a:a.update(all_asset_movements_match_verified_orders=False),
])
def test_unverified_inventory_never_reaches_net_pnl(monkeypatch,change):
    rows=[dict(tx_hash=TX_BUY,wallet_address=WALLET)]
    source=proof("BUY",TX_BUY,100,"100","2")
    monkeypatch.setattr(token_net,"_load_all_verified_owner_token_fills",
                        lambda did,asset:rows)
    async def priced(**kw):
        return source
    monkeypatch.setattr(token_net,"independently_price_stored_evm_fill",priced)
    a=audit()
    a["matched_mininex_swap_transfers"]=1
    change(a)
    async def check_audit(**kw):
        return a
    monkeypatch.setattr(token_net,"audit_owner_erc20_token_inventory",check_audit)
    async def run():
        async with httpx.AsyncClient() as client:
            return await token_net.read_owner_erc20_verified_realized_net(
                did="did:privy:account_12345",asset="LINK",client=client)
    with pytest.raises(ValueError):
        asyncio.run(run())


def test_postgres_fill_must_match_finalized_original_broadcast():
    route=_pair("UNI","USDC","BUY")
    row=dict(
        asset="UNI",chain_id=route["chain_id"],side="BUY",stable="USDC",
        intent_key="sig_123456789",tx_hash=TX_BUY,wallet_address=WALLET,
        created_at=datetime.now(timezone.utc),
        source_amount=Decimal("100"),destination_amount=Decimal("2"),
        network_fee_native=Decimal("0.001"),
        broadcast_status="FINALIZED_SUCCESS",
        broadcast_side="BUY",broadcast_stable="USDC",
        broadcast_wallet_address=WALLET,broadcast_tx_hash=TX_BUY)
    assert token_net._verified_account_fill(row,"UNI")["tx_hash"]==TX_BUY
    row["broadcast_status"]="SUBMITTED"
    with pytest.raises(ValueError):
        token_net._verified_account_fill(row,"UNI")


def test_verified_token_closures_enter_existing_hwm_once_without_wallet_transfer(monkeypatch):
    from backend import shared_evm_hwm_ledger as hwm
    events = []
    def fake_record(**kw):
        events.append(kw)
        return {"asset":kw["asset"],"fee_assessed_usd":"1.23",
                "source_tx_hash":kw["source_tx_hash"],
                "fee_token_transferred":False}
    monkeypatch.setattr(hwm,"record_internal_evm_realized_close",fake_record)
    async def reconciled(**kw):
        return {
            "mode":"RECONCILED_EVM_FIFO_NET_READ_ONLY",
            "asset":"LINK","wallet_address":WALLET,
            "fee_assessed":False,"hwm_updated":False,
            "asset_transfer_audit":{
                "wallet_address":WALLET,
                "external_inventory_reconciled":True,
                "no_other_execution_costs_verified":True},
            "closed_trades":[{
                "mode":"RECONCILED_EVM_NET_USD_MATH_ONLY",
                "asset":"LINK","wallet_address":WALLET,
                "tx_hash":TX_SELL,"intent_key":"sig_0123456789",
                "realized_net_usd":"24.7",
                "hwm_updated":False,"fee_assessed":False,
            }],
        }
    monkeypatch.setattr(token_net,"read_owner_erc20_verified_realized_net",reconciled)
    async def run():
        async with httpx.AsyncClient() as client:
            return await token_net.reconcile_owner_erc20_hwm_after_verified_sell(
                did="did:privy:account_12345",asset="LINK",client=client)
    result=asyncio.run(run())
    assert result["verified_closes"]==1
    assert events[0]["realized_net_usd"]=="24.7"
    assert events[0]["source_tx_hash"]==TX_SELL
    assert result["privy_treasury_signer_used"] is False
    assert result["fee_transferred"] is False


@pytest.mark.parametrize("tamper",[
    lambda x:x.update(fee_assessed=True),
    lambda x:x.update(hwm_updated=True),
    lambda x:x["asset_transfer_audit"].update(
        external_inventory_reconciled=False),
    lambda x:x["asset_transfer_audit"].update(
        no_other_execution_costs_verified=False),
    lambda x:x["closed_trades"][0].update(wallet_address="0x"+"cd"*20),
    lambda x:x["closed_trades"][0].update(mode="PAPER_NET"),
])
def test_hwm_cannot_be_written_from_incomplete_token_report(monkeypatch,tamper):
    from backend import shared_evm_hwm_ledger as hwm
    events=[]
    monkeypatch.setattr(hwm,"record_internal_evm_realized_close",
                        lambda **kw:events.append(kw))
    report={
        "mode":"RECONCILED_EVM_FIFO_NET_READ_ONLY",
        "asset":"LINK","wallet_address":WALLET,
        "fee_assessed":False,"hwm_updated":False,
        "asset_transfer_audit":{
            "wallet_address":WALLET,
            "external_inventory_reconciled":True,
            "no_other_execution_costs_verified":True},
        "closed_trades":[{
            "mode":"RECONCILED_EVM_NET_USD_MATH_ONLY",
            "asset":"LINK","wallet_address":WALLET,
            "tx_hash":TX_SELL,"intent_key":"sig_0123456789",
            "realized_net_usd":"24.7","hwm_updated":False,
            "fee_assessed":False,
        }],
    }
    tamper(report)
    async def fake(**kw):
        return report
    monkeypatch.setattr(token_net,"read_owner_erc20_verified_realized_net",fake)
    async def run():
        async with httpx.AsyncClient() as client:
            return await token_net.reconcile_owner_erc20_hwm_after_verified_sell(
                did="did:privy:account_12345",asset="LINK",client=client)
    with pytest.raises(ValueError):
        asyncio.run(run())
    assert events==[]
