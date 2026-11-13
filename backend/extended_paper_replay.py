"""Bounded, past-only market sample preparation for extra-coin PAPER replay.

The existing extra-market collector already has 1-minute candles and short
live ticks. Collapse only observations within the SAME 30-second bucket,
choosing the most recent observed mark. Never average future data into past
samples, request new quotes or alter any live strategy or account balance.
"""
from __future__ import annotations

import math
from typing import Any


def compact_replay_observations(rows: Any, *, bucket_seconds: int = 30
                                ) -> list[dict[str, float]]:
    if (not isinstance(rows, list) or len(rows) > 3600
            or type(bucket_seconds) is not int or not 15 <= bucket_seconds <= 60):
        return []
    out: list[dict[str, float]] = []
    last = -math.inf
    previous_bucket: int | None = None
    for row in rows:
        if not isinstance(row, dict):
            return []
        try:
            at = float(row["timestamp"])
            price = float(row["price_usdt"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return []
        if (not math.isfinite(at) or not math.isfinite(price)
                or at <= 0 or price <= 0 or at < last):
            return []
        point = {"timestamp": at, "price_usdt": price}
        bucket = math.floor(at / bucket_seconds)
        if bucket == previous_bucket:
            # Exactly this later observation replaces the same bucket; no
            # sample is moved backwards and no future prices are revealed.
            out[-1] = point
        else:
            out.append(point)
            previous_bucket = bucket
        last = at
    return out
