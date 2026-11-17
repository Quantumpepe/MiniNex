"""Finality for all five new EVM coins binds actual on-chain receipt and journal."""
import asyncio

import pytest
from fastapi import HTTPException

from backend.shared_evm_order_finality import (
    _finalize_locked,reconcile_evm_internal_order,
)
from backend.shared_evm_swap_receipts import _pair

DID="did:privy:test"
KEY="sig_abcdef01234567890123456789"
TX="0x"+"ab"*32
WALLET="0x"+"ab"*20


class Cursor:
    def __init__(self,row,update_status=None):
        self.row=row;self.update_status=update_status;self.sql=[]
    def execute(self,query,args):self.sql.append((query,args))
    def fetchone(self):
        if self.sql[-1][0].lstrip().startswith("UPDATE"):
            if "mininex_live_evm_budget_reservations" in self.sql[-1][0]:
                return {"state":self.sql[-1][1][0]}
            return {"status":self.update_status or self.sql[-1][1][0]}
        return self.row


def receipt(asset,side="BUY",stable=None,success=True):
    stable=stable or ("USDT" if asset=="BNB" else "USDC")
    p=_pair(asset,stable,side)
    return {
        "mode":"SHARED_EVM_FINALIZED_SWAP_VERIFICATION",
        "asset":asset,"network":p["network"],"chain_id":p["chain_id"],
        "side":side,"stable":stable,"tx_hash":TX,
        "wallet_address":WALLET.lower(),
        "transaction_finalized":True,
        "state":"FINALIZED_ERC20_SWAP_LEG" if success else "FINALIZED_REVERTED",
        "swap_erc20_leg_verified":success,
        "source_spent_amount":"1" if success else None,
        "destination_received_amount":"2" if success else None,
        "network_fee_native":"0.0002" if success else None,
        "performance_fee_assessed":False,
        "real_order_recorded":False,
    }


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI"])
@pytest.mark.parametrize("success", [True,False])
def test_all_evm_assets_bind_onchain_receipt_to_durable_intent(asset,success):
    proof=receipt(asset,success=success)
    row={"status":"SUBMITTED","tx_hash":TX,
         "chain_id":proof["chain_id"],"wallet_address":WALLET,
         "side":proof["side"],"stable":proof["stable"]}
    cur=Cursor(row)
    state=_finalize_locked(
        cur,did=DID,key=KEY,asset=asset,proof=proof)
    assert state==("FINALIZED_SUCCESS" if success else "FINALIZED_REVERTED")
    assert len(cur.sql)==(4 if success else 3)
    assert "FOR UPDATE" in cur.sql[0][0]
    assert "AND status='SUBMITTED'" in cur.sql[1][0]
    assert cur.sql[1][1][-1]==TX
    if success:
        assert "INSERT INTO mininex_live_evm_verified_fills" in cur.sql[2][0]
        assert cur.sql[2][1][4]==TX
    assert "mininex_live_evm_budget_reservations" in cur.sql[-1][0]
    assert "AND state='SUBMITTED'" in cur.sql[-1][0]
    expected_budget=(
        "RECONCILIATION_REQUIRED" if asset in ("BNB","POL","AVAX")
        else "FINALIZED")
    assert cur.sql[-1][1][0]==expected_budget


@pytest.mark.parametrize("alter",[
    lambda p:p.update(transaction_finalized=False),
    lambda p:p.update(chain_id=1),
    lambda p:p.update(network="bsc-mainnet"),
    lambda p:p.update(swap_erc20_leg_verified=False),
    lambda p:p.update(source_spent_amount=None),
    lambda p:p.update(network_fee_native=None),
    lambda p:p.update(network_fee_native="-1"),
    lambda p:p.update(destination_received_amount="nan"),
    lambda p:p.update(performance_fee_assessed=True),
    lambda p:p.update(real_order_recorded=True),
    lambda p:p.update(state="INCLUDED_SUCCESS"),
])
def test_unverified_chain_or_token_proof_cannot_finalize(alter):
    proof=receipt("AVAX")
    alter(proof)
    cur=Cursor({"status":"SUBMITTED","tx_hash":TX,
                "chain_id":43114,"side":"BUY","stable":"USDC",
                "wallet_address":WALLET})
    with pytest.raises(ValueError):
        _finalize_locked(cur,did=DID,key=KEY,asset="AVAX",proof=proof)
    assert cur.sql==[]


@pytest.mark.parametrize("alter",[
    lambda r:r.update(status="SENDING"),
    lambda r:r.update(wallet_address="0x"+"cd"*20),
    lambda r:r.update(status="FINALIZED_SUCCESS"),
    lambda r:r.update(tx_hash="0x"+"cd"*32),
    lambda r:r.update(chain_id=56),
    lambda r:r.update(side="SELL"),
    lambda r:r.update(stable="USDT"),
])
def test_only_matching_submitted_durable_order_can_finalize(alter):
    row={"status":"SUBMITTED","tx_hash":TX,
         "chain_id":137,"side":"BUY","stable":"USDC",
         "wallet_address":WALLET}
    alter(row)
    cur=Cursor(row)
    with pytest.raises(HTTPException):
        _finalize_locked(cur,did=DID,key=KEY,
                         asset="POL",proof=receipt("POL"))
    assert len(cur.sql)==1


def test_failed_finalization_status_never_inserts_fill():
    proof=receipt("LINK")
    cur=Cursor({
        "status":"SUBMITTED","tx_hash":TX,"chain_id":1,
        "side":"BUY","stable":"USDC","wallet_address":WALLET,
    },update_status="SUBMITTED")
    with pytest.raises(HTTPException) as err:
        _finalize_locked(cur,did=DID,key=KEY,asset="LINK",proof=proof)
    assert err.value.status_code==409
    assert len(cur.sql)==2


def test_unfinalized_automated_reconciliation_does_not_write_database(monkeypatch):
    from backend import shared_evm_order_finality as mod
    async def verify(did,tx_hash,*,asset,side,stable):
        assert did==DID
        return {"transaction_finalized":False,"state":"INCLUDED_SUCCESS"}
    monkeypatch.setattr(mod,"check_user_evm_swap",verify)
    result=asyncio.run(reconcile_evm_internal_order(
        did=DID,intent_key=KEY,asset="AVAX",tx_hash=TX,
        side="BUY",stable="USDC"))
    assert result["state"]=="AWAITING_FINALITY"
    assert result["fee_assessed"] is False
    assert result["trade_settled"] is False


def test_reconciliation_module_has_no_public_fastapi_route():
    from backend.app import app
    assert all("evm-order-finalize" not in r.path for r in app.routes)
