import asyncio
from backend import net_fee_send as m

def test_net_fee_sender_disabled_until_receipt_reconciliation(monkeypatch):
    assert m.net_fee_worker_enabled() is False
    def forbidden(*a,**k):raise AssertionError("Disabled worker must not query or sign")
    monkeypatch.setattr(m,"pending_net_fee_sends",forbidden)
    monkeypatch.setattr(m,"audit_original_evm_coin",forbidden)
    assert asyncio.run(m.reconcile_net_fee_sends_once(None)) is None
    assert asyncio.run(m.submit_net_fee_once(None,{})) is None
