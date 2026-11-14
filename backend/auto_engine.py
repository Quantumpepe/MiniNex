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
    # A steady uptrend is an entry opportunity even without a single
    # unusually large one-minute breakout. These are PAPER signal filters,
    # not promises of profitability or an instruction to force a buy.
    min_trend_60s_pct: float = 0.05
    min_trend_180s_pct: float = 0.12
    min_trend_near_high_fraction: float = 0.997
    min_graduated_trend_600s_pct: float = 0.15
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
    # -2.5% triggers a risk review, not an automatic exit.
    risk_exit_loss_pct: float = 2.50
    bearish_exit_loss_pct: float = 3.50
    emergency_exit_loss_pct: float = 6.00
    max_additions: int = 3
    min_add_discount_pct: float = 0.60
    cooldown_seconds: float = 90.0
    min_buy_budget_usd: float = 30.0
    min_coin_units: float = 1e-10
    early_rebound_prior_drop_pct: float = 0.25
    early_rebound_recovery_pct: float = 0.10
    early_rebound_60s_pct: float = 0.035
    spot_short_decline_60s_pct: float = -0.08
    spot_short_decline_180s_pct: float = -0.18


@dataclass
class PaperPortfolio:
    asset: str
    settlement: str
    strategy_mode: str = "LONG"
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
    last_signal_summary: str = ""
    last_sold_quantity: float | None = None
    last_buy_price: float | None = None
    add_count: int = 0
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
        p600 = self._prev_price(points, now - 600)
        if not p60 or not p180:
            return {"ready": False, "reason": "MOMENTUM_WINDOW_NOT_READY", "samples": len(points)}
        prices = [float(p["price_usdt"]) for p in points]
        high = max(prices)
        low = min(prices)
        # Use actual chronological extremes to measure a prior pullback:
        # "current price below high" alone misses a completed rebound.
        running_high = prices[0]
        prior_max_drawdown_pct = 0.0
        for observed in prices[1:]:
            prior_max_drawdown_pct = max(
                prior_max_drawdown_pct,
                (1 - observed / running_high) * 100,
            )
            running_high = max(running_high, observed)
        # The last five observed prices smooth transient WS noise.
        recent = prices[-min(5,len(prices)):]
        recent_mean = sum(recent) / len(recent)
        local_window = [p["price_usdt"] for p in points
                        if now - p["timestamp"] <= min(self.rules.price_window_seconds,900)]
        local_high = max(local_window) if local_window else high
        local_mean = sum(local_window)/len(local_window) if local_window else price
        short_window = [p["price_usdt"] for p in points if now-p["timestamp"] <= 240]
        short_low = min(short_window) if short_window else low
        short_high = max(short_window) if short_window else high
        mom600 = (price / p600 - 1) * 100 if p600 else None
        mom60 = (price / p60 - 1) * 100
        mom180 = (price / p180 - 1) * 100
        fall_from_high = max(0.0, (1 - price / high) * 100)
        rebound_from_low = max(0.0, (price / low - 1) * 100)
        return {"ready": True, "reason": "OK", "samples": len(points),
                "momentum_60s_pct": clean(mom60, 4), "momentum_180s_pct": clean(mom180, 4),
                "fall_from_window_high_pct": clean(fall_from_high, 4),
                "rebound_from_window_low_pct": clean(rebound_from_low, 4),
                "prior_max_drawdown_pct": clean(prior_max_drawdown_pct, 4),
                "recent_mean_price": clean(recent_mean),
                "local_high": clean(local_high),
                "short_window_low": clean(short_low),
                "short_window_high": clean(short_high),
                "local_mean": clean(local_mean),
                "momentum_600s_pct": clean(mom600,4) if mom600 is not None else None,
                "window_high": clean(high), "window_low": clean(low)}

    def process(self, *, now: float | None = None) -> None:
        at = self.clock() if now is None else now
        snap = self.market.snapshot(now=at)
        for asset in SUPPORTED:
            self.process_asset(asset, snap["markets"][asset], at)
        self.last_tick_at = at

    def _strong_uptrend(self, sig: dict[str, Any], price: float) -> bool:
        """Persistent positive momentum near the verified high, not a single spike."""
        fast = (sig.get("momentum_60s_pct", -math.inf) >= self.rules.strong_trend_60s_pct
                and sig.get("momentum_180s_pct", -math.inf) >= self.rules.strong_trend_180s_pct)
        # A steady 10-minute rise can also warrant full allocation; it need
        # not contain an explosive one-minute candle.
        sustained = (sig.get("momentum_600s_pct") is not None
                     and sig["momentum_600s_pct"] >= 0.40
                     and sig.get("momentum_180s_pct", -math.inf) >= 0.15
                     and sig.get("momentum_60s_pct", -math.inf) >= 0.05)
        return (sig.get("ready") is True
                and (fast or sustained)
                and price >= sig.get("window_high", math.inf) * 0.997
                and price >= sig.get("recent_mean_price", price))

    def _deployment_target(self, sig: dict[str, Any], price: float,
                           reason: str, *, adding: bool = False,
                           additions_done: int = 0) -> float:
        """Risk-aware LONG exposure. Reserve cash for confirmed subsequent adds.

        A strong 1m/3m candle alone must not bypass the volatility reserve
        and turn the entire 5,000 USD PAPER account into a coin position.
        This affects position sizing only, not profit trailing or spot-short.
        """
        m60 = max(0.0, float(sig.get("momentum_60s_pct") or 0.0))
        m180 = max(0.0, float(sig.get("momentum_180s_pct") or 0.0))
        m600 = max(0.0, float(sig.get("momentum_600s_pct") or 0.0))
        strength = (0.40 * min(1.0, m60 / 0.30)
                    + 0.40 * min(1.0, m180 / 0.60)
                    + 0.20 * min(1.0, m600 / 0.80))
        high = float(sig.get("window_high") or price)
        low = float(sig.get("window_low") or price)
        volatility = (high / low - 1) * 100 if low > 0 and high >= low else 0.0
        volatility_reserve = min(0.12, max(0.0, volatility - 0.8) * 0.02)

        if self._strong_uptrend(sig, price):
            # Still deploy aggressively into sustained momentum, but do not
            # mistake short-only 5m evidence for a fully confirmed trend.
            target = 0.86 + 0.12 * strength - volatility_reserve
            if sig.get("momentum_600s_pct") is None:
                target -= 0.03
        else:
            target = 0.34 + 0.66 * strength - volatility_reserve

        if not adding:
            return min(0.93 - 0.40 * volatility_reserve,
                       max(0.25, target))

        # Release reserves progressively, only after a confirmed ADD_TREND or
        # ADD_REBOUND. Higher observed volatility keeps a larger buffer even
        # after several additions. No hardcoded fixed profit thresholds.
        stage = min(max(0, additions_done), 2)
        floor = (0.90, 0.94, 0.98)[stage] - volatility_reserve
        ceiling = (0.94, 0.97, 0.995)[stage] - 0.40 * volatility_reserve
        target += 0.07 if reason == "ADD_TREND" else 0.17
        return min(ceiling, max(0.25, floor, target))

    def _trend_add_has_room(self, sig: dict[str, Any], price: float,
                            anchor: float) -> bool:
        """Do not repeatedly buy the same rally at essentially one price.

        Momentum must advance from the previous buy enough to clear estimated
        spot execution friction or a volatility-sensitive step, whichever is
        larger. This preserves cash for a genuine rebound or a new leg.
        """
        if not math.isfinite(anchor) or anchor <= 0 or price <= anchor:
            return False
        high = float(sig.get("window_high") or price)
        low = float(sig.get("window_low") or price)
        volatility = (high / low - 1) * 100 if low > 0 and high >= low else 0.0
        estimated_roundtrip_friction = (
            self.costs.rate + 2 * self.costs.execution_markup) * 100
        required_advance = max(estimated_roundtrip_friction,
                               0.10 * volatility)
        return (price / anchor - 1) * 100 >= required_advance

    def _allocation_budget(self, p: PaperPortfolio, price: float, target: float) -> float:
        """All-in trade allowance, never exceeds available simulated stable cash."""
        if not (math.isfinite(target) and 0 < target <= 1 and price > 0):
            return 0.0
        held = max(0.0, p.coin_quantity * price)
        desired = max(0.0, target * (p.stable_cash + held) - held)
        return min(p.stable_cash, desired) if desired >= self.rules.min_buy_budget_usd else 0.0

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
        # Durable, compact reason diagnostics for the user's OWN session.
        # Never claim an entry was possible without actual momentum inputs.
        if sig.get("ready"):
            def show(value: Any) -> str:
                return f"{value:+.3f}%" if type(value) in (float,int) else "n/a"
            p.last_signal_summary=(
                "1m "+show(sig.get("momentum_60s_pct"))
                +" | 3m "+show(sig.get("momentum_180s_pct"))
                +" | 10m "+show(sig.get("momentum_600s_pct"))
                +" | rebound "+show(sig.get("rebound_from_window_low_pct"))
            )
        else:
            p.last_signal_summary=str(sig.get("reason","UNAVAILABLE"))
        if p.coin_quantity > self.rules.min_coin_units:
            p.peak_price = max(price, p.peak_price or price)
            loss_pct = (1 - price / (p.entry_price or price)) * 100
            bearish = (sig.get("ready") is True
                       and sig.get("momentum_60s_pct", 0) <= -0.30
                       and sig.get("momentum_180s_pct", 0) <= -0.70
                       and price < sig.get("recent_mean_price", price))
            emergency = loss_pct >= self.rules.emergency_exit_loss_pct
            breakdown = loss_pct >= self.rules.bearish_exit_loss_pct and bearish
            # Do not sell blindly at -2.5%. A severe breakdown still overrides
            # cooldown; an emergency price gap does not wait for fresh history.
            if emergency or breakdown:
                sale = self._sell_quote(asset, p.coin_quantity, price)
                if sale:
                    self._paper_sell(p, sale, now, price,
                                     "RISK_EMERGENCY_EXIT" if emergency else "RISK_TREND_BREAKDOWN")
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
            # A held LONG position may be topped up only on a confirmed
            # recovery from below the last buy, or a verified strong uptrend.
            if p.strategy_mode in ("LONG", "AUTO"):
                strong = self._strong_uptrend(sig, price)
                anchor = p.last_buy_price or p.entry_price or price
                discounted = (1 - price / anchor) * 100 >= self.rules.min_add_discount_pct
                rebound = (discounted
                           and sig.get("momentum_60s_pct", -math.inf) >= self.rules.early_rebound_60s_pct
                           and sig.get("rebound_from_window_low_pct", 0) >= self.rules.early_rebound_recovery_pct
                           and sig.get("momentum_180s_pct", -math.inf) > -1.0
                           and price >= sig.get("recent_mean_price", price))
                # A prior strong signal cannot spend all remaining cash on
                # identical-price rechecks every 90 seconds.
                trend_add = strong and self._trend_add_has_room(sig, price, anchor)
                add_reason = "ADD_TREND" if trend_add else "ADD_REBOUND" if rebound else None
            else:
                add_reason = None
            # Spot-downtrend is SELL OWNED COINS / REBUY, never leveraged short.
            # Do not apply long-only profit-trailing thresholds to this mode.
            if p.strategy_mode == "SPOT_SHORT":
                bearish = (
                    sig["momentum_60s_pct"] <= self.rules.spot_short_decline_60s_pct
                    and sig["momentum_180s_pct"] <= self.rules.spot_short_decline_180s_pct
                    and price < sig.get("recent_mean_price", price)
                )
                if bearish:
                    self._paper_sell(p,sale,now,price,"SPOT_DOWNTREND_EXIT")
                else:
                    p.last_decision,p.last_reason="HOLD","SPOT_AWAITING_DOWNTREND"
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
                return
            # AUTO combines the LONG/DCA path with defensive spot exits.
            # Sell only coins already held, never borrow or create leverage.
            # Avoid churn on tiny dips: require a confirmed falling trend,
            # a meaningful retreat from the held peak and nonnegative net
            # proceeds after the hypothetical venue/network costs.
            if p.strategy_mode == "AUTO":
                auto_bearish = (
                    sig.get("ready") is True
                    and sig["momentum_60s_pct"] <= -0.30
                    and sig["momentum_180s_pct"] <= -0.70
                    and price < sig.get("recent_mean_price", price)
                )
                if auto_bearish and peak_drop_pct >= 0.40 and net_gain >= 0:
                    self._paper_sell(p, sale, now, price, "AUTO_DOWNTREND_EXIT")
                    return
            if add_reason and p.add_count < self.rules.max_additions:
                budget = self._allocation_budget(
                    p, price, self._deployment_target(
                        sig, price, add_reason, adding=True,
                        additions_done=p.add_count))
                buy = self._buy_quote(asset, budget, price)
                if buy:
                    self._paper_buy(p, buy, now, price, add_reason)
                    return
            p.last_decision, p.last_reason = "HOLD", "TREND_ACTIVE_OR_NET_EXIT_NOT_PROFITABLE"
        else:
            # A rebound after a meaningful pullback, or a confirmed short-term breakout.
            rebound = (sig.get("prior_max_drawdown_pct",
                               sig["fall_from_window_high_pct"]) >= self.rules.min_prior_drop_pct
                       and sig["rebound_from_window_low_pct"] >= self.rules.min_rebound_pct
                       and sig["momentum_60s_pct"] >= self.rules.min_positive_60s_pct)
            # Fast local recovery: a 15m high may be far away even when
            # the market has already reversed from the latest short-term low.
            short_high=sig.get("short_window_high",sig["window_high"])
            short_low=sig.get("short_window_low",sig["window_low"])
            recent_drop=((1-short_low/short_high)*100 if short_high>0 else 0)
            recent_recovery=((price/short_low-1)*100 if short_low>0 else 0)
            early_rebound=(
                recent_drop >= self.rules.early_rebound_prior_drop_pct
                and recent_recovery >= self.rules.early_rebound_recovery_pct
                and sig["momentum_60s_pct"] >= self.rules.early_rebound_60s_pct
                and price >= sig.get("recent_mean_price",price)
            )
            rebound=rebound or early_rebound
            breakout = (sig["momentum_60s_pct"] >= self.rules.min_breakout_60s_pct
                        and sig["momentum_180s_pct"] >= self.rules.min_breakout_180s_pct
                        and price >= sig["window_high"] * 0.999)
            trend_continuation = (
                sig["momentum_60s_pct"] >= self.rules.min_trend_60s_pct
                and sig["momentum_180s_pct"] >= self.rules.min_trend_180s_pct
                and price >= sig["window_high"] * self.rules.min_trend_near_high_fraction
                and price >= sig.get("recent_mean_price", price)
            )
            # Gradual rise over ten minutes: the old 60s/180s thresholds
            # excluded quiet but persistent uptrends. A flat/downward
            # 60s candle alone cannot qualify; recent support must hold.
            gradual_trend = (
                sig.get("momentum_600s_pct") is not None
                and sig["momentum_600s_pct"] >= self.rules.min_graduated_trend_600s_pct
                and sig["momentum_180s_pct"] >= 0.0
                and sig["momentum_60s_pct"] >= 0.0
                and price >= sig.get("local_mean",price)
                and price >= sig.get("local_high",price)*0.995
            )
            if p.last_exit_price is not None:
                # After selling into stablecoins, buying on a falling candle is
                # prohibited: first observe a decline, then a positive rebound.
                discount = ((1 - (p.post_exit_low or price) / p.last_exit_price) * 100
                            if p.last_exit_price > 0 else 0)
                recovered = ((price / (p.post_exit_low or price) - 1) * 100
                             if (p.post_exit_low or price) > 0 else 0)
                # A spot-downtrend cycle may rebuy earlier, but only if the
                # actual current buy quote can acquire MORE BTC/ETH than sold.
                # This inherently accounts for estimated fees, slippage and
                # the remaining stablecoin budget.
                recovery_ok = (
                    recovered >= self.rules.early_rebound_recovery_pct
                    and sig["momentum_60s_pct"] >= self.rules.early_rebound_60s_pct
                ) if p.strategy_mode in ("SPOT_SHORT", "AUTO") else (
                    recovered >= self.rules.reentry_rebound_pct
                    and sig["momentum_60s_pct"] >= self.rules.min_positive_60s_pct
                )
                rebound = discount >= (
                    self.rules.early_rebound_prior_drop_pct
                    if p.strategy_mode in ("SPOT_SHORT", "AUTO") else self.rules.reentry_drop_pct
                ) and recovery_ok
                if rebound and p.strategy_mode == "SPOT_SHORT":
                    proposed=self._buy_quote(asset,p.stable_cash,price)
                    if not proposed or (
                        p.last_sold_quantity is not None
                        and proposed["quantity"] <= p.last_sold_quantity
                    ):
                        rebound=False
                # Allow confirmed strong breakouts even after a prior exit;
                # SPOT_SHORT always requires a cheaper, cost-aware rebuy.
                breakout = (p.strategy_mode in ("LONG", "AUTO")
                            and self._strong_uptrend(sig, price)
                            and price >= p.last_exit_price * 1.003)
                trend_continuation = False
                gradual_trend = False
                if not (rebound or breakout):
                    p.last_decision, p.last_reason = "WAIT", "AWAITING_POST_EXIT_REBOUND"
                    return
            elif not (rebound or breakout or trend_continuation or gradual_trend):
                p.last_decision, p.last_reason = "WAIT", "NO_CONFIRMED_ENTRY_SIGNAL"
                return
            reason = ("REBOUND" if rebound else "BREAKOUT" if breakout
                      else "GRADUAL_TREND" if gradual_trend else "TREND_CONTINUATION")
            # SPOT_SHORT rebuy remains all-in to compare purchased coin quantity
            # with the quantity sold; LONG uses dynamic reserve allocation.
            target = (1.0 if p.strategy_mode == "SPOT_SHORT"
                      else self._deployment_target(sig, price, reason))
            buy = self._buy_quote(asset, self._allocation_budget(p, price, target), price)
            if not buy:
                p.last_decision, p.last_reason = "WAIT", "BUDGET_TOO_SMALL_AFTER_ALL_COSTS"
                return
            self._paper_buy(p, buy, now, price, reason)

    def _paper_buy(self, p: PaperPortfolio, quote: dict[str, float], at: float,
                   price: float, reason: str) -> None:
        adding = p.coin_quantity > self.rules.min_coin_units
        if adding and (p.strategy_mode not in ("LONG", "AUTO") or p.add_count >= self.rules.max_additions):
            raise ValueError("Paper position addition not permitted")
        if quote["total_spent"] > p.stable_cash + 1e-7:
            raise ValueError("All-in paper budget exceeded")
        old_qty = p.coin_quantity
        new_qty = old_qty + quote["quantity"]
        old_reference = p.entry_price or price
        p.stable_cash -= quote["total_spent"]
        p.coin_quantity = new_qty
        p.cost_basis += quote["total_spent"]
        # Quantity-weighted reference: all-in cost basis includes buy fees.
        p.entry_price = (old_qty * old_reference + quote["quantity"] * price) / new_qty
        p.peak_price = price
        p.last_buy_price = price
        if adding:
            p.add_count += 1
        else:
            p.add_count = 0
            p.last_exit_price = None
            p.post_exit_low = None
            p.last_sold_quantity = None
        p.last_trade_at = at
        p.trade_count += 1
        p.accumulated_buy_costs += quote["venue_fee"] + quote["network_fee"] + quote["spread_and_slippage_cost"]
        p.last_decision, p.last_reason = "PAPER_BUY", reason
        p.trade_history.append({"at": iso(at), "kind": "PAPER_BUY", "asset": p.asset,
                                "settlement": p.settlement, "reason": reason,
                                "price_reference": clean(price),
                                "quantity": clean(quote["quantity"], 12),
                                "position_quantity": clean(p.coin_quantity, 12),
                                "total_spent": clean(quote["total_spent"]),
                                "position_cost_basis": clean(p.cost_basis),
                                "estimated_costs": self._cost_breakdown(quote),
                                "cash_reserved_for_exit": clean(p.stable_cash), "real_trade": False})

    def _paper_sell(self, p: PaperPortfolio, quote: dict[str, float], at: float, price: float, reason: str) -> None:
        basis = p.cost_basis
        qty = p.coin_quantity
        p.last_sold_quantity=qty
        profit = quote["net_proceeds"] - basis
        p.stable_cash += quote["net_proceeds"]
        p.coin_quantity = 0.0
        p.cost_basis = 0.0
        p.last_buy_price = None
        p.add_count = 0
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
