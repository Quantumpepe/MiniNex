"""Multi-EVM passive worker never sends, signs or repeats ambiguous orders."""
import asyncio
import inspect

import pytest

from backend import shared_evm_passive_settlement as m

DID="did:privy:alice"
TX="0x"+"ab"*32
KEY="sig_abcdef01234567890123456789"


def pending(asset="BNB",side="BUY",stable=None):
    return {
        "privy_did":DID,"asset":asset,"intent_key":KEY,
        "tx_hash":TX,"side":side,
        "stable":stable or ("USDT" if asset=="BNB" else "USDC"),
        "chain_id":{"BNB":56,"POL":137,"AVAX":43114,
                    "LINK":1,"UNI":1,"ARB":42161}[asset],
    }


@pytest.mark.parametrize("asset",["BNB","POL","AVAX","LINK","UNI","ARB"])
def test_valid_network_specific_journal_row(asset):
    assert m._valid_pending_row(pending(asset)) is True


@pytest.mark.parametrize("change",[
    lambda r:r.update(chain_id=1),
    lambda r:r.update(tx_hash="0x1234"),
    lambda r:r.update(side="SHORT"),
    lambda r:r.update(privy_did="other"),
    lambda r:r.update(intent_key="bad"),
    lambda r:r.update(stable="DAI"),
])
def test_invalid_pending_claim_rejected_before_network(change):
    row=pending("AVAX")
    change(row)
    assert m._valid_pending_row(row) is False


def test_worker_defaults_off_without_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("MININEX_EVM_PASSIVE_SETTLEMENT_ENABLED",raising=False)
    monkeypatch.setenv("DATABASE_URL","postgresql://example.invalid/test")
    assert m._settlement_enabled() is False
    assert asyncio.run(m.reconcile_submitted_evm_once())=={
        "worker_enabled":False,"scanned":0,
        "finalized":0,"pending":0,"errors":0,
        "new_transactions_sent":0,
    }


def test_completed_pending_and_bad_rows_are_separately_counted(monkeypatch):
    monkeypatch.setenv("MININEX_EVM_PASSIVE_SETTLEMENT_ENABLED","explicit-readonly")
    monkeypatch.setenv("DATABASE_URL","postgresql://example.invalid/test")
    rows=[pending("BNB"),pending("AVAX"),pending("LINK"),
          {**pending("POL"),"chain_id":56},pending("UNI"),pending("ARB")]
    monkeypatch.setattr(m,"_pending_submitted",lambda limit:rows)
    calls=[]
    async def observed(**kwargs):
        calls.append(kwargs)
        if kwargs["asset"]=="BNB":
            return {"state":"FINALIZED_SUCCESS"}
        if kwargs["asset"]=="AVAX":
            return {"state":"FINALIZED_REVERTED"}
        if kwargs["asset"]=="LINK":
            return {"state":"AWAITING_FINALITY"}
        raise RuntimeError("Temporary RPC outage")
    monkeypatch.setattr(m,"reconcile_evm_internal_order",observed)
    result=asyncio.run(m.reconcile_submitted_evm_once(limit=10))
    assert result=={
        "worker_enabled":True,"scanned":6,
        "finalized":2,"pending":1,"errors":3,
        "new_transactions_sent":0,
    }
    assert [c["asset"] for c in calls]==["BNB","AVAX","LINK","UNI","ARB"]


def test_no_background_ability_to_resend_unknown_or_uncertain_claim():
    source=inspect.getsource(m)
    assert "post_claimed_privy_evm_rpc" not in source
    assert "sign_privy_evm_swap_request" not in source
    assert "claim_internal_evm_send" not in source
    assert "record_internal_evm_send_result" not in source
    assert "WHERE status='SUBMITTED' AND tx_hash IS NOT NULL" in source
    from backend.app import app
    assert all("evm-passive-settlement" not in r.path for r in app.routes)
