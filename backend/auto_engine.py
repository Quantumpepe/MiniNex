"""MiniNex V0.3 -- deterministic BTC/ETH spot AUTO PAPER engine.

No wallet access, signatures, blockchain RPC, order routing or real execution.
All prices and fees are INDICATIVE. USDC/USD and USDT/USD parity is a paper-only
assumption and MUST NOT be used as a live execution quote.

Each asset has an independent balance ledger: BTC -> stable -> BTC; ETH -> stable -> ETH.
Costs are included in the configured all-in budget and realised PnL. Stop-loss
sells can crystallise a loss; they are never labelled as profit.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import math
import time
from typing import Any

LOGGER = logging.getLogger("mininex.paper")
SUPPORTED = ("BTC", "ETH")
STABLES = ("USDC", "USDT")


def clean(n: float, places: int = 8) -> float:
    return round(float(n), places)


def iso(t: float | None) -> str | None:
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat() if t is not None else None


@dataclass(frozen=True)
class PaperCosts:
    """Intentionally conservative PAPER estimates, not executable route quotes."""
    venue_fee_pct: float = 0.10
    spread_pct: float = 0.05
    slippage_pct: float = 0.08
    btc_network_usd: float = 2.50
    eth_network_usd: float = 1.50

    @property
    def rate(self) -> float:
        return self.venue_fee_pct / 100

    @property
    def execution_markup(self) -> float:
        return (self.spread_pct + self.slippage_pct) / 100

    def network(self, asset: str) -> float:
        return self.btc_network_usd if asset == "BTC" else self.eth_network_usd

    def output(self) -> dict[str, float | str]:
        return {
            "venue_fee_pct": self.venue_fee_pct,
            "spread_pct": self.spread_pct,
            "slippage_pct": self.slippage_pct,
            "estimated_network_cost_btc_usd": self.btc_network_usd,
            "estimated_network_cost_eth_usd": self.eth_network_usd,
            "pricing_basis": "PAPER ESTIMATES ONLY; no executable quotes or live gas estimates",
        }


@dataclass(frozen=True)
class PaperRules:
    # Observe a meaningful history before entering: no artificial 1-second momentum.
    warmup_seconds: float = 190.0
    min_history_samples: int = 30
    price_window_seconds: float = 600.0
    min_rebound_pct: float = 0.20
    min_prior_drop_pct: float = 0.70
    min_positive_60s_pct: float = 0.10
    min_breakout_60s_pct: float = 0.24
    min_breakout_180s_pct: float = 0.45
    profit_trailing_arm_net_pct: float = 0.30
    trailing_pullback_pct: float = 0.20
    # In strong upward momentum, do not prematurely take profit on tiny pullbacks.
    strong_trend_60s_pct: float = 0.22
    strong_trend_180s_pct: float = 0.50
    strong_trend_trailing_multiplier: float = 2.0
    volatility_trailing_multiplier: float = 0.25
    max_trailing_pullback_pct: float = 1.50
    min_exit_net_profit_pct: float = 0.15
    # After an exit, watch a meaningful decline and confirmation of recovery.
    reentry_drop_pct: float = 0.70
    reentry_rebound_pct: float = 0.20
    risk_exit_loss_pct: float = 2.50
    cooldown_seconds: float = 90.0
    min_buy_budget_usd: float = 30.0
    min_coin_units: float = 1e-10


@dataclass
class PaperPortfolio:
    asset: str
    settlement: str
    stable_cash: float = 0.0
    coin_quantity: float = 0.0
    cost_basis: float = 0.0
    initial_equity: float | None = None
    entry_price: float | None = None
    peak_price: float | None = None
    last_exit_price: float | None = None
    post_exit_low: float | None = None
    initial_valuation_pending: bool = False
    last_trade_at: float | None = None
    last_evaluated_at: float | None = None
    last_decision: str = "INITIALIZING"
    last_reason: str = "WAITING_FOR_VERIFIED_DATA"
    realized_profit: float = 0.0
    accumulated_buy_costs: float = 0.0
    accumulated_sell_costs: float = 0.0
    trade_count: int = 0
    trade_history: deque = field(default_factory=lambda: deque(maxlen=120))


class PaperAutoEngine:
    """Public, read-only paper strategy fed by a verified MarketDataService."""

    def __init__(self, market: Any, *, costs: PaperCosts | None = None,
                 rules: PaperRules | None = None, clock: Any = time.time) -> None:
        self.market = market
        self.costs = costs or PaperCosts()
        self.rules = rules or PaperRules()
        self.clock = clock
        self._tasks: list[asyncio.Task] = []
        self.last_tick_at: float | None = None
        self.last_error: str | None = None
        # Fixed, transparent DEMO balances. Never represent user wallet funds.
        self.portfolios: dict[str, PaperPortfolio] = {
            "BTC": PaperPortfolio(asset="BTC", settlement="USDT", stable_cash=1000.0, initial_equity=1000.0),
            "ETH": PaperPortfolio(asset="ETH", settlement="USDC", coin_quantity=0.25, initial_valuation_pending=True),
        }

    async def start(self) -> None:
        if not self._tasks:
            self._tasks = [asyncio.create_task(self._run(), name="mininex-paper-auto")]

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            try:
                self.process()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {str(exc)[:150]}"
                LOGGER.exception("Paper engine tick failed")
            await asyncio.sleep(3.0)

    def _buy_quote(self, asset: str, budget: float, price: float) -> dict[str, float] | None:
        """Buy costs + exit network reserve stay inside the all-in stablecoin budget."""
        gas = self.costs.network(asset)
        reserve_exit = gas
        if budget < self.rules.min_buy_budget_usd or budget <= gas + reserve_exit:
            return None
        gross = (budget - gas - reserve_exit) / (1 + self.costs.rate)
        fee = gross * self.costs.rate
        effective_price = price * (1 + self.costs.execution_markup)
        qty = gross / effective_price
        paid = gross + fee + gas
        if not (math.isfinite(qty) and qty > self.rules.min_coin_units and paid <= budget + 1e-7):
            return None
        return {"quantity": qty, "gross": gross, "venue_fee": fee,
                "network_fee": gas, "spread_and_slippage_cost": gross - qty * price,
                "total_spent": paid, "reserved_for_future_exit_network": reserve_exit,
                "effective_unit_price": effective_price}

    def _sell_quote(self, asset: str, qty: float, price: float) -> dict[str, float] | None:
        if qty <= self.rules.min_coin_units or price <= 0:
            return None
        market_value = qty * price
        gross = market_value * (1 - self.costs.execution_markup)
        venue_fee = gross * self.costs.rate
        gas = self.costs.network(asset)
        net = gross - venue_fee - gas
        if not (math.isfinite(net) and net > 0):
            return None
        return {"market_value": market_value, "gross": gross, "venue_fee": venue_fee,
                "network_fee": gas, "spread_and_slippage_cost": market_value - gross,
                "net_proceeds": net, "effective_unit_price": gross / qty}

    @staticmethod
    def _prev_price(points: list[dict[str, Any]], cutoff: float) -> float | None:
        # Search backwards: closest sample *at or before* requested age.
        for p in reversed(points):
            if p["timestamp"] <= cutoff:
                return p["price_usdt"]
        return None

    def _signal(self, asset: str, now: float, price: float) -> dict[str, Any]:
        points = []
        for sample in self.market.history(asset, limit=3600):
            if not isinstance(sample, dict):
                continue
            try:
                timestamp = float(sample["timestamp"])
                sample_price = float(sample["price_usdt"])
            except (ValueError, TypeError, KeyError, OverflowError):
                continue
            if not (math.isfinite(sample_price) and sample_price > 0
                    and math.isfinite(timestamp)
                    and 0 <= now - timestamp <= self.rules.price_window_seconds):
                continue
            points.append({"timestamp": timestamp, "price_usdt": sample_price})
        if len(points) < self.rules.min_history_samples:
            return {"ready": False, "reason": "INSUFFICIENT_HISTORY", "samples": len(points)}
        points.sort(key=lambda p: p["timestamp"])
        if now - points[0]["timestamp"] < self.rules.warmup_seconds:
            return {"ready": False, "reason": "OBSERVING_MARKET", "samples": len(points)}
        if now - points[-1]["timestamp"] > 12.0:
            return {"ready": False, "reason": "SIGNAL_HISTORY_STALE", "samples": len(points)}
        p60 = self._prev_price(points, now - 60)
        p180 = self._prev_price(points, now - 180)
        if not p60 or not p180:
            return {"ready": False, "reason": "MOMENTUM_WINDOW_NOT_READY", "samples": len(points)}
        prices = [float(p["price_usdt"]) for p in points]
        high = max(prices)
        low = min(prices)
        mom60 = (price / p60 - 1) * 100
        mom180 = (price / p180 - 1) * 100
        fall_from_high = max(0.0, (1 - price / high) * 100)
        rebound_from_low = max(0.0, (price / low - 1) * 100)
        return {"ready": True, "reason": "OK", "samples": len(points),
                "momentum_60s_pct": clean(mom60, 4), "momentum_180s_pct": clean(mom180, 4),
                "fall_from_window_high_pct": clean(fall_from_high, 4),
                "rebound_from_window_low_pct": clean(rebound_from_low, 4),
                "window_high": clean(high), "window_low": clean(low)}

    def process(self, *, now: float | None = None) -> None:
        at = self.clock() if now is None else now
        snap = self.market.snapshot(now=at)
        for asset in SUPPORTED:
            self.process_asset(asset, snap["markets"][asset], at)
        self.last_tick_at = at

    def process_asset(self, asset: str, feed: dict[str, Any], now: float) -> None:
        p = self.portfolios[asset]
        raw = feed.get("price_usdt")
        try:
            price = float(raw) if raw is not None else None
        except (ValueError, TypeError, OverflowError):
            price = None
        if not feed.get("market_data_valid") or price is None or not math.isfinite(price) or price <= 0:
            p.last_decision = "WAIT"
            p.last_reason = "MARKET_DATA_NOT_VERIFIED:" + str(feed.get("reason") or "UNKNOWN")
            return
        if p.initial_valuation_pending:
            p.cost_basis = p.coin_quantity * price
            p.initial_equity = p.cost_basis + p.stable_cash
            p.entry_price = price
            p.peak_price = price
            p.initial_valuation_pending = False
        sig = self._signal(asset, now, price)
        if p.coin_quantity > self.rules.min_coin_units:
            p.peak_price = max(price, p.peak_price or price)
            # Emergency loss protection must NOT wait for warmup, cooldown or momentum data.
            # Risk exits may legitimately realise a loss; they are not counted as profits.
            entry_loss_pct = (1 - price / (p.entry_price or price)) * 100
            if entry_loss_pct >= self.rules.risk_exit_loss_pct:
                sale = self._sell_quote(asset, p.coin_quantity, price)
                if sale:
                    self._paper_sell(p, sale, now, price, "RISK_EXIT_MAY_REALIZE_LOSS")
                else:
                    p.last_decision, p.last_reason = "WAIT", "RISK_EXIT_QUOTE_UNECONOMIC"
                return
        # Record the actual lowest post-exit price independently of the rolling
        # window so a rebound cannot be manufactured by shifting window bounds.
        if p.coin_quantity <= self.rules.min_coin_units and p.last_exit_price is not None:
            p.post_exit_low = min(price, p.post_exit_low if p.post_exit_low is not None else price)
        if not sig["ready"]:
            p.last_decision, p.last_reason = "WAIT", sig["reason"]
            return
        if p.last_trade_at is not None and now - p.last_trade_at < self.rules.cooldown_seconds:
            p.last_decision, p.last_reason = "WAIT", "COOLDOWN"
            return
        if p.coin_quantity > self.rules.min_coin_units:
            sale = self._sell_quote(asset, p.coin_quantity, price)
            if not sale:
                p.last_decision, p.last_reason = "WAIT", "SALE_QUOTE_UNECONOMIC"
                return
            net_gain = sale["net_proceeds"] - p.cost_basis
            gain_pct = net_gain / p.cost_basis * 100 if p.cost_basis > 0 else -math.inf
            peak_drop_pct = (1 - price / (p.peak_price or price)) * 100
            enough_gain = gain_pct >= self.rules.min_exit_net_profit_pct
            peak_quote = self._sell_quote(asset, p.coin_quantity, p.peak_price or price)
            peak_net_pct = ((peak_quote["net_proceeds"] / p.cost_basis - 1) * 100
                            if peak_quote and p.cost_basis > 0 else -math.inf)
            trailing_armed = peak_net_pct >= self.rules.profit_trailing_arm_net_pct
            # Require a wider retracement in a strong uptrend and in a volatile
            # market. Do not use fixed profit targets that cut winning runs short.
            volatility_pct = ((sig["window_high"] / sig["window_low"] - 1) * 100
                              if sig["window_low"] > 0 else 0)
            strong_trend = (sig["momentum_60s_pct"] >= self.rules.strong_trend_60s_pct
                            and sig["momentum_180s_pct"] >= self.rules.strong_trend_180s_pct)
            pullback_trigger = min(
                self.rules.max_trailing_pullback_pct,
                max(self.rules.trailing_pullback_pct,
                    volatility_pct * self.rules.volatility_trailing_multiplier)
                * (self.rules.strong_trend_trailing_multiplier if strong_trend else 1.0)
            )
            reversal_confirmed = sig["momentum_60s_pct"] <= 0 or not strong_trend
            if trailing_armed and enough_gain and reversal_confirmed and peak_drop_pct >= pullback_trigger:
                self._paper_sell(p, sale, now, price, "DYNAMIC_NET_PROFIT_TRAILING")
            else:
                p.last_decision, p.last_reason = "HOLD", "TREND_ACTIVE_OR_NET_EXIT_NOT_PROFITABLE"
        else:
            # A rebound after a meaningful pullback, or a confirmed short-term breakout.
            rebound = (sig["fall_from_window_high_pct"] >= self.rules.min_prior_drop_pct
                       and sig["rebound_from_window_low_pct"] >= self.rules.min_rebound_pct
                       and sig["momentum_60s_pct"] >= self.rules.min_positive_60s_pct)
            breakout = (sig["momentum_60s_pct"] >= self.rules.min_breakout_60s_pct
                        and sig["momentum_180s_pct"] >= self.rules.min_breakout_180s_pct
                        and price >= sig["window_high"] * 0.999)
            if p.last_exit_price is not None:
                # After selling into stablecoins, buying on a falling candle is
                # prohibited: first observe a decline, then a positive rebound.
                discount = ((1 - (p.post_exit_low or price) / p.last_exit_price) * 100
                            if p.last_exit_price > 0 else 0)
                recovered = ((price / (p.post_exit_low or price) - 1) * 100
                             if (p.post_exit_low or price) > 0 else 0)
                rebound = (discount >= self.rules.reentry_drop_pct
                           and recovered >= self.rules.reentry_rebound_pct
                           and sig["momentum_60s_pct"] >= self.rules.min_positive_60s_pct)
                breakout = False
                if not rebound:
                    p.last_decision, p.last_reason = "WAIT", "AWAITING_POST_EXIT_REBOUND"
                    return
            elif not (rebound or breakout):
                p.last_decision, p.last_reason = "WAIT", "NO_CONFIRMED_REBOUND_OR_BREAKOUT"
                return
            buy = self._buy_quote(asset, p.stable_cash, price)
            if not buy:
                p.last_decision, p.last_reason = "WAIT", "BUDGET_TOO_SMALL_AFTER_ALL_COSTS"
                return
            self._paper_buy(p, buy, now, price, "REBOUND" if rebound else "BREAKOUT")

    def _paper_buy(self, p: PaperPortfolio, quote: dict[str, float], at: float, price: float, reason: str) -> None:
        if p.coin_quantity > self.rules.min_coin_units:
            raise ValueError("Only one paper spot position per asset")
        if quote["total_spent"] > p.stable_cash + 1e-7:
            raise ValueError("All-in paper budget exceeded")
        p.stable_cash -= quote["total_spent"]
        p.coin_quantity = quote["quantity"]
        p.cost_basis = quote["total_spent"]
        p.entry_price = price
        p.peak_price = price
        p.last_exit_price = None
        p.post_exit_low = None
        p.last_trade_at = at
        p.trade_count += 1
        p.accumulated_buy_costs += quote["venue_fee"] + quote["network_fee"] + quote["spread_and_slippage_cost"]
        p.last_decision, p.last_reason = "PAPER_BUY", reason
        p.trade_history.append({"at": iso(at), "kind": "PAPER_BUY", "asset": p.asset,
                                "settlement": p.settlement, "reason": reason, "price_reference": clean(price),
                                "quantity": clean(p.coin_quantity, 12), "total_spent": clean(quote["total_spent"]),
                                "estimated_costs": self._cost_breakdown(quote),
                                "cash_reserved_for_exit": clean(p.stable_cash), "real_trade": False})

    def _paper_sell(self, p: PaperPortfolio, quote: dict[str, float], at: float, price: float, reason: str) -> None:
        basis = p.cost_basis
        qty = p.coin_quantity
        profit = quote["net_proceeds"] - basis
        p.stable_cash += quote["net_proceeds"]
        p.coin_quantity = 0.0
        p.cost_basis = 0.0
        p.entry_price = None
        p.peak_price = None
        p.last_exit_price = price
        p.post_exit_low = price
        p.last_trade_at = at
        p.realized_profit += profit
        p.accumulated_sell_costs += quote["venue_fee"] + quote["network_fee"] + quote["spread_and_slippage_cost"]
        p.trade_count += 1
        p.last_decision = "PAPER_SELL"
        p.last_reason = reason
        p.trade_history.append({"at": iso(at), "kind": "PAPER_SELL", "asset": p.asset,
                                "settlement": p.settlement, "reason": reason, "price_reference": clean(price),
                                "quantity": clean(qty, 12), "net_proceeds": clean(quote["net_proceeds"]),
                                "cost_basis": clean(basis), "realized_net_profit": clean(profit),
                                "estimated_costs": self._cost_breakdown(quote), "real_trade": False})

    @staticmethod
    def _cost_breakdown(q: dict[str, float]) -> dict[str, float]:
        return {"venue_fee": clean(q["venue_fee"]), "network_fee": clean(q["network_fee"]),
                "spread_and_slippage_cost": clean(q["spread_and_slippage_cost"])}

    def status(self, asset: str | None = None) -> dict[str, Any]:
        at = self.clock()
        market_state = self.market.snapshot(now=at)
        portfolios: dict[str, dict[str, Any]] = {}
        for name in (SUPPORTED if asset is None else (asset,)):
            p = self.portfolios[name]
            feed = market_state["markets"][name]
            price = feed["price_usdt"] if feed["market_data_valid"] else None
            paper_exit = self._sell_quote(name, p.coin_quantity, price) if price and p.coin_quantity else None
            value_after_exit = (p.stable_cash + paper_exit["net_proceeds"]) if paper_exit else (p.stable_cash if p.coin_quantity == 0 else None)
            unrealized = paper_exit["net_proceeds"] - p.cost_basis if paper_exit else None
            position = "COIN" if p.coin_quantity > self.rules.min_coin_units else "STABLE"
            portfolios[name] = {
                "asset": name, "settlement": p.settlement, "paper_only": True,
                "starting_mode": "NATIVE_COINS" if name == "ETH" else "STABLECOINS",
                "position": position, "stable_cash": clean(p.stable_cash),
                "coin_quantity": clean(p.coin_quantity, 12), "cost_basis": clean(p.cost_basis),
                "entry_reference_price": clean(p.entry_price) if p.entry_price else None,
                "peak_price": clean(p.peak_price) if p.peak_price else None,
                "last_exit_price": clean(p.last_exit_price) if p.last_exit_price else None,
                "post_exit_low": clean(p.post_exit_low) if p.post_exit_low else None,
                "mark_price": price, "market_data_valid": feed["market_data_valid"],
                "mark_after_estimated_exit_costs": clean(value_after_exit) if value_after_exit is not None else None,
                "estimated_unrealized_net_pnl": clean(unrealized) if unrealized is not None else None,
                "realized_net_pnl": clean(p.realized_profit),
                "initial_demo_equity": clean(p.initial_equity) if p.initial_equity is not None else None,
                "trade_count": p.trade_count, "last_decision": p.last_decision,
                "last_reason": p.last_reason, "last_trade_at": iso(p.last_trade_at),
                "buy_costs_paid_estimate": clean(p.accumulated_buy_costs),
                "sell_costs_paid_estimate": clean(p.accumulated_sell_costs),
            }
        return {
            "app": "MiniNex", "version": "0.3.0", "mode": "PAPER_AUTO_DEMO",
            "real_trading_enabled": False, "wallet_connected": False,
            "user_balances": False, "data_persistent": False,
            "warning": "Simulated funds and fills only. Resets on each server restart.",
            "last_tick": iso(self.last_tick_at), "worker_error": self.last_error,
            "estimated_costs": self.costs.output(), "portfolios": portfolios,
        }

    def trades(self, asset: str, limit: int = 50) -> dict[str, Any]:
        if asset not in SUPPORTED:
            raise ValueError("BTC and ETH only")
        return {"asset": asset, "mode": "PAPER_AUTO_DEMO", "real_trading_enabled": False,
                "trades": list(self.portfolios[asset].trade_history)[-max(1, min(100, limit)):]}
