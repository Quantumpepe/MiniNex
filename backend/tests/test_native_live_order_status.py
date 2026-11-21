"""BTC/SOL/XRP owner order readback never creates a paper or real trade."""
from datetime import datetime,timezone
import pytest
from fastapi import HTTPException
from backend import native_live_order_status as status

DID="did:privy:known"
KEY="native_"+"a"*64

def row(asset="BTC",side="BUY",state="SOURCE_FINALIZED"):
    return dict(asset=asset,side=side,stable="USDC",
        intent_key=KEY,status=state,
        chain="ethereum-mainnet" if side=="BUY" else {
            "BTC":"bitcoin-mainnet","SOL":"solana-mainnet","XRP":"xrpl-mainnet"}[asset],
        budget_revision=1,input_amount="100.0",
        source_tx_hash=("0x"+"b"*64 if side=="BUY" else
            "c"*64 if asset in ("BTC","XRP") else "2"*88),
        source_signed_tx_hash=None,destination_tx_hash=None,
        destination_proof_sha256=None,destination_received_atomic=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc))

def test_original_native_order_history_only_reports_received_if_true_chain_fill():
    old=row()
    proof=status._original_native_order_row(old,"BTC")
    assert proof["source_blockchain_finalized"] is True
    assert proof["destination_wallet_credited"] is False
    assert proof["realized_net_pnl_usd"] is None
    assert proof["wallet_transaction_sent_by_this_api"] is False
    complete={**old,"status":"DESTINATION_FINALIZED",
              "destination_tx_hash":"d"*64,
              "destination_proof_sha256":"f"*64,
              "destination_received_atomic":800000}
    credited=status._original_native_order_row(complete,"BTC")
    assert credited["destination_wallet_credited"] is True
    assert credited["destination_asset"]=="BTC"
    assert credited["destination_amount_atomic"]=="800000"
    assert credited["destination_tx_hash"]=="d"*64
    refunded={**old,"status":"REFUNDED_VERIFIED",
              "destination_tx_hash":"e"*64,
              "destination_proof_sha256":"f"*64,
              "destination_received_atomic":99000000}
    returned=status._original_native_order_row(refunded,"BTC")
    assert returned["original_funds_refunded"] is True
    assert returned["destination_asset"]=="USDC"
    assert returned["destination_tx_hash"]=="0x"+"e"*64

@pytest.mark.parametrize("mutation",[
    lambda r:r.update(asset="SOL"),
    lambda r:r.update(chain="xrpl-mainnet"),
    lambda r:r.update(destination_tx_hash="0x"+"e"*64),
    lambda r:r.update(source_tx_hash=None),
    lambda r:r.update(status="FINALIZED"),
    lambda r:r.update(input_amount="-2"),
    lambda r:r.update(destination_received_atomic="10"),
])
def test_unverified_native_order_states_never_infer_a_fill(mutation):
    original=row()
    mutation(original)
    with pytest.raises(ValueError):
        status._original_native_order_row(original,"BTC")

def test_native_order_status_db_enforces_same_owner_and_coin(monkeypatch):
    requested=[]
    class DB:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def cursor(self,**kw):return self
        def execute(self,sql,args):
            requested.append((sql,args))
        def fetchall(self):return [row()]
    monkeypatch.setattr(status,"_db_url",lambda:"postgresql://test")
    monkeypatch.setattr(status,"_ensure_schema",lambda db:None)
    monkeypatch.setattr(status.psycopg,"connect",lambda *a,**kw:DB())
    result=status.get_owner_native_live_order_status(DID,asset="BTC",limit=20)
    assert len(result["orders"])==1
    assert result["includes_simulation"] is False
    assert requested[0][1]==(DID,"BTC",21)
    assert "WHERE privy_did=%s AND asset=%s" in requested[0][0]

def test_only_bearer_authenticated_native_order_get():
    from backend.app import app
    matches=[r for r in app.routes if r.path==
        "/api/live/native/order-status/{asset}"]
    assert len(matches)==1
    assert matches[0].methods=={"GET"}
    import inspect
    code=inspect.getsource(matches[0].endpoint)
    assert "verify_privy_bearer" in code
    assert "get_owner_native_live_order_status" in code
