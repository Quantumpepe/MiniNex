"""Finalized blockchain legs become owner-bound durable activity, not Paper."""
import asyncio
import pytest
from fastapi import HTTPException
from backend import real_eth_trade_legs as eth

H="0x"+"a"*64
def swap():
    return dict(tx_hash=H,asset="ETH",network="ethereum-mainnet",
        transaction_finalized=True,execution_enabled=False,
        state="FINALIZED_ERC20_SWAP_LEG",
        swap_erc20_leg_verified=True,side="BUY",stable="USDC",
        swap_output_asset="WETH",source_spent_amount="10",
        destination_received_amount="0.02",network_fee_eth="0.0001")

def test_finalized_swap_yields_exact_persistable_fields():
    leg=eth.validated_finalized_leg(tx_hash=H,side="BUY",stable="USDC",
                                    kind="SWAP",receipt=swap())
    assert leg["source_amount"]=="10"
    assert leg["destination_amount"]=="0.02"
    assert leg["tx_hash"]==H

@pytest.mark.parametrize("field,value",[
  ("transaction_finalized",False),("state","PENDING"),
  ("swap_erc20_leg_verified",False),("side","SELL"),
  ("destination_received_amount","NaN"),("network","polygon-pos-mainnet"),
  ("tx_hash","0x"+"b"*64),("execution_enabled",True),
])
def test_bad_proof_never_records(field,value):
    evidence=swap();evidence[field]=value
    with pytest.raises(ValueError):
        eth.validated_finalized_leg(tx_hash=H,side="BUY",stable="USDC",
                                    kind="SWAP",receipt=evidence)

@pytest.mark.parametrize("kind,side,operation",[
    ("WRAP","SELL","DEPOSIT"),("UNWRAP","BUY","WITHDRAW")])
def test_native_weth9_conversion_is_separate_leg(kind,side,operation):
    receipt=dict(tx_hash=H,asset="ETH",network="ethereum-mainnet",
        transaction_finalized=True,execution_enabled=False,
        state="FINALIZED_WETH9_CONVERSION",confirmed_native_conversion=True,
        operation=operation,weth9_event_amount="0.3",
        network_fee_eth="0.00012")
    leg=eth.validated_finalized_leg(tx_hash=H,side=side,stable="USDC",
                                    kind=kind,receipt=receipt)
    assert leg["source_amount"]=="0.3"
    assert leg["kind"]==kind

def test_authenticated_receipt_is_required_before_db_write(monkeypatch):
    calls=[]
    async def fake(did,hash,*,side,stable):
        calls.append((did,hash,side,stable));return swap()
    monkeypatch.setattr(eth,"check_user_router02_receipt",fake)
    monkeypatch.setattr(eth,"save_verified_leg",lambda did,leg:
        {"recorded":True,"did":did,**leg})
    result=asyncio.run(eth.record_real_eth_leg("did:privy:verified",
        tx_hash=H,side="BUY",stable="USDC",kind="SWAP"))
    assert result["recorded"]
    assert calls==[("did:privy:verified",H,"BUY","USDC")]

def test_invalid_stage_rejected_before_chain_call():
    with pytest.raises(HTTPException) as e:
        asyncio.run(eth.record_real_eth_leg("did:privy:verified",
            tx_hash=H,side="SELL",stable="USDC",kind="UNWRAP"))
    assert e.value.status_code==422


def test_confirmed_swaps_are_owner_and_order_bound(monkeypatch):
    """The read-only API cannot mix unmatched, pending or manual legs."""
    from datetime import datetime, timezone
    import inspect
    sql = inspect.getsource(eth.get_confirmed_eth_swaps)
    for required in ("l.privy_did=o.privy_did", "l.tx_hash=o.tx_hash",
                     "l.side=o.side", "l.stable=o.stable",
                     "l.kind='SWAP'", "o.state='CONFIRMED'",
                     "o.privy_did=%s", "o.finalized_at IS NOT NULL",
                     "n.privy_did=o.privy_did", "n.source_key=o.intent_key",
                     "u.privy_did=n.privy_did", "u.tx_hash=n.tx_hash",
                     "LIMIT 101"):
        assert required in sql

    class Cursor:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def execute(self,sql,args): self.sql=sql; self.params=args
        def fetchall(self):
            assert self.params==("did:privy:verified",)
            return [
                {"intent_key":"buy_1","side":"BUY","stable":"USDC",
                 "tx_hash":H,"source_amount":"100.5",
                 "destination_amount":"0.03","network_fee_eth":"0.0001",
                 "finalized_at":datetime(2026,11,16,tzinfo=timezone.utc),
                 "native_unwrap_status":"CONFIRMED",
                 "native_unwrap_hash":"0x"+"c"*64,
                 "native_unwrap_wei":30000000000000000,
                 "verified_unwrap_eth":"0.03",
                 "unwrap_network_fee_eth":"0.00009"},
                {"intent_key":"sell_1","side":"SELL","stable":"USDT",
                 "tx_hash":"0x"+"b"*64,"source_amount":"0.02",
                 "destination_amount":"72","network_fee_eth":"0.0002",
                 "finalized_at":datetime(2026,11,16,tzinfo=timezone.utc),
                 "native_unwrap_status":None,
                 "native_unwrap_hash":None,
                 "native_unwrap_wei":None,
                 "verified_unwrap_eth":None,
                 "unwrap_network_fee_eth":None},
            ]
    class Connection:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def cursor(self,**kwargs): return Cursor()
    monkeypatch.setattr(eth,"_db_url",lambda:"db")
    monkeypatch.setattr(eth,"_ensure_schema",lambda url:None)
    monkeypatch.setattr(eth,"_prepare_rows",lambda cur,did:None)
    monkeypatch.setattr(eth.psycopg,"connect",lambda *args,**kwargs:Connection())
    result=eth.get_confirmed_eth_swaps("did:privy:verified")
    assert [s["side"] for s in result["swaps"]]==["BUY","SELL"]
    assert result["swaps"][0]["spent_asset"]=="USDC"
    assert result["swaps"][0]["received_asset"]=="WETH"
    assert result["swaps"][1]["spent_asset"]=="WETH"
    assert result["swaps"][1]["received_asset"]=="USDT"
    assert result["swaps"][0]["native_conversion_status"]=="CONFIRMED_VERIFIED"
    assert result["swaps"][0]["native_conversion_network_fee_eth"]=="0.00009"
    assert result["swaps"][1]["native_conversion_status"]=="NOT_APPLICABLE"
    assert result["history_truncated"] is False
    assert result["realized_pnl_reconciled"] is False
    assert result["performance_fee_assessed"] is False


def test_confirmed_swap_api_requires_authentication():
    from backend.app import app
    import inspect
    route=next(r for r in app.routes if r.path=="/api/live/eth/confirmed-swaps")
    assert "verify_privy_bearer" in inspect.getsource(route.endpoint)


@pytest.mark.parametrize("status,units,verified,expected", [
    ("CONFIRMED",30000000000000000,"0.03","CONFIRMED_VERIFIED"),
    ("CONFIRMED",30000000000000000,"0.02","RECONCILIATION_REQUIRED"),
    ("CONFIRMED",20000000000000000,"0.03","RECONCILIATION_REQUIRED"),
    ("SUBMITTED",30000000000000000,None,"SUBMITTED"),
    ("REVERTED",30000000000000000,None,"REVERTED"),
])
def test_native_buy_conversion_requires_matching_finalized_amount(
    monkeypatch, status, units, verified, expected
):
    from datetime import datetime, timezone

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, args): assert args==("did:privy:verified",)
        def fetchall(self):
            return [{
                "intent_key":"buy", "side":"BUY", "stable":"USDC",
                "tx_hash":H, "source_amount":"100.5",
                "destination_amount":"0.03", "network_fee_eth":"0.0001",
                "finalized_at":datetime(2026,11,16,tzinfo=timezone.utc),
                "native_unwrap_status":status,
                "native_unwrap_hash":"0x"+"c"*64,
                "native_unwrap_wei":units,
                "verified_unwrap_eth":verified,
                "unwrap_network_fee_eth":"0.00009",
            }]
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def cursor(self, **kwargs): return Cursor()
    monkeypatch.setattr(eth,"_db_url",lambda:"db")
    monkeypatch.setattr(eth,"_ensure_schema",lambda url:None)
    monkeypatch.setattr(eth,"_prepare_rows",lambda cur,did:None)
    monkeypatch.setattr(eth.psycopg,"connect",lambda *args,**kwargs:Connection())
    result=eth.get_confirmed_eth_swaps("did:privy:verified")
    assert result["swaps"][0]["native_conversion_status"]==expected
    assert result["swaps"][0]["native_conversion_network_fee_eth"] == (
        "0.00009" if expected=="CONFIRMED_VERIFIED" else None
    )
