"""Owner-scoped, read-only realized automatic trading return.

No deposit, withdrawal, open position or simulated trade is a realized close.
Use the same finalized FIFO evidence as the fee ledger, but never book fees.
"""
from fractions import Fraction
import logging
import psycopg
from psycopg.rows import dict_row
from fastapi import HTTPException
from .storage import _db_url, _ensure_schema, _safe_did
from .realized_net_snapshot import NET_FILLS_SQL, hydrate_net_rows
from .realized_net_accounting import calculate_realized_net, exact, decimal_text

LOG = logging.getLogger("mininex.portfolio_return")


def summarize_realized_return(rows):
    closes = calculate_realized_net(rows)
    by_key = {(r["asset"], r["family"], r["fill_key"]): r for r in rows}
    net, basis = Fraction(0), Fraction(0)
    verified = pending = 0
    for close in closes:
        if not close["net_verified"]:
            pending += 1
            continue
        row = by_key[(close["asset"], close["family"], close["fill_key"])]
        gain = Fraction(*map(int, close["net_usd_ratio"]))
        rate = row["stable_usd"]
        entry = exact(row["destination_amount"]) * rate - row["network_cost_usd"] - gain
        if entry <= 0:
            pending += 1
            continue
        # Include the exact assessed 5% amount in settlement atomics, once,
        # whether already paid or still reserved. Never subtract it twice.
        fee = Fraction(int(close["fee_due_stable_atomic"]), 10**6) * rate
        net += gain - fee
        basis += entry
        verified += 1
    available = verified > 0 or pending == 0
    return {
        "mode": "REALIZED_AUTO_NET_READ_ONLY",
        "pnl_usd": decimal_text(net) if available else None,
        "pnl_pct": decimal_text(net * 100 / basis, 8) if basis else ("0" if available else None),
        "verified_closes": verified,
        "pending_closes": pending,
        "paper_excluded": True,
    }


def read_owner_realized_return(did):
    did = _safe_did(did)
    try:
        db = _db_url()
        _ensure_schema(db)
        with psycopg.connect(db, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("WITH fills AS (" + NET_FILLS_SQL +
                            ") SELECT * FROM fills ORDER BY settled_at,fill_key",
                            {"did": did})
                return summarize_realized_return(hydrate_net_rows(cur.fetchall()))
    except (psycopg.Error, ValueError, KeyError, TypeError, ArithmeticError):
        LOG.warning("Verified portfolio return unavailable")
        raise HTTPException(503, "Verified portfolio return unavailable") from None
