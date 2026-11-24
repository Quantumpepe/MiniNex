"""Passive native source reconciliation for BTC/SOL/XRP/TRX and native BUYs.

This component NEVER signs, broadcasts, creates orders, enables automatic
trading, collects fees, or alters wallet policies. It periodically checks
previously saved original transaction hashes and promotes only independently
finalized source-chain receipts through the existing database journal.

With a configured database the worker observes only existing journal rows;
no separate LIVE switch is required for receipt monitoring.
The reconciler runs in a dedicated thread event loop so blocking PostgreSQL
queries cannot freeze the API server's live pricing and user requests.
"""
from __future__ import annotations

import asyncio
import logging
import os

from .shared_evm_router_approval_finality import (
    reconcile_original_native_thor_approvals_once,
)
from .native_thor_approval_unknown_reconcile import (
    reconcile_unknown_thor_approvals_once,
)
from .native_eth_buy_unknown_reconcile import (
    reconcile_unknown_original_eth_buy_sources_once,
)
from .native_passive_destination import reconcile_original_native_destinations_once
from .native_live_source_finality import (
    reconcile_original_native_source_once,
    reconcile_unknown_original_native_signed_sources_once,
    reconcile_original_eth_buy_sources_once,
)

LOG = logging.getLogger("mininex.native_passive_settlement")
_INTERVAL_SECONDS = 30
_BATCH_SIZE = 24


def _native_passive_enabled() -> bool:
    """Observe existing durable orders whenever the database is configured.

    Already submitted transactions must reconcile even after the user pauses
    or the trading worker is disabled. This worker never creates orders,
    signs, broadcasts, charges fees or requires another LIVE switch.
    """
    return bool(os.getenv("DATABASE_URL","").strip())


async def _reconcile_in_thread(callback, *, limit: int) -> dict:
    """Give each read-only coroutine its own worker-thread event loop."""
    return await asyncio.to_thread(lambda: asyncio.run(callback(limit=limit)))


async def reconcile_original_native_passive_once() -> dict:
    """One bounded pass across actual source and destination chain finality."""
    result = {
        "mode": "NATIVE_ORIGINAL_PASSIVE_FINALITY_ONLY",
        "worker_enabled": _native_passive_enabled(),
        "passes": 0,
        "scanned": 0,
        "source_finalized": 0,
        "router_approvals_finalized": 0,
        "unknown_thor_approvals_identified": 0,
        "unknown_thor_approvals_reverted": 0,
        "unknown_eth_buy_sources_identified": 0,
        "unresolved_or_unavailable": 0,
        "errors": 0,
        "new_signatures_created": 0,
        "new_transactions_sent": 0,
        "destination_settlements_recorded": 0,
        "performance_fees_collected": 0,
        "source_resubmission_permitted": False,
    }
    if not result["worker_enabled"]:
        return result
    for callback, count_key in (
        (reconcile_unknown_thor_approvals_once,"recovered_approvals"),
        (reconcile_original_native_thor_approvals_once,"finalized_approvals"),
        (reconcile_unknown_original_eth_buy_sources_once,"recovered_buy_sources"),
        (reconcile_unknown_original_native_signed_sources_once,
         "finalized_without_resend"),
        (reconcile_original_native_source_once, "finalized_sources"),
        (reconcile_original_eth_buy_sources_once, "verified_eth_source"),
        (reconcile_original_native_destinations_once,
         "destination_finalized"),
    ):
        try:
            report = await _reconcile_in_thread(callback, limit=_BATCH_SIZE)
            if (type(report) is not dict
                    or type(report.get("scanned")) is not int
                    or type(report.get(count_key)) is not int
                    or report.get("new_transactions_sent") != 0
                    or report.get("fees_paid", False) is not False):
                raise ValueError("Native source read-only report invalid")
            result["passes"] += 1
            result["scanned"] += report["scanned"]
            if count_key == "recovered_approvals":
                # A recovered exact nonce is not yet a usable ERC20
                # approval until the ordinary passive finality verifies it.
                if type(report.get("reverted_approvals")) is not int:
                    raise ValueError("Native THOR revert recovery report missing")
                result["unknown_thor_approvals_identified"] += report[count_key]
                result["unknown_thor_approvals_reverted"] += report[
                    "reverted_approvals"]
            elif count_key == "finalized_approvals":
                result["router_approvals_finalized"] += report[count_key]
            elif count_key == "recovered_buy_sources":
                # A recovered transaction is not yet a proven native fill.
                result["unknown_eth_buy_sources_identified"] += report[count_key]
            elif count_key == "destination_finalized":
                if type(report.get("refunds_verified")) is not int:
                    raise ValueError("Native refund finality report missing")
                result["destination_settlements_recorded"] += (
                    report["destination_finalized"] + report["refunds_verified"])
            else:
                result["source_finalized"] += report[count_key]
            result["unresolved_or_unavailable"] += (
                report.get("unresolved_without_resend", 0)
                + report.get("unverified_or_unavailable", 0))
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("Original native passive source check failed")
            result["errors"] += 1
    return result


class PassiveNativeSettlementWorker:
    """Passive observer, not a native trading strategy worker."""
    def __init__(self):
        self._task = None

    async def start(self) -> None:
        if _native_passive_enabled() and self._task is None:
            self._task = asyncio.create_task(
                self._run(), name="mininex-native-passive-finality")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(_INTERVAL_SECONDS)
            if _native_passive_enabled():
                await reconcile_original_native_passive_once()
