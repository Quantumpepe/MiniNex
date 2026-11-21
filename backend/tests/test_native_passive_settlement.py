"""Native passive settlement is opt-in, read-only and crash-resilient."""
from __future__ import annotations

import asyncio

from backend import native_passive_settlement as worker


def test_passive_native_disabled_without_explicit_readonly_and_database(monkeypatch):
    monkeypatch.delenv("MININEX_NATIVE_PASSIVE_SETTLEMENT_ENABLED", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://testing")
    assert worker._native_passive_enabled() is False
    monkeypatch.setenv("MININEX_NATIVE_PASSIVE_SETTLEMENT_ENABLED",
                       "explicit-readonly")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert worker._native_passive_enabled() is False
    monkeypatch.setenv("DATABASE_URL", "postgresql://testing")
    assert worker._native_passive_enabled() is True


def test_native_observer_disabled_never_scans_any_db_or_rpc(monkeypatch):
    monkeypatch.setattr(worker, "_native_passive_enabled", lambda: False)
    async def unexpected(*a, **kw):
        raise AssertionError("Read-only native worker should stay disabled")
    monkeypatch.setattr(worker, "_reconcile_in_thread", unexpected)
    result = asyncio.run(worker.reconcile_original_native_passive_once())
    assert result["worker_enabled"] is False
    assert result["passes"] == 0
    assert result["scanned"] == 0
    assert result["new_transactions_sent"] == 0
    assert result["performance_fees_collected"] == 0
    assert result["source_resubmission_permitted"] is False


def test_native_passive_finality_checks_all_original_source_states(monkeypatch):
    monkeypatch.setattr(worker, "_native_passive_enabled", lambda: True)
    calls = []

    async def fake(callback, *, limit):
        calls.append((callback.__name__, limit))
        values = {
            "reconcile_original_native_thor_approvals_once": {
                "scanned": 2, "finalized_approvals": 1,
                "unresolved_without_resend": 1,
                "new_transactions_sent": 0,
            },
            "reconcile_unknown_original_native_signed_sources_once": {
                "scanned": 2, "finalized_without_resend": 1,
                "unresolved_without_resend": 1,
                "new_transactions_sent": 0,
            },
            "reconcile_original_native_source_once": {
                "scanned": 3, "finalized_sources": 2,
                "unverified_or_unavailable": 1,
                "new_transactions_sent": 0,
                "fees_paid": False,
            },
            "reconcile_original_eth_buy_sources_once": {
                "scanned": 1, "verified_eth_source": 1,
                "unverified_or_unavailable": 0,
                "new_transactions_sent": 0,
            },
            "reconcile_original_native_destinations_once": {
                "scanned": 3, "destination_finalized": 2,
                "refunds_verified": 1,
                "unverified_or_unavailable": 0,
                "new_transactions_sent": 0,
                "fees_paid": False,
            },
        }
        return values[callback.__name__]

    monkeypatch.setattr(worker, "_reconcile_in_thread", fake)
    result = asyncio.run(worker.reconcile_original_native_passive_once())
    assert calls == [
        ("reconcile_original_native_thor_approvals_once",24),
        ("reconcile_unknown_original_native_signed_sources_once", 24),
        ("reconcile_original_native_source_once", 24),
        ("reconcile_original_eth_buy_sources_once", 24),
        ("reconcile_original_native_destinations_once", 24),
    ]
    assert result["worker_enabled"] is True
    assert result["passes"] == 5
    assert result["scanned"] == 11
    assert result["router_approvals_finalized"] == 1
    assert result["source_finalized"] == 4
    assert result["unresolved_or_unavailable"] == 3
    assert result["errors"] == 0
    assert result["new_signatures_created"] == 0
    assert result["new_transactions_sent"] == 0
    assert result["destination_settlements_recorded"] == 3
    assert result["performance_fees_collected"] == 0


def test_unavailable_native_chain_does_not_block_other_read_only_receipts(
    monkeypatch,
):
    monkeypatch.setattr(worker, "_native_passive_enabled", lambda: True)
    calls = []

    async def partial(callback, *, limit):
        calls.append(callback.__name__)
        if len(calls) == 1:
            raise RuntimeError("chain explorer temporarily offline")
        return {
            "scanned": 1, "finalized_approvals": 1,
            "finalized_without_resend": 1, "finalized_sources": 1,
            "verified_eth_source": 1,
            "destination_finalized": 1, "refunds_verified": 0,
            "new_transactions_sent": 0, "fees_paid": False,
        }

    monkeypatch.setattr(worker, "_reconcile_in_thread", partial)
    result = asyncio.run(worker.reconcile_original_native_passive_once())
    assert len(calls) == 5
    assert result["passes"] == 4
    assert result["errors"] == 1
    assert result["new_transactions_sent"] == 0


def test_native_passive_observer_rejects_broadcast_claims_from_its_report(
    monkeypatch,
):
    monkeypatch.setattr(worker, "_native_passive_enabled", lambda: True)

    async def invalid(callback, *, limit):
        return {
            "scanned": 1, "finalized_without_resend": 1,
            "finalized_sources": 1, "verified_eth_source": 1,
            "new_transactions_sent": 1,
        }

    monkeypatch.setattr(worker, "_reconcile_in_thread", invalid)
    result = asyncio.run(worker.reconcile_original_native_passive_once())
    assert result["errors"] == 5
    assert result["source_finalized"] == 0
    assert result["new_transactions_sent"] == 0


def test_native_passive_worker_lifecycle_does_not_create_live_orders(monkeypatch):
    async def exercise():
        w = worker.PassiveNativeSettlementWorker()
        monkeypatch.setattr(worker, "_native_passive_enabled", lambda: False)
        await w.start()
        assert w._task is None
        await w.stop()

        monkeypatch.setattr(worker, "_native_passive_enabled", lambda: True)
        await w.start()
        task = w._task
        assert task is not None
        assert not task.done()
        await w.start()
        assert w._task is task
        await w.stop()
        assert w._task is None
        assert task.done()
    asyncio.run(exercise())


def test_original_native_auto_worker_requires_no_second_receipt_flag(monkeypatch):
    monkeypatch.delenv("MININEX_NATIVE_PASSIVE_SETTLEMENT_ENABLED",raising=False)
    monkeypatch.delenv("MININEX_NATIVE_AUTO_WORKER_ENABLED",raising=False)
    monkeypatch.delenv("MININEX_LIVE_TRADING_ENABLED",raising=False)
    monkeypatch.setenv("DATABASE_URL","postgresql://example.invalid/test")
    assert worker._native_passive_enabled() is False
    monkeypatch.setenv("MININEX_NATIVE_AUTO_WORKER_ENABLED","explicit-live")
    assert worker._native_passive_enabled() is False
    monkeypatch.setenv("MININEX_LIVE_TRADING_ENABLED","true")
    assert worker._native_passive_enabled() is True
    monkeypatch.delenv("MININEX_NATIVE_AUTO_WORKER_ENABLED")
    monkeypatch.setenv("MININEX_NATIVE_PASSIVE_SETTLEMENT_ENABLED","explicit-readonly")
    assert worker._native_passive_enabled() is True
    monkeypatch.delenv("DATABASE_URL")
    assert worker._native_passive_enabled() is False
