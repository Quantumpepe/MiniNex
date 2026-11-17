"""Native EVM capital stays reserved until real wrap/unwrap and swap settle."""
import inspect
import pytest
from fastapi import HTTPException

from backend.shared_evm_native_cycle_finality import (
    _complete_native_cycle_locked,
)
from backend.shared_evm_swap_receipts import _pair
from backend.storage import SCHEMA

DID="did:privy:owner"
KEY="sig_abcdef01234567890123456789"
WALLET="0x"+"ab"*20
SWAP="0x"+"cd"*32
NATIVE="0x"+"ef"*32
ASSETS={"BNB":"USDT","POL":"USDC","AVAX":"USDC"}


def proof(asset,side="BUY"):
    pair=_pair(asset,ASSETS[asset],side)
    return {
        "mode":"READ_ONLY_FINALIZED_EVM_TRADE_CYCLE_PROOF",
        "asset":asset,"side":side,"stable":ASSETS[asset],
        "network":pair["network"],"chain_id":pair["chain_id"],
        "wallet_address":WALLET,
        "swap_tx_hash":SWAP,
        "native_conversion_tx_hash":NATIVE,
        "native_conversion_verified":True,
        "required_transaction_legs_complete":True,
        "inventory_or_cost_basis_settled":False,
        "realized_net_profit_verified":False,
        "five_percent_fee_assessed":False,
    }


class Cursor:
    def __init__(self,rows):self.rows=iter(rows);self.sql=[]
    def execute(self,q,args):self.sql.append((q,args))
    def fetchone(self):return next(self.rows)


def rows(asset,side):
    pair=_pair(asset,ASSETS[asset],side)
    return [
        {"status":"FINALIZED_SUCCESS","tx_hash":SWAP,
         "wallet_address":WALLET,"chain_id":pair["chain_id"],
         "side":side,"stable":ASSETS[asset]},
        {"tx_hash":SWAP,"wallet_address":WALLET,
         "side":side,"stable":ASSETS[asset]},
        {"state":"FINALIZED"},
    ]


@pytest.mark.parametrize("asset",["BNB","POL","AVAX"])
@pytest.mark.parametrize("side",["BUY","SELL"])
def test_native_budget_only_releases_after_both_verified_chain_legs(asset,side):
    cur=Cursor(rows(asset,side))
    r=_complete_native_cycle_locked(
        cur,did=DID,asset=asset,key=KEY,proof=proof(asset,side))
    assert r["required_transaction_legs_complete"] is True
    assert r["budget_reservation_reconciled"] is True
    assert r["performance_fee_assessed"] is False
    assert len(cur.sql)==4
    assert "FOR UPDATE" in cur.sql[0][0]
    assert "FOR UPDATE" in cur.sql[1][0]
    assert "mininex_live_evm_completed_native_cycles" in cur.sql[2][0]
    assert "state='RECONCILIATION_REQUIRED'" in cur.sql[3][0]


@pytest.mark.parametrize("tamper",[
    lambda p:p.update(asset="ETH"),
    lambda p:p.update(native_conversion_verified=False),
    lambda p:p.update(required_transaction_legs_complete=False),
    lambda p:p.update(native_conversion_tx_hash=SWAP),
    lambda p:p.update(native_conversion_tx_hash="0xdead"),
    lambda p:p.update(realized_net_profit_verified=True),
    lambda p:p.update(five_percent_fee_assessed=True),
    lambda p:p.update(chain_id=1),
    lambda p:p.update(network="ethereum-mainnet"),
])
def test_native_reconciliation_rejects_wrong_chain_unverified_leg(tamper):
    p=proof("AVAX")
    tamper(p)
    cur=Cursor([])
    with pytest.raises(ValueError):
        _complete_native_cycle_locked(
            cur,did=DID,asset="AVAX",key=KEY,proof=p)
    assert len(cur.sql)==0


@pytest.mark.parametrize("wrong",[
    lambda r:r.update(status="SUBMITTED"),
    lambda r:r.update(tx_hash=NATIVE),
    lambda r:r.update(wallet_address="0x"+"ee"*20),
    lambda r:r.update(side="SELL"),
    lambda r:r.update(chain_id=1),
])
def test_native_cycle_cannot_attach_to_other_swap_or_wallet(wrong):
    inputs=rows("BNB","BUY")
    wrong(inputs[0])
    cur=Cursor(inputs)
    with pytest.raises(HTTPException):
        _complete_native_cycle_locked(
            cur,did=DID,asset="BNB",key=KEY,proof=proof("BNB"))
    assert len(cur.sql)==1


def test_missing_verified_swap_fill_keeps_native_budget_locked():
    inputs=rows("POL","SELL")
    inputs[1]["tx_hash"]=NATIVE
    cur=Cursor(inputs)
    with pytest.raises(HTTPException):
        _complete_native_cycle_locked(
            cur,did=DID,asset="POL",key=KEY,proof=proof("POL","SELL"))
    assert len(cur.sql)==2


def test_concurrent_budget_release_conflict_rolls_back_native_link():
    inputs=rows("AVAX","SELL")
    inputs[-1]=None
    cur=Cursor(inputs)
    with pytest.raises(HTTPException):
        _complete_native_cycle_locked(
            cur,did=DID,asset="AVAX",key=KEY,proof=proof("AVAX","SELL"))
    assert len(cur.sql)==4


def test_native_completion_is_internal_and_unique_per_chain_hash():
    ddl="\n".join(SCHEMA)
    assert "CREATE TABLE IF NOT EXISTS mininex_live_evm_completed_native_cycles" in ddl
    assert "UNIQUE(chain_id,conversion_tx_hash)" in ddl
    from backend.app import app
    assert all("native-cycle-settle" not in r.path for r in app.routes)
    from backend import shared_evm_native_cycle_finality as mod
    code=inspect.getsource(mod)
    assert "post_claimed_privy_evm_rpc" not in code
    assert "sign_privy" not in code
