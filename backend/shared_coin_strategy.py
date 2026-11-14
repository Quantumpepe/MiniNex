"""One shared MiniNex strategy decision path for all 11 active spot coins.

BTC, ETH and the nine extra-coin sessions call this exact engine with their
OWN restored portfolio, budget, history and settlement asset. No changes to
wallets, no synthetic fills beyond the existing PAPER engine, no LIVE orders.
Future trading adapters must consume independently verified order intents,
not these paper events as proof of a real swap.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from .auto_engine import PaperAutoEngine, PaperPortfolio, PaperRules
from .strategy_replay import REPLAY_ASSETS, WINDOWS

COINS = tuple(REPLAY_ASSETS)
assert COINS == ("BTC", "ETH", "BNB", "SOL", "XRP", "TRX", "POL",
                 "AVAX", "LINK", "DOGE", "UNI")


@dataclass(frozen=True)
class CoinDecision:
    asset: str
    action: str
    reason: str
    new_paper_trade: dict | None
    paper_only: bool = True
    live_order_submitted: bool = False


def _parameters(asset: str, portfolio: PaperPortfolio, at: float, horizon: str) -> None:
    if asset not in COINS or type(asset) is not str or portfolio.asset != asset:
        raise ValueError("Unsupported or mixed-asset strategy portfolio")
    if horizon not in WINDOWS:
        raise ValueError("Unsupported strategy time horizon")
    if type(at) not in (float, int) or not math.isfinite(at) or at <= 0:
        raise ValueError("Invalid strategy timestamp")
    if portfolio.strategy_mode not in ("LONG", "SPOT_SHORT", "AUTO"):
        raise ValueError("Unsupported spot strategy mode")


def evaluate_coin_strategy(
    asset: str, portfolio: PaperPortfolio, market: Any, at: float, horizon: str,
) -> CoinDecision:
    """Evaluate exactly one coin using the same trend/DCA/trailing logic.

    This is the ONLY per-session strategy entry point for both old and extra
    PAPER accounts. Each caller owns its DB row lock and persists its own
    fills; this helper cannot access another coin's budget or position.
    """
    _parameters(asset, portfolio, at, horizon)
    engine = PaperAutoEngine(market, rules=PaperRules(
        warmup_seconds=0.0,
        min_history_samples=4,
        price_window_seconds=WINDOWS[horizon],
    ))
    engine.portfolios = {asset: portfolio}
    feed = market.snapshot(now=at)["markets"][asset]
    old_count = portfolio.trade_count
    engine.process_asset(asset, feed, at)
    difference = portfolio.trade_count - old_count
    if difference not in (0, 1):
        raise ValueError("A strategy evaluation cannot emit multiple orders")
    event = None
    if difference:
        event = dict(portfolio.trade_history[-1])
        if (event.get("asset") != asset or event.get("real_trade") is not False
                or event.get("kind") not in ("PAPER_BUY", "PAPER_SELL")):
            raise ValueError("Unexpected strategy fill type")
    portfolio.last_evaluated_at = at
    return CoinDecision(asset, portfolio.last_decision, portfolio.last_reason, event)


def exit_coin_to_stable(
    asset: str, portfolio: PaperPortfolio, market: Any, at: float,
) -> bool:
    """A single shared pause-to-stable PAPER conversion for all 11 coins.

    False means the account is already paused but the simulated conversion
    still requires a valid current market price; a future tick can retry it.
    This is not an on-chain swap or a wallet transfer.
    """
    _parameters(asset, portfolio, at, "15m")
    if portfolio.coin_quantity <= 0:
        return True
    try:
        feed = market.snapshot(now=at)["markets"][asset]
        raw = feed.get("price_usdt")
        if (feed.get("market_data_valid") is not True
                or type(raw) not in (int, float)
                or not math.isfinite(raw) or raw <= 0):
            return False
        engine = PaperAutoEngine(market)
        quote = engine._sell_quote(asset, portfolio.coin_quantity, float(raw))
        if not quote:
            return False
        engine._paper_sell(portfolio, quote, at, float(raw), "USER_PAUSE_TO_STABLE")
        return True
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return False
