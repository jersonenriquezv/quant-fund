#!/usr/bin/env python3
"""
Backtester — Replay historical candles through StrategyService + simulate fills.

Loads candles from PostgreSQL, replays candle-by-candle, detects setups via
StrategyService, simulates entry/SL/TP fills, and produces performance metrics.

Usage:
    python scripts/backtest.py --days 60 --profile aggressive
    python scripts/backtest.py --days 90 --capital 10000
    python scripts/backtest.py --pair BTC/USDT --profile aggressive --verbose
    python scripts/backtest.py --days 60 --csv
"""

import argparse
import csv
import json
import math
import re
import sys
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from unittest.mock import patch

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_service.data_store import PostgresStore
from shared.models import Candle, TradeSetup, FundingRate, OpenInterest, MarketSnapshot
from strategy_service.service import StrategyService
from shared.logger import setup_logger
from config.settings import settings, QUICK_SETUP_TYPES

logger = setup_logger("backtest")


# ================================================================
# BacktestDataService — mock DataService for replay
# ================================================================

class BacktestDataService:
    """Mock DataService that serves candles up to a simulated time cursor.

    Implements get_candles() and get_market_snapshot() — the only two
    methods StrategyService.evaluate() calls on the data service.

    Now includes historical funding rates and OI from PostgreSQL for
    realistic MarketSnapshot during backtests.
    """

    def __init__(self):
        # {(pair, timeframe): [Candle, ...]} sorted oldest-first
        self._candles: dict[tuple[str, str], list[Candle]] = {}
        self._current_time_ms: int = 0
        # Historical funding rates and OI per pair, sorted oldest-first
        self._funding: dict[str, list[FundingRate]] = {}
        self._oi: dict[str, list[OpenInterest]] = {}

    def load_from_postgres(self, pg: PostgresStore, pairs: list[str],
                           timeframes: list[str], count: int = 50000):
        """Load historical candles, funding rates, and OI from PostgreSQL."""
        for pair in pairs:
            for tf in timeframes:
                candles = pg.load_candles(pair, tf, count)
                if candles:
                    self._candles[(pair, tf)] = candles
                    logger.info(f"Loaded {len(candles)} candles: {pair} {tf} "
                                f"[{_ts_to_str(candles[0].timestamp)} -> "
                                f"{_ts_to_str(candles[-1].timestamp)}]")
                else:
                    logger.warning(f"No candles in DB: {pair} {tf}")

            # Load historical funding rates
            funding = pg.load_funding_rates(pair)
            if funding:
                self._funding[pair] = funding
                logger.info(f"Loaded {len(funding)} funding rates: {pair}")

            # Load historical OI
            oi = pg.load_open_interest(pair)
            if oi:
                self._oi[pair] = oi
                logger.info(f"Loaded {len(oi)} OI snapshots: {pair}")

    def set_time(self, time_ms: int):
        self._current_time_ms = time_ms

    def get_candles(self, pair: str, timeframe: str,
                    count: int = 100) -> list[Candle]:
        """Return last `count` candles with timestamp <= current cursor."""
        all_candles = self._candles.get((pair, timeframe), [])
        visible = [c for c in all_candles if c.timestamp <= self._current_time_ms]
        return visible[-count:]

    def _find_nearest(self, records: list, time_ms: int):
        """Binary search for the most recent record at or before time_ms."""
        if not records:
            return None
        lo, hi = 0, len(records) - 1
        result = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if records[mid].timestamp <= time_ms:
                result = records[mid]
                lo = mid + 1
            else:
                hi = mid - 1
        return result

    def get_market_snapshot(self, pair: str) -> MarketSnapshot:
        """Return MarketSnapshot with historical funding + OI at current time."""
        funding = self._find_nearest(
            self._funding.get(pair, []), self._current_time_ms
        )
        oi = self._find_nearest(
            self._oi.get(pair, []), self._current_time_ms
        )
        return MarketSnapshot(
            pair=pair,
            timestamp=self._current_time_ms,
            funding=funding,
            oi=oi,
        )

    def get_trigger_candles(self, pair: str,
                            ltf_timeframes: list[str]) -> list[Candle]:
        """Get all LTF candles sorted chronologically for simulation."""
        candles = []
        for tf in ltf_timeframes:
            candles.extend(self._candles.get((pair, tf), []))
        candles.sort(key=lambda c: (c.timestamp, c.timeframe))
        return candles


# ================================================================
# SimulatedClock — patches time.time() for OB/FVG expiration
# ================================================================

class SimulatedClock:
    """Callable that replaces time.time() during backtest."""

    def __init__(self):
        self._time_s: float = 0.0

    def set_ms(self, time_ms: int):
        self._time_s = time_ms / 1000.0

    def __call__(self) -> float:
        return self._time_s


# ================================================================
# RejectTracker — captures rejection reasons from loguru
# ================================================================

class RejectTracker:
    """Loguru sink that categorizes strategy rejection reasons."""

    PATTERNS = [
        (r"No HTF bias", "no_htf_bias"),
        (r"Setup A.*no recent sweeps", "setup_a_no_sweeps"),
        (r"Setup A.*no CHoCH", "setup_a_no_choch"),
        (r"Setup A.*CHoCH.*!= HTF", "setup_a_choch_htf_mismatch"),
        (r"Setup A.*no aligned sweep before", "setup_a_no_aligned_sweep"),
        (r"Setup A.*PD misaligned", "setup_a_pd_misaligned"),
        (r"Setup A.*no aligned OBs", "setup_a_no_obs"),
        (r"Setup A.*no OBs within range", "setup_a_ob_out_of_range"),
        (r"Setup A.*insufficient confluences", "setup_a_low_confluences"),
        (r"Setup A.*R:R too low", "setup_a_rr_too_low"),
        (r"Setup B.*no BOS", "setup_b_no_bos"),
        (r"Setup B.*BOS.*!= HTF", "setup_b_bos_htf_mismatch"),
        (r"Setup B.*PD misaligned", "setup_b_pd_misaligned"),
        (r"Setup B.*no aligned OBs", "setup_b_no_obs"),
        (r"Setup B.*no aligned FVGs", "setup_b_no_fvgs"),
        (r"Setup B.*no adjacent OB\+FVG", "setup_b_no_ob_fvg_pair"),
    ]

    def __init__(self):
        self.counts: dict[str, int] = {}
        self.last_reasons: list[str] = []
        self._compiled = [(re.compile(p), cat) for p, cat in self.PATTERNS]

    def sink(self, message):
        text = str(message)
        for regex, category in self._compiled:
            if regex.search(text):
                self.counts[category] = self.counts.get(category, 0) + 1
                self.last_reasons.append(category)
                return

    def reset_last(self):
        self.last_reasons.clear()


# ================================================================
# SimulatedTrade — tracks a single trade through its lifecycle
# ================================================================

@dataclass
class SimulatedTrade:
    """A trade being simulated through candle replay.

    Exit management (matches live execution):
    - SL at sl_price for 100% of position
    - Single TP at tp2_price (2:1 R:R) for 100% close
    - Breakeven: price crosses tp1_price (1:1) → SL moves to entry
    - Trailing: price crosses midpoint(tp1,tp2) (1.5:1) → SL moves to tp1
    """

    # Setup identity
    pair: str
    direction: str              # "long" or "short"
    setup_type: str

    # Target prices (from TradeSetup)
    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: float

    # Sizing
    position_size: float        # base currency
    leverage: float

    # State tracking
    phase: str = "pending"      # "pending" -> "active" -> "closed"
    current_sl: float = 0.0     # Tracks SL moves (breakeven, trailing)
    breakeven_hit: bool = False
    trailing_sl_moved: bool = False

    # Timing (ms)
    setup_time_ms: int = 0
    entry_deadline_ms: int = 0
    entry_time_ms: int = 0
    close_time_ms: int = 0

    # Exit info
    exit_price: float = 0.0
    pnl_usd: float = 0.0
    exit_reason: str = ""


# ================================================================
# TradeSimulator — fill simulation engine
# ================================================================

class TradeSimulator:
    """Simulates entry fills, SL, TPs, and timeouts candle-by-candle.

    Applies the same risk guardrails as the live RiskService:
    min_risk_distance, R:R check, cooldown, max_trades_per_day,
    daily/weekly drawdown limits.
    """

    def __init__(self, initial_capital: float):
        self.initial_capital: float = initial_capital
        self.equity: float = initial_capital
        self.pending: list[SimulatedTrade] = []
        self.active: list[SimulatedTrade] = []
        self.closed: list[SimulatedTrade] = []
        # (timestamp_ms, equity) — for drawdown calculation
        self.equity_curve: list[tuple[int, float]] = [(0, initial_capital)]

        # Risk state tracking (mirrors RiskStateTracker)
        self._last_loss_time_ms: int | None = None
        self._trades_today: int = 0
        self._current_day: str = ""
        self._daily_pnl: float = 0.0
        self._weekly_pnl: float = 0.0
        self._current_week: int = -1  # ISO week number
        self.risk_rejections: dict[str, int] = {}

    def _reject(self, reason: str) -> bool:
        """Record a risk rejection and return False."""
        key = reason.split(":")[0].split("(")[0].strip()
        self.risk_rejections[key] = self.risk_rejections.get(key, 0) + 1
        return False

    def _update_day_week(self, ts_ms: int) -> None:
        """Reset daily/weekly counters on new day/week."""
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        day_str = dt.strftime("%Y-%m-%d")
        week_num = dt.isocalendar()[1]

        if day_str != self._current_day:
            self._trades_today = 0
            self._daily_pnl = 0.0
            self._current_day = day_str

        if week_num != self._current_week:
            self._weekly_pnl = 0.0
            self._current_week = week_num

    def on_setup(self, setup: TradeSetup, candle: Candle) -> bool:
        """Accept a new setup. Returns True if trade was created."""
        self._update_day_week(candle.timestamp)

        # --- Risk guardrails (same as live RiskService) ---

        # Max open positions
        open_count = len(self.pending) + len(self.active)
        if open_count >= settings.MAX_OPEN_POSITIONS:
            return self._reject("Max open positions")

        # Skip if equity depleted
        if self.equity <= 0:
            return False

        # Min risk distance
        distance = abs(setup.entry_price - setup.sl_price)
        if distance == 0:
            return self._reject("Zero risk distance")
        risk_pct = distance / setup.entry_price
        if risk_pct < settings.MIN_RISK_DISTANCE_PCT:
            return self._reject("SL too close to entry")

        # R:R check
        reward = abs(setup.tp2_price - setup.entry_price)
        rr = reward / distance if distance > 0 else 0
        min_rr = (settings.MIN_RISK_REWARD_QUICK
                  if setup.setup_type in QUICK_SETUP_TYPES
                  else settings.MIN_RISK_REWARD)
        if rr < min_rr:
            return self._reject(f"R:R {rr:.2f} below {min_rr}")

        # Cooldown after loss
        if self._last_loss_time_ms is not None:
            elapsed_min = (candle.timestamp - self._last_loss_time_ms) / (1000 * 60)
            if elapsed_min < settings.COOLDOWN_MINUTES:
                return self._reject("Cooldown after loss")

        # Max trades per day
        if self._trades_today >= settings.MAX_TRADES_PER_DAY:
            return self._reject("Max trades per day")

        # Daily drawdown
        if self.initial_capital > 0:
            daily_dd = abs(self._daily_pnl) / self.initial_capital if self._daily_pnl < 0 else 0.0
            if daily_dd >= settings.MAX_DAILY_DRAWDOWN:
                return self._reject("Daily drawdown limit")

        # Weekly drawdown
        if self.initial_capital > 0:
            weekly_dd = abs(self._weekly_pnl) / self.initial_capital if self._weekly_pnl < 0 else 0.0
            if weekly_dd >= settings.MAX_WEEKLY_DRAWDOWN:
                return self._reject("Weekly drawdown limit")

        # --- Position sizing ---
        risk_amount = self.equity * settings.RISK_PER_TRADE
        position_size = risk_amount / distance
        notional = position_size * setup.entry_price
        leverage = notional / self.equity

        # Cap at MAX_LEVERAGE
        if leverage > settings.MAX_LEVERAGE:
            leverage = float(settings.MAX_LEVERAGE)
            notional = self.equity * leverage
            position_size = notional / setup.entry_price

        # Entry timeout
        if setup.setup_type in QUICK_SETUP_TYPES:
            timeout_ms = settings.ENTRY_TIMEOUT_QUICK_SECONDS * 1000
        else:
            timeout_ms = settings.ENTRY_TIMEOUT_SECONDS * 1000

        self._trades_today += 1

        trade = SimulatedTrade(
            pair=setup.pair,
            direction=setup.direction,
            setup_type=setup.setup_type,
            entry_price=setup.entry_price,
            sl_price=setup.sl_price,
            tp1_price=setup.tp1_price,
            tp2_price=setup.tp2_price,
            position_size=position_size,
            leverage=leverage,
            current_sl=setup.sl_price,
            setup_time_ms=candle.timestamp,
            entry_deadline_ms=candle.timestamp + timeout_ms,
        )
        self.pending.append(trade)
        return True

    def on_candle(self, candle: Candle) -> None:
        """Process one candle: check pending entries + active trade fills."""
        self._process_pending(candle)
        self._process_active(candle)

    def _process_pending(self, candle: Candle) -> None:
        """Check if pending entries fill or expire."""
        still_pending = []
        for trade in self.pending:
            if trade.pair != candle.pair:
                still_pending.append(trade)
                continue

            # Entry timeout
            if candle.timestamp > trade.entry_deadline_ms:
                trade.phase = "closed"
                trade.exit_reason = "entry_timeout"
                trade.close_time_ms = candle.timestamp
                self.closed.append(trade)
                continue

            # Entry fill check
            filled = False
            if trade.direction == "long":
                # Buy limit: fills when price drops to entry
                filled = candle.low <= trade.entry_price
            else:
                # Sell limit: fills when price rises to entry
                filled = candle.high >= trade.entry_price

            if filled:
                trade.phase = "active"
                trade.entry_time_ms = candle.timestamp
                self.active.append(trade)
            else:
                still_pending.append(trade)

        self.pending = still_pending

    def _process_active(self, candle: Candle) -> None:
        """Check SL, TP, breakeven, trailing, and timeout for active trades.

        Exit management (matches live execution):
        1. SL check first (always priority)
        2. TP hit = price reaches tp2 → close 100% (reason="tp")
        3. Breakeven: price crosses tp1 → SL moves to entry
        4. Trailing: price crosses midpoint(tp1,tp2) → SL moves to tp1
        """
        still_active = []
        for trade in self.active:
            if trade.pair != candle.pair:
                still_active.append(trade)
                continue

            # Timeout check
            if trade.setup_type in QUICK_SETUP_TYPES:
                max_duration_ms = settings.MAX_TRADE_DURATION_QUICK * 1000
            else:
                max_duration_ms = settings.MAX_TRADE_DURATION_SECONDS * 1000

            duration_ms = candle.timestamp - trade.entry_time_ms
            if duration_ms >= max_duration_ms:
                self._close_trade(trade, candle.close, "timeout", candle.timestamp)
                self.closed.append(trade)
                continue

            # SL check (priority — always first)
            sl_hit = False
            if trade.direction == "long":
                sl_hit = candle.low <= trade.current_sl
            else:
                sl_hit = candle.high >= trade.current_sl

            if sl_hit:
                if trade.trailing_sl_moved:
                    reason = "trailing_sl"
                elif trade.breakeven_hit:
                    reason = "breakeven_sl"
                else:
                    reason = "sl"
                self._close_trade(trade, trade.current_sl, reason, candle.timestamp)
                self.closed.append(trade)
                continue

            # TP check — single TP at tp2 (100% close)
            tp_hit = self._price_reached(trade, candle, trade.tp2_price)
            if tp_hit:
                self._close_trade(trade, trade.tp2_price, "tp", candle.timestamp)
                self.closed.append(trade)
                continue

            # Breakeven: price crosses tp1 → SL moves to entry
            if not trade.breakeven_hit:
                if self._price_reached(trade, candle, trade.tp1_price):
                    trade.current_sl = trade.entry_price
                    trade.breakeven_hit = True

            # Trailing: price crosses midpoint(tp1,tp2) → SL moves to tp1
            if trade.breakeven_hit and not trade.trailing_sl_moved:
                midpoint = (trade.tp1_price + trade.tp2_price) / 2.0
                if self._price_reached(trade, candle, midpoint):
                    trade.current_sl = trade.tp1_price
                    trade.trailing_sl_moved = True

            # Still active
            still_active.append(trade)

        self.active = still_active

    def _price_reached(self, trade: SimulatedTrade, candle: Candle,
                       target: float) -> bool:
        """Check if target price was reached within this candle."""
        if trade.direction == "long":
            return candle.high >= target
        else:
            return candle.low <= target

    def _close_trade(self, trade: SimulatedTrade, price: float,
                     reason: str, timestamp_ms: int) -> None:
        """Close entire position at given price."""
        trade.phase = "closed"
        trade.exit_price = price
        trade.close_time_ms = timestamp_ms
        trade.exit_reason = reason

        # Compute PnL
        if trade.direction == "long":
            trade.pnl_usd = (price - trade.entry_price) * trade.position_size
        else:
            trade.pnl_usd = (trade.entry_price - price) * trade.position_size

        # Update equity
        self.equity += trade.pnl_usd
        self.equity_curve.append((timestamp_ms, self.equity))

        # Update risk state
        self._daily_pnl += trade.pnl_usd
        self._weekly_pnl += trade.pnl_usd
        if trade.pnl_usd < 0:
            self._last_loss_time_ms = trade.close_time_ms

    def get_closed_trades(self) -> list[SimulatedTrade]:
        """Return all closed trades (excludes entry_timeout)."""
        return [t for t in self.closed if t.exit_reason != "entry_timeout"]

    def get_all_closed(self) -> list[SimulatedTrade]:
        """Return all closed trades including entry timeouts."""
        return self.closed


# ================================================================
# Metrics computation
# ================================================================

@dataclass
class BacktestMetrics:
    """Computed performance metrics."""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_pnl_usd: float = 0.0
    total_pnl_pct: float = 0.0
    avg_r_multiple: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    profit_factor: float = 0.0
    trades_per_week: float = 0.0
    avg_trade_duration_hours: float = 0.0
    # Breakdown
    by_setup: dict = field(default_factory=dict)
    by_pair: dict = field(default_factory=dict)
    by_direction: dict = field(default_factory=dict)
    exit_reasons: dict = field(default_factory=dict)
    entry_timeouts: int = 0


def compute_metrics(simulator: TradeSimulator, period_days: float) -> BacktestMetrics:
    """Compute all performance metrics from simulator results."""
    m = BacktestMetrics()
    trades = simulator.get_closed_trades()
    all_closed = simulator.get_all_closed()

    m.total_trades = len(trades)
    m.entry_timeouts = len(all_closed) - len(trades)

    if not trades:
        return m

    # Win/Loss
    m.wins = sum(1 for t in trades if t.pnl_usd > 0)
    m.losses = sum(1 for t in trades if t.pnl_usd <= 0)
    m.win_rate = m.wins / m.total_trades if m.total_trades > 0 else 0.0

    # PnL
    m.total_pnl_usd = sum(t.pnl_usd for t in trades)
    m.total_pnl_pct = (m.total_pnl_usd / simulator.initial_capital) * 100

    # Average R-multiple
    r_multiples = []
    for t in trades:
        risk = t.position_size * abs(t.entry_price - t.sl_price)
        if risk > 0:
            r_multiples.append(t.pnl_usd / risk)
    m.avg_r_multiple = sum(r_multiples) / len(r_multiples) if r_multiples else 0.0

    # Max drawdown from equity curve
    m.max_drawdown_pct = _compute_max_drawdown(simulator.equity_curve)

    # Sharpe ratio (daily, annualized)
    m.sharpe_ratio = _compute_sharpe(trades, simulator.initial_capital, period_days)

    # Profit factor
    gross_profit = sum(t.pnl_usd for t in trades if t.pnl_usd > 0)
    gross_loss = abs(sum(t.pnl_usd for t in trades if t.pnl_usd < 0))
    m.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Trades per week
    weeks = period_days / 7
    m.trades_per_week = m.total_trades / weeks if weeks > 0 else 0.0

    # Average trade duration
    durations = []
    for t in trades:
        if t.entry_time_ms > 0 and t.close_time_ms > 0:
            durations.append((t.close_time_ms - t.entry_time_ms) / (1000 * 3600))
    m.avg_trade_duration_hours = sum(durations) / len(durations) if durations else 0.0

    # Breakdown by setup type
    for key_attr, target_dict in [("setup_type", m.by_setup),
                                   ("pair", m.by_pair),
                                   ("direction", m.by_direction)]:
        groups: dict[str, list] = defaultdict(list)
        for t in trades:
            groups[getattr(t, key_attr)].append(t)
        for name, group in sorted(groups.items()):
            w = sum(1 for t in group if t.pnl_usd > 0)
            pnl = sum(t.pnl_usd for t in group)
            target_dict[name] = {
                "count": len(group),
                "wins": w,
                "win_rate": w / len(group) if group else 0,
                "pnl": pnl,
            }

    # Exit reason distribution
    for t in trades:
        m.exit_reasons[t.exit_reason] = m.exit_reasons.get(t.exit_reason, 0) + 1

    return m


def _compute_max_drawdown(equity_curve: list[tuple[int, float]]) -> float:
    """Compute max drawdown % from equity curve."""
    if len(equity_curve) < 2:
        return 0.0
    peak = equity_curve[0][1]
    max_dd = 0.0
    for _, equity in equity_curve:
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd * 100


def _compute_sharpe(trades: list[SimulatedTrade], initial_capital: float,
                    period_days: float) -> float:
    """Compute annualized Sharpe ratio from daily PnL."""
    if not trades or period_days < 2:
        return 0.0

    # Bucket PnL by day
    daily_pnl: dict[str, float] = defaultdict(float)
    for t in trades:
        day = datetime.fromtimestamp(
            t.close_time_ms / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d")
        daily_pnl[day] += t.pnl_usd

    # Convert to daily returns (as fraction of initial capital)
    returns = [pnl / initial_capital for pnl in daily_pnl.values()]
    if len(returns) < 2:
        return 0.0

    mean_r = sum(returns) / len(returns)
    variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    std_r = math.sqrt(variance) if variance > 0 else 0.0

    if std_r == 0:
        return 0.0

    return (mean_r / std_r) * math.sqrt(365)


# ================================================================
# Helpers
# ================================================================

def _ts_to_str(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _ts_to_date(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


# ================================================================
# Report printing
# ================================================================

def print_report(m: BacktestMetrics, simulator: TradeSimulator,
                 profile: str, period_days: float,
                 setups_found: int, total_evaluated: int,
                 tracker: RejectTracker,
                 setups_deduped: int = 0) -> None:
    """Print full backtest report to console."""
    print()
    print("=" * 70)
    print(f"BACKTEST RESULTS — profile: {profile}")
    print("=" * 70)

    # -- Setup detection --
    print(f"\nSETUP DETECTION:")
    print(f"  Candles evaluated: {total_evaluated}")
    print(f"  Setups detected:   {setups_found}")
    if setups_deduped > 0:
        print(f"  Setups deduped:    {setups_deduped}")
    if total_evaluated > 0:
        print(f"  Detection rate:    {setups_found/total_evaluated*100:.2f}%")

    # -- Rejection breakdown --
    if tracker.counts and total_evaluated > 0:
        print(f"\nREJECTION BREAKDOWN:")
        sorted_reasons = sorted(tracker.counts.items(), key=lambda x: -x[1])
        for reason, count in sorted_reasons[:10]:
            pct = count / total_evaluated * 100
            label = reason.replace("_", " ").title()
            print(f"  {label:<40} {count:>5} ({pct:>5.1f}%)")

    # -- Risk rejections --
    if simulator.risk_rejections:
        total_risk = sum(simulator.risk_rejections.values())
        print(f"\nRISK REJECTIONS ({total_risk} total):")
        for reason, count in sorted(simulator.risk_rejections.items(), key=lambda x: -x[1]):
            print(f"  {reason:<30} {count:>5}")

    # -- Trade simulation --
    trades = simulator.get_closed_trades()
    print(f"\n{'='*70}")
    print(f"TRADE SIMULATION")
    print(f"{'='*70}")
    print(f"  Initial capital:    ${simulator.initial_capital:,.2f}")
    print(f"  Final equity:       ${simulator.equity:,.2f}")
    print(f"  Entry timeouts:     {m.entry_timeouts}")
    print(f"  Trades executed:    {m.total_trades}")

    if m.total_trades == 0:
        print(f"\n  No trades executed. Cannot compute metrics.")
        print(f"{'='*70}")
        return

    print(f"  Wins / Losses:      {m.wins} / {m.losses}")
    print(f"  Win rate:           {m.win_rate*100:.1f}%")
    print()

    # -- Performance --
    print(f"PERFORMANCE:")
    print(f"  Total PnL:          ${m.total_pnl_usd:+,.2f} ({m.total_pnl_pct:+.2f}%)")
    print(f"  Avg R-multiple:     {m.avg_r_multiple:+.2f}R")
    print(f"  Profit factor:      {m.profit_factor:.2f}")
    print(f"  Max drawdown:       {m.max_drawdown_pct:.2f}%")
    print(f"  Sharpe ratio:       {m.sharpe_ratio:.2f}")
    print(f"  Trades/week:        {m.trades_per_week:.1f}")
    print(f"  Avg duration:       {m.avg_trade_duration_hours:.1f}h")
    print()

    # -- Targets comparison --
    print(f"TARGETS (from CLAUDE.md):")
    _target_line("Win rate", f"{m.win_rate*100:.1f}%", ">45%", m.win_rate > 0.45)
    _target_line("Avg R:R", f"{m.avg_r_multiple:.2f}", ">1.5", m.avg_r_multiple > 1.5)
    _target_line("Max DD", f"{m.max_drawdown_pct:.1f}%", "<10%", m.max_drawdown_pct < 10)
    _target_line("Sharpe", f"{m.sharpe_ratio:.2f}", ">1.0", m.sharpe_ratio > 1.0)
    _target_line("Profit factor", f"{m.profit_factor:.2f}", ">1.5", m.profit_factor > 1.5)
    _target_line("Trades/week", f"{m.trades_per_week:.1f}", "5-15",
                 5 <= m.trades_per_week <= 15)
    print()

    # -- Breakdown by setup type --
    if m.by_setup:
        print(f"BY SETUP TYPE:")
        for name, stats in sorted(m.by_setup.items()):
            print(f"  {name:<12} trades={stats['count']:<4} "
                  f"win={stats['win_rate']*100:5.1f}%  "
                  f"PnL=${stats['pnl']:+,.2f}")
        print()

    # -- Breakdown by pair --
    if m.by_pair:
        print(f"BY PAIR:")
        for name, stats in sorted(m.by_pair.items()):
            print(f"  {name:<12} trades={stats['count']:<4} "
                  f"win={stats['win_rate']*100:5.1f}%  "
                  f"PnL=${stats['pnl']:+,.2f}")
        print()

    # -- Breakdown by direction --
    if m.by_direction:
        print(f"BY DIRECTION:")
        for name, stats in sorted(m.by_direction.items()):
            print(f"  {name:<12} trades={stats['count']:<4} "
                  f"win={stats['win_rate']*100:5.1f}%  "
                  f"PnL=${stats['pnl']:+,.2f}")
        print()

    # -- Exit reasons --
    if m.exit_reasons:
        print(f"EXIT REASONS:")
        for reason, count in sorted(m.exit_reasons.items(), key=lambda x: -x[1]):
            pct = count / m.total_trades * 100
            print(f"  {reason:<20} {count:>4} ({pct:5.1f}%)")
        print()

    # -- Individual trades --
    if trades:
        print(f"TRADE LOG:")
        print(f"  {'#':<4} {'Time':<17} {'Pair':<10} {'Type':<10} "
              f"{'Dir':<6} {'Entry':>10} {'Exit':>10} {'PnL':>10} {'Reason':<14}")
        print(f"  {'-'*95}")
        for i, t in enumerate(trades, 1):
            print(f"  {i:<4} {_ts_to_str(t.entry_time_ms):<17} {t.pair:<10} "
                  f"{t.setup_type:<10} {t.direction:<6} "
                  f"{t.entry_price:>10.2f} {t.exit_price:>10.2f} "
                  f"${t.pnl_usd:>+9.2f} {t.exit_reason:<14}")
        print()

    print("=" * 70)


def _target_line(name: str, actual: str, target: str, met: bool) -> None:
    status = "PASS" if met else "FAIL"
    print(f"  {name:<18} {actual:>8}  target {target:<6}  [{status}]")


# ================================================================
# CSV export
# ================================================================

def export_csv(trades: list[SimulatedTrade], filename: str) -> None:
    """Export trade results to CSV."""
    headers = [
        "trade_id", "pair", "direction", "setup_type",
        "entry_price", "sl_price", "tp1", "tp2",
        "position_size", "leverage",
        "entry_time", "close_time",
        "exit_price", "pnl_usd", "exit_reason",
    ]
    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for i, t in enumerate(trades, 1):
            writer.writerow([
                i, t.pair, t.direction, t.setup_type,
                f"{t.entry_price:.2f}", f"{t.sl_price:.2f}",
                f"{t.tp1_price:.2f}", f"{t.tp2_price:.2f}",
                f"{t.position_size:.6f}", f"{t.leverage:.2f}",
                _ts_to_str(t.entry_time_ms), _ts_to_str(t.close_time_ms),
                f"{t.exit_price:.2f}", f"{t.pnl_usd:.2f}", t.exit_reason,
            ])
    print(f"CSV exported: {filename}")


# ================================================================
# JSON result persistence
# ================================================================

def save_results_json(m: BacktestMetrics, profile: str, period_days: float,
                      capital: float, pairs: list[str],
                      setups_found: int, setups_deduped: int) -> str:
    """Save backtest summary to JSON for future reference."""
    results_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "backtest_results")
    os.makedirs(results_dir, exist_ok=True)

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(results_dir, f"{ts}_{profile}_{int(period_days)}d.json")

    result = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "profile": profile,
        "period_days": round(period_days, 1),
        "capital": capital,
        "pairs": pairs,
        "summary": {
            "total_trades": m.total_trades,
            "wins": m.wins,
            "losses": m.losses,
            "win_rate": round(m.win_rate * 100, 1),
            "total_pnl_usd": round(m.total_pnl_usd, 2),
            "total_pnl_pct": round(m.total_pnl_pct, 2),
            "avg_r_multiple": round(m.avg_r_multiple, 3),
            "max_drawdown_pct": round(m.max_drawdown_pct, 1),
            "sharpe_ratio": round(m.sharpe_ratio, 2),
            "profit_factor": round(m.profit_factor, 2) if m.profit_factor != float("inf") else "inf",
            "trades_per_week": round(m.trades_per_week, 1),
            "avg_trade_duration_hours": round(m.avg_trade_duration_hours, 1),
        },
        "by_setup": m.by_setup,
        "by_pair": m.by_pair,
        "by_direction": m.by_direction,
        "exit_reasons": m.exit_reasons,
        "entry_timeouts": m.entry_timeouts,
        "setups_found": setups_found,
        "setups_deduped": setups_deduped,
    }

    # Round floats in nested dicts
    for section in [result["by_setup"], result["by_pair"], result["by_direction"]]:
        for key, val in section.items():
            if isinstance(val, dict):
                for k, v in val.items():
                    if isinstance(v, float):
                        val[k] = round(v, 4)

    with open(filename, "w") as f:
        json.dump(result, f, indent=2)

    print(f"Results saved: {filename}")
    return filename


# ================================================================
# Main backtest
# ================================================================

def run_backtest(pairs: list[str] | None = None, verbose: bool = False,
                 warmup: int = 50, profile: str = "default",
                 capital: float = 10000.0, export: bool = False,
                 days: int | None = None):
    from config.settings import Settings, settings, STRATEGY_PROFILES, apply_profile, reset_profile

    if pairs is None:
        pairs = settings.TRADING_PAIRS

    # Apply profile
    if profile != "default":
        if profile not in STRATEGY_PROFILES:
            print(f"Unknown profile: {profile}")
            print(f"Available: {', '.join(STRATEGY_PROFILES.keys())}")
            sys.exit(1)

        reset_profile(settings)
        apply_profile(settings, profile)

        overrides = STRATEGY_PROFILES[profile]
        print("=" * 70)
        print(f"PROFILE: {profile.upper()}")
        print("=" * 70)
        for key, value in overrides.items():
            default_val = getattr(Settings(), key)
            print(f"  {key}: {default_val} -> {value}")
        print()
    else:
        reset_profile(settings)

    all_timeframes = settings.HTF_TIMEFRAMES + settings.LTF_TIMEFRAMES

    # Connect to PostgreSQL
    pg = PostgresStore()
    if not pg.connect():
        logger.error("Cannot connect to PostgreSQL -- aborting")
        sys.exit(1)

    # Load data — enough for requested days
    load_count = 50000  # Covers 90+ days of 5m data
    data = BacktestDataService()
    data.load_from_postgres(pg, pairs, all_timeframes, count=load_count)
    pg.close()

    # Set up rejection tracker
    tracker = RejectTracker()
    import loguru
    sink_id = loguru.logger.add(
        tracker.sink,
        filter=lambda record: record["name"].startswith("strategy_"),
        level="DEBUG",
    )

    clock = SimulatedClock()
    simulator = TradeSimulator(initial_capital=capital)
    setups_count = 0
    setups_deduped = 0
    total_evaluated = 0
    total_warmup = 0

    # Setup dedup cache — same logic as main.py
    # Key: (pair, direction, setup_type, rounded entry_price)
    # Value: timestamp_ms of last evaluation
    _dedup_cache: dict[tuple, int] = {}
    _DEDUP_TTL_MS = 3600 * 1000  # 1 hour

    for pair in pairs:
        trigger_candles = data.get_trigger_candles(pair, settings.LTF_TIMEFRAMES)
        if not trigger_candles:
            logger.warning(f"No trigger candles for {pair}")
            continue

        # Filter by --days if specified
        if days is not None:
            cutoff_ms = trigger_candles[-1].timestamp - (days * 86400 * 1000)
            trigger_candles = [c for c in trigger_candles if c.timestamp >= cutoff_ms]
            if not trigger_candles:
                logger.warning(f"No candles within {days} days for {pair}")
                continue

        logger.info(f"Replaying {pair}: {len(trigger_candles)} LTF candles "
                    f"[{_ts_to_str(trigger_candles[0].timestamp)} -> "
                    f"{_ts_to_str(trigger_candles[-1].timestamp)}]")

        # Fresh StrategyService per pair — detectors accumulate state
        strategy = StrategyService(data)

        with patch("strategy_service.service.time.time", clock), \
             patch("strategy_service.setups.time.time", clock):

            for i, candle in enumerate(trigger_candles):
                clock.set_ms(candle.timestamp)
                data.set_time(candle.timestamp)

                # Process existing trades BEFORE strategy eval (no look-ahead)
                simulator.on_candle(candle)

                is_warmup = i < warmup
                if is_warmup:
                    total_warmup += 1
                    continue

                total_evaluated += 1
                tracker.reset_last()

                setup = strategy.evaluate(pair, candle)
                if setup:
                    # Dedup: skip if same setup was evaluated recently
                    dedup_key = (setup.pair, setup.direction,
                                 setup.setup_type,
                                 round(setup.entry_price, 2))
                    last_eval = _dedup_cache.get(dedup_key, 0)
                    if (candle.timestamp - last_eval) < _DEDUP_TTL_MS:
                        setups_deduped += 1
                        continue
                    _dedup_cache[dedup_key] = candle.timestamp

                    setups_count += 1
                    taken = simulator.on_setup(setup, candle)
                    if verbose:
                        status = "TAKEN" if taken else "SKIP (max pos)"
                        print(f"  [SETUP {status}] {_ts_to_str(candle.timestamp)} "
                              f"{candle.timeframe} {setup.setup_type} "
                              f"{setup.direction} entry={setup.entry_price:.2f}")
                elif verbose:
                    reasons = tracker.last_reasons or ["passed_htf_but_no_setup"]
                    print(f"  [--] {_ts_to_str(candle.timestamp)} "
                          f"{candle.timeframe} close={candle.close:.2f} "
                          f"reason={reasons[-1] if reasons else '?'}")

    loguru.logger.remove(sink_id)

    # Compute period
    all_trigger = []
    for pair in pairs:
        tc = data.get_trigger_candles(pair, settings.LTF_TIMEFRAMES)
        if days is not None and tc:
            cutoff_ms = tc[-1].timestamp - (days * 86400 * 1000)
            tc = [c for c in tc if c.timestamp >= cutoff_ms]
        all_trigger.extend(tc)

    if all_trigger:
        first_ts = min(c.timestamp for c in all_trigger)
        last_ts = max(c.timestamp for c in all_trigger)
        period_days = (last_ts - first_ts) / (1000 * 86400)
    else:
        period_days = 0

    if period_days > 0:
        print(f"Period: {_ts_to_date(first_ts)} to {_ts_to_date(last_ts)} "
              f"({period_days:.1f} days)")

    # Compute metrics and print report
    metrics = compute_metrics(simulator, period_days)
    print_report(metrics, simulator, profile, period_days,
                 setups_count, total_evaluated, tracker,
                 setups_deduped=setups_deduped)

    # Always save JSON summary
    save_results_json(metrics, profile, period_days, capital, pairs,
                      setups_count, setups_deduped)

    # CSV export
    if export:
        trades = simulator.get_closed_trades()
        if trades:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"backtest_results_{ts}.csv"
            export_csv(trades, filename)

    # Restore default settings
    reset_profile(settings)


def main():
    from config.settings import STRATEGY_PROFILES

    available = ", ".join(STRATEGY_PROFILES.keys())
    parser = argparse.ArgumentParser(
        description="Backtest: replay candles + simulate trades")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print per-candle evaluation results")
    parser.add_argument("--pair", type=str, default=None,
                        help="Single pair to test (e.g. BTC/USDT)")
    parser.add_argument("--warmup", type=int, default=50,
                        help="Number of warmup candles to skip (default: 50)")
    parser.add_argument("--profile", type=str, default="default",
                        help=f"Strategy profile ({available})")
    parser.add_argument("--capital", type=float, default=10000.0,
                        help="Initial capital in USDT (default: 10000)")
    parser.add_argument("--days", type=int, default=None,
                        help="Limit to last N days of data")
    parser.add_argument("--csv", action="store_true",
                        help="Export results to CSV file")
    args = parser.parse_args()

    pairs = [args.pair] if args.pair else None
    run_backtest(
        pairs=pairs,
        verbose=args.verbose,
        warmup=args.warmup,
        profile=args.profile,
        capital=args.capital,
        days=args.days,
        export=args.csv,
    )


if __name__ == "__main__":
    main()
