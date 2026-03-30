#!/usr/bin/env python3
"""
Backtester — Replay historical candles through StrategyService + simulate fills.

Loads candles from PostgreSQL, replays candle-by-candle, detects setups via
StrategyService, simulates entry/SL/TP fills, and produces performance metrics.

Usage:
    python scripts/backtest.py --days 60
    python scripts/backtest.py --days 90 --capital 10000
    python scripts/backtest.py --pair BTC/USDT --verbose
    python scripts/backtest.py --days 60 --fill-mode conservative --fill-buffer 0.001
    python scripts/backtest.py --days 60 --csv
"""

import argparse
import asyncio
import csv
import json
import math
import random
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
from shared.models import Candle, TradeSetup, AIDecision, FundingRate, OpenInterest, CVDSnapshot, MarketSnapshot
from strategy_service.service import StrategyService
from shared.logger import setup_logger
from config.settings import settings, QUICK_SETUP_TYPES

logger = setup_logger("backtest", file_level="INFO")


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
        # Historical funding rates, OI, and CVD per pair, sorted oldest-first
        self._funding: dict[str, list[FundingRate]] = {}
        self._oi: dict[str, list[OpenInterest]] = {}
        self._cvd: dict[str, list[CVDSnapshot]] = {}
        # 1m candle index for timeframe-detail resolution
        # {pair: {timestamp_ms: [Candle, ...]}} — 1m candles grouped by parent candle timestamp
        self._detail_loaded: bool = False

    def load_from_postgres(self, pg: PostgresStore, pairs: list[str],
                           timeframes: list[str], count: int = 50000,
                           load_detail: bool = False):
        """Load historical candles, funding rates, and OI from PostgreSQL.

        Args:
            load_detail: If True, also load 1m candles for timeframe-detail
                        resolution of ambiguous SL/TP ordering.
        """
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

            # Load 1m candles for timeframe-detail mode
            if load_detail and "1m" not in timeframes:
                candles_1m = pg.load_candles(pair, "1m", count * 5)
                if candles_1m:
                    self._candles[(pair, "1m")] = candles_1m
                    self._detail_loaded = True
                    logger.info(f"Loaded {len(candles_1m)} detail candles: {pair} 1m "
                                f"[{_ts_to_str(candles_1m[0].timestamp)} -> "
                                f"{_ts_to_str(candles_1m[-1].timestamp)}]")
                else:
                    logger.warning(f"No 1m candles in DB for {pair} — "
                                   f"timeframe-detail will fall back to SL-first")

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

            # Load historical CVD
            cvd = pg.load_cvd_snapshots(pair)
            if cvd:
                self._cvd[pair] = cvd
                logger.info(f"Loaded {len(cvd)} CVD snapshots: {pair}")

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
        """Return MarketSnapshot with historical funding + OI + CVD at current time."""
        funding = self._find_nearest(
            self._funding.get(pair, []), self._current_time_ms
        )
        oi = self._find_nearest(
            self._oi.get(pair, []), self._current_time_ms
        )
        cvd = self._find_nearest(
            self._cvd.get(pair, []), self._current_time_ms
        )
        return MarketSnapshot(
            pair=pair,
            timestamp=self._current_time_ms,
            funding=funding,
            oi=oi,
            cvd=cvd,
        )

    def get_detail_candles(self, pair: str, start_ms: int,
                           end_ms: int) -> list[Candle]:
        """Get 1m candles within a time range for timeframe-detail resolution.

        Args:
            start_ms: Start of parent candle (inclusive)
            end_ms: End of parent candle (exclusive)

        Returns:
            List of 1m candles sorted chronologically, or empty if unavailable.
        """
        all_1m = self._candles.get((pair, "1m"), [])
        if not all_1m:
            return []

        # Binary search for start position
        lo, hi = 0, len(all_1m) - 1
        start_idx = len(all_1m)
        while lo <= hi:
            mid = (lo + hi) // 2
            if all_1m[mid].timestamp >= start_ms:
                start_idx = mid
                hi = mid - 1
            else:
                lo = mid + 1

        # Collect candles in range
        result = []
        for i in range(start_idx, len(all_1m)):
            if all_1m[i].timestamp >= end_ms:
                break
            result.append(all_1m[i])
        return result

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
# Pre-filter for Claude (copied from main.py)
# ================================================================

def _pre_filter_for_claude(setup, snapshot) -> str | None:
    """Deterministic pre-filter before Claude API call.

    Returns rejection reason string if setup should be rejected, None if it should
    proceed to Claude. Conservative: skips checks when data is unavailable.

    Setup C skips funding check (extreme funding IS the signal).
    """
    threshold = settings.FUNDING_EXTREME_THRESHOLD

    # Check 1: Funding rate extreme against trade direction
    # Skip for Setup C — extreme funding IS the signal
    if setup.setup_type != "setup_c":
        if snapshot.funding is not None and snapshot.funding.rate is not None:
            rate = snapshot.funding.rate
            if setup.direction == "long" and rate > threshold:
                return f"Funding extreme against long ({rate*100:.4f}% > {threshold*100:.4f}%)"
            if setup.direction == "short" and rate < -threshold:
                return f"Funding extreme against short ({rate*100:.4f}% < -{threshold*100:.4f}%)"

    # Check 2a: Hard regime gate — reject ALL directions in extreme fear
    if snapshot.news_sentiment is not None:
        fg = snapshot.news_sentiment.score
        if fg < settings.REGIME_EXTREME_FEAR_GATE:
            return f"Regime gate: F&G={fg} < {settings.REGIME_EXTREME_FEAR_GATE} (systemic crisis)"

    # Check 2b: Fear & Greed extreme against trade direction
    if snapshot.news_sentiment is not None:
        fg = snapshot.news_sentiment.score
        if setup.direction == "long" and fg < settings.NEWS_EXTREME_FEAR_THRESHOLD:
            return f"Extreme Fear (F&G={fg}) — rejecting long"
        if setup.direction == "short" and fg > settings.NEWS_EXTREME_GREED_THRESHOLD:
            return f"Extreme Greed (F&G={fg}) — rejecting short"

    # Check 3: CVD strong divergence against trade direction
    if snapshot.cvd is not None:
        buy_vol = snapshot.cvd.buy_volume
        sell_vol = snapshot.cvd.sell_volume
        total_vol = buy_vol + sell_vol
        if total_vol > 0:
            buy_dominance = buy_vol / total_vol
            if setup.direction == "long" and buy_dominance < 0.40:
                return f"CVD divergence against long (buy dominance {buy_dominance*100:.1f}% < 40%)"
            if setup.direction == "short" and buy_dominance > 0.60:
                return f"CVD divergence against short (buy dominance {buy_dominance*100:.1f}% > 60%)"

    return None


# ================================================================
# SimulatedTrade — tracks a single trade through its lifecycle
# ================================================================

@dataclass
class SimulatedTrade:
    """A trade being simulated through candle replay.

    Exit management (matches live execution):
    - SL at sl_price for 100% of position
    - Legacy mode (TRAILING_TP_ENABLED=False):
        - Single TP at tp2_price (2:1 R:R) for 100% close
        - Breakeven: price crosses tp1_price (1:1) → SL moves to entry
        - Trailing: price crosses midpoint(tp1,tp2) (1.5:1) → SL moves to tp1
    - Progressive trail mode (TRAILING_TP_ENABLED=True):
        - Ceiling TP at TRAIL_CEILING_RR (5:1) as safety net
        - SL trails in TRAIL_STEP_RR (0.5) R:R steps, one step behind
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
    trail_level: int = 0        # Progressive trail step (0=no trail)

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
# SimulatedCampaign — tracks an HTF campaign through its lifecycle
# ================================================================

@dataclass
class SimulatedCampaign:
    """An HTF campaign being simulated through candle replay.

    Exit management (matches live campaigns):
    - No TP — exit via trailing SL on 4H swing levels only
    - Pyramid adds: up to 3 with decreasing margin ($15/$10/$5)
    - Timeout: 7 days max duration
    """

    # Setup identity
    pair: str
    direction: str              # "long" or "short"
    setup_type: str

    # Target prices
    entry_price: float
    sl_price: float             # Initial SL (OB edge)
    initial_margin: float
    leverage: float
    position_size: float        # Initial size in base currency

    # State tracking
    phase: str = "pending"      # "pending" -> "active" -> "closed"
    current_sl: float = 0.0     # Trails on 4H swing levels
    weighted_entry: float = 0.0 # VWAP of all fills
    total_size: float = 0.0     # Sum of all filled sizes
    total_margin: float = 0.0   # Sum of all margins

    # Pyramid adds (filled)
    adds: list = field(default_factory=list)  # list of dicts
    # Pending add: {add_number, entry_price, margin, size, deadline_ms}
    pending_add: dict | None = None

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

    Pending entries are stored per-pair (dict), matching live ExecutionService
    behavior where a new setup replaces the existing pending for the same pair.
    """

    def __init__(self, initial_capital: float,
                 fill_mode: str = "optimistic",
                 fill_buffer_pct: float = 0.001,
                 fill_probability: float = 1.0,
                 seed: int = 42,
                 data_service: BacktestDataService | None = None):
        self.initial_capital: float = initial_capital
        self.equity: float = initial_capital
        self.pending: dict[str, SimulatedTrade] = {}   # keyed by pair
        self.active: list[SimulatedTrade] = []
        self.closed: list[SimulatedTrade] = []
        # (timestamp_ms, equity) — for drawdown calculation
        self.equity_curve: list[tuple[int, float]] = [(0, initial_capital)]

        # Fill model: "optimistic" (touch=fill) or "conservative" (penetrate by buffer)
        self.fill_mode: str = fill_mode
        self.fill_buffer_pct: float = fill_buffer_pct
        # Probabilistic fill model: after price reaches entry, apply this probability
        self.fill_probability: float = fill_probability
        self._rng: random.Random = random.Random(seed)
        # Data service for timeframe-detail resolution (1m candles)
        self._data: BacktestDataService | None = data_service

        # Execution funnel counters
        self._pending_created: int = 0
        self._pending_replaced: int = 0
        self._pending_timeout: int = 0
        self._pending_filled: int = 0
        self._exec_by_setup: dict[str, dict[str, int]] = {}

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

    def _track_exec(self, setup_type: str, event: str) -> None:
        """Increment per-setup execution funnel counter."""
        if setup_type not in self._exec_by_setup:
            self._exec_by_setup[setup_type] = {
                "created": 0, "replaced": 0, "timeout": 0, "filled": 0,
            }
        self._exec_by_setup[setup_type][event] += 1

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
        """Accept a new setup. Returns True if trade was created.

        Matches live ExecutionService behavior:
        - Only one pending entry per pair (new replaces old)
        - Rejects if pair already has an active trade
        """
        self._update_day_week(candle.timestamp)

        # --- Matching live ExecutionService ---

        # Skip if pair already has an active trade
        if any(t.pair == setup.pair for t in self.active):
            return self._reject("Active position exists for pair")

        # --- Risk guardrails (same as live RiskService) ---

        # Max open positions (don't count the pending we're about to replace)
        is_replacement = setup.pair in self.pending
        open_count = len(self.pending) + len(self.active)
        if is_replacement:
            open_count -= 1
        if open_count >= settings.MAX_OPEN_POSITIONS:
            return self._reject("Max open positions")

        # Skip if equity depleted
        if self.equity <= 0:
            return False

        # Min/max risk distance
        distance = abs(setup.entry_price - setup.sl_price)
        if distance == 0:
            return self._reject("Zero risk distance")
        risk_pct = distance / setup.entry_price
        if risk_pct < settings.MIN_RISK_DISTANCE_PCT:
            return self._reject("SL too close to entry")
        if risk_pct > settings.MAX_SL_PCT:
            return self._reject(f"SL too far ({risk_pct*100:.1f}% > {settings.MAX_SL_PCT*100:.0f}%)")

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

        # Portfolio heat check — sum of (size × sl_distance) across all open positions
        current_heat = sum(
            t.position_size * abs(t.entry_price - t.sl_price)
            for t in list(self.pending.values()) + self.active
        )
        new_heat = position_size * distance
        max_heat = self.equity * settings.MAX_PORTFOLIO_HEAT_PCT
        if max_heat > 0 and (current_heat + new_heat) > max_heat:
            return self._reject(
                f"Portfolio heat (${current_heat + new_heat:.2f} > ${max_heat:.2f})"
            )

        # Entry timeout
        if setup.setup_type in QUICK_SETUP_TYPES:
            timeout_ms = settings.ENTRY_TIMEOUT_QUICK_SECONDS * 1000
        else:
            timeout_ms = settings.ENTRY_TIMEOUT_SECONDS * 1000

        self._trades_today += 1

        # Replace old pending for same pair if exists (matches live behavior)
        if is_replacement:
            old = self.pending.pop(setup.pair)
            old.phase = "closed"
            old.exit_reason = "pending_replaced"
            old.close_time_ms = candle.timestamp
            self.closed.append(old)
            self._pending_replaced += 1
            self._track_exec(old.setup_type, "replaced")

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
        self.pending[setup.pair] = trade
        self._pending_created += 1
        self._track_exec(setup.setup_type, "created")
        return True

    def on_candle(self, candle: Candle) -> None:
        """Process one candle: check pending entries + active trade fills."""
        self._process_pending(candle)
        self._process_active(candle)

    def _check_entry_fill(self, trade: SimulatedTrade, candle: Candle) -> bool:
        """Check if a pending entry would fill on this candle.

        Optimistic mode: touch = fill (price reaches entry level).
        Conservative mode: price must penetrate beyond entry by buffer,
        simulating that a limit order needs the market to move through it.

        After price check passes, applies fill_probability (0.0-1.0) to
        simulate realistic limit order fill rates.
        """
        price_reached = False
        if self.fill_mode == "conservative":
            buffer = trade.entry_price * self.fill_buffer_pct
            if trade.direction == "long":
                price_reached = candle.low <= (trade.entry_price - buffer)
            else:
                price_reached = candle.high >= (trade.entry_price + buffer)
        else:  # optimistic
            if trade.direction == "long":
                price_reached = candle.low <= trade.entry_price
            else:
                price_reached = candle.high >= trade.entry_price

        if not price_reached:
            return False

        # Apply fill probability
        if self.fill_probability < 1.0:
            return self._rng.random() < self.fill_probability

        return True

    def _process_pending(self, candle: Candle) -> None:
        """Check if pending entries fill or expire."""
        to_remove: list[str] = []
        for pair, trade in self.pending.items():
            if pair != candle.pair:
                continue

            # Entry timeout
            if candle.timestamp > trade.entry_deadline_ms:
                trade.phase = "closed"
                trade.exit_reason = "entry_timeout"
                trade.close_time_ms = candle.timestamp
                self.closed.append(trade)
                self._pending_timeout += 1
                self._track_exec(trade.setup_type, "timeout")
                to_remove.append(pair)
                continue

            # Entry fill check
            if self._check_entry_fill(trade, candle):
                trade.phase = "active"
                trade.entry_time_ms = candle.timestamp
                self.active.append(trade)
                self._pending_filled += 1
                self._track_exec(trade.setup_type, "filled")
                to_remove.append(pair)

        for pair in to_remove:
            del self.pending[pair]

    def _candle_duration_ms(self, timeframe: str) -> int:
        """Return duration of a candle in milliseconds."""
        multipliers = {"1m": 60_000, "5m": 300_000, "15m": 900_000,
                       "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
        return multipliers.get(timeframe, 300_000)

    def _resolve_ambiguous_exit(self, trade: SimulatedTrade, candle: Candle,
                                tp_target: float) -> str | None:
        """Use 1m candles to determine if SL or TP was hit first.

        Returns "sl" if SL hit first, "tp" if TP hit first, None if
        no 1m data available (caller falls back to SL-first default).
        """
        if self._data is None or not self._data._detail_loaded:
            return None

        duration_ms = self._candle_duration_ms(candle.timeframe)
        detail_candles = self._data.get_detail_candles(
            candle.pair, candle.timestamp, candle.timestamp + duration_ms
        )
        if not detail_candles:
            return None

        for dc in detail_candles:
            dc_sl_hit = (dc.low <= trade.current_sl if trade.direction == "long"
                         else dc.high >= trade.current_sl)
            dc_tp_hit = (dc.high >= tp_target if trade.direction == "long"
                         else dc.low <= tp_target)

            if dc_sl_hit and dc_tp_hit:
                # Both hit on same 1m candle — can't resolve further, SL-first
                return "sl"
            if dc_sl_hit:
                return "sl"
            if dc_tp_hit:
                return "tp"

        return None

    def _process_active(self, candle: Candle) -> None:
        """Check SL, TP, breakeven, trailing, and timeout for active trades.

        Exit management (matches live execution):
        1. SL check first (always priority)
        2. TP hit → close 100%
        3. SL management: progressive trail or legacy breakeven+trailing

        Timeframe-detail: When both SL and TP are within the same candle,
        uses 1m sub-candles to determine which was hit first. Falls back
        to SL-first if no 1m data is available.
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

            # Determine SL and TP hit on this candle
            sl_hit = (candle.low <= trade.current_sl if trade.direction == "long"
                      else candle.high >= trade.current_sl)

            tp_target = trade.tp2_price
            if settings.TRAILING_TP_ENABLED:
                risk = abs(trade.entry_price - trade.sl_price)
                if trade.direction == "long":
                    tp_target = trade.entry_price + (risk * settings.TRAIL_CEILING_RR)
                else:
                    tp_target = trade.entry_price - (risk * settings.TRAIL_CEILING_RR)

            tp_hit = self._price_reached(trade, candle, tp_target)

            # Ambiguous: both SL and TP within same candle — resolve with 1m detail
            if sl_hit and tp_hit:
                resolution = self._resolve_ambiguous_exit(trade, candle, tp_target)
                if resolution == "tp":
                    # TP was hit first
                    self._close_trade(trade, tp_target, "tp", candle.timestamp)
                    self.closed.append(trade)
                    continue
                else:
                    # SL first (or no 1m data — default to SL-first)
                    sl_hit = True
                    tp_hit = False

            if sl_hit:
                if trade.trail_level > 0 and settings.TRAILING_TP_ENABLED:
                    reason = "trailing_sl"
                elif trade.trailing_sl_moved:
                    reason = "trailing_sl"
                elif trade.breakeven_hit:
                    reason = "breakeven_sl"
                else:
                    reason = "sl"
                self._close_trade(trade, trade.current_sl, reason, candle.timestamp)
                self.closed.append(trade)
                continue

            if tp_hit:
                self._close_trade(trade, tp_target, "tp", candle.timestamp)
                self.closed.append(trade)
                continue

            # SL management
            if settings.TRAILING_TP_ENABLED:
                self._progressive_trail(trade, candle)
            else:
                # Legacy: breakeven + trailing
                if not trade.breakeven_hit:
                    if self._price_reached(trade, candle, trade.tp1_price):
                        trade.current_sl = trade.entry_price
                        trade.breakeven_hit = True

                if trade.breakeven_hit and not trade.trailing_sl_moved:
                    midpoint = (trade.tp1_price + trade.tp2_price) / 2.0
                    if self._price_reached(trade, candle, midpoint):
                        trade.current_sl = trade.tp1_price
                        trade.trailing_sl_moved = True

            # Still active
            still_active.append(trade)

        self.active = still_active

    def _progressive_trail(self, trade: SimulatedTrade, candle: Candle) -> None:
        """Advance SL in TRAIL_STEP_RR increments, one step behind."""
        risk = abs(trade.entry_price - trade.sl_price)
        if risk <= 0:
            return

        # Use candle high (long) or low (short) as best price in candle
        if trade.direction == "long":
            best_price = candle.high
            current_rr = (best_price - trade.entry_price) / risk
        else:
            best_price = candle.low
            current_rr = (trade.entry_price - best_price) / risk

        if current_rr < settings.TRAIL_ACTIVATION_RR:
            return

        level = int(current_rr / settings.TRAIL_STEP_RR)
        if level <= trade.trail_level:
            return

        # New SL: one step behind
        sl_rr = (level - 1) * settings.TRAIL_STEP_RR
        if trade.direction == "long":
            trade.current_sl = trade.entry_price + (risk * sl_rr)
        else:
            trade.current_sl = trade.entry_price - (risk * sl_rr)

        trade.trail_level = level
        if not trade.breakeven_hit:
            trade.breakeven_hit = True

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

        # Compute PnL (net of fees)
        if trade.direction == "long":
            trade.pnl_usd = (price - trade.entry_price) * trade.position_size
        else:
            trade.pnl_usd = (trade.entry_price - price) * trade.position_size
        entry_notional = trade.entry_price * trade.position_size
        exit_notional = price * trade.position_size
        total_fees = (entry_notional + exit_notional) * settings.TRADING_FEE_RATE
        trade.pnl_usd -= total_fees

        # Update equity
        self.equity += trade.pnl_usd
        self.equity_curve.append((timestamp_ms, self.equity))

        # Update risk state
        self._daily_pnl += trade.pnl_usd
        self._weekly_pnl += trade.pnl_usd
        if trade.pnl_usd < 0:
            self._last_loss_time_ms = trade.close_time_ms

    def get_closed_trades(self) -> list[SimulatedTrade]:
        """Return all closed trades (excludes entry_timeout and pending_replaced)."""
        return [t for t in self.closed
                if t.exit_reason not in ("entry_timeout", "pending_replaced")]

    def get_all_closed(self) -> list[SimulatedTrade]:
        """Return all closed trades including entry timeouts."""
        return self.closed


# ================================================================
# CampaignSimulator — HTF campaign simulation engine
# ================================================================

class CampaignSimulator:
    """Simulates HTF campaign lifecycle: entry, pyramid adds, trailing SL, timeout.

    Matches production CampaignMonitor logic:
    - No TP orders — exit via trailing SL on 4H swing levels only
    - Pyramid adds (up to 3) with decreasing margin
    - 7-day max duration
    - One campaign at a time (HTF_MAX_CAMPAIGNS=1)
    """

    def __init__(self, initial_capital: float,
                 fill_mode: str = "optimistic",
                 fill_buffer_pct: float = 0.001):
        self.initial_capital: float = initial_capital
        self.equity: float = initial_capital
        self.pending: SimulatedCampaign | None = None
        self.active: SimulatedCampaign | None = None
        self.closed: list[SimulatedCampaign] = []
        self.equity_curve: list[tuple[int, float]] = [(0, initial_capital)]
        self.fill_mode: str = fill_mode
        self.fill_buffer_pct: float = fill_buffer_pct

        # Counters
        self._campaigns_created: int = 0
        self._campaigns_filled: int = 0
        self._campaigns_timeout: int = 0
        self._adds_attempted: int = 0
        self._adds_filled: int = 0

        # Failed entries — prevents re-entering same OB after SL
        self._failed_entries: set[tuple[str, str, float]] = set()

    def on_setup(self, setup: TradeSetup, candle: Candle) -> bool:
        """Accept a new campaign setup. Returns True if pending created."""
        if self.active is not None or self.pending is not None:
            return False

        # Don't re-enter an OB that already lost
        entry_key = (setup.pair, setup.direction, round(setup.entry_price, 2))
        if entry_key in self._failed_entries:
            return False

        margin = settings.HTF_INITIAL_MARGIN
        leverage = float(settings.MAX_LEVERAGE)
        notional = margin * leverage
        position_size = notional / setup.entry_price

        # Check exchange minimum
        min_size = settings.MIN_ORDER_SIZES.get(setup.pair, 0)
        if min_size > 0 and position_size < min_size:
            return False

        campaign = SimulatedCampaign(
            pair=setup.pair,
            direction=setup.direction,
            setup_type=setup.setup_type,
            entry_price=setup.entry_price,
            sl_price=setup.sl_price,
            initial_margin=margin,
            leverage=leverage,
            position_size=position_size,
            current_sl=setup.sl_price,
            setup_time_ms=candle.timestamp,
            entry_deadline_ms=candle.timestamp + settings.HTF_ENTRY_TIMEOUT_SECONDS * 1000,
        )
        self.pending = campaign
        self._campaigns_created += 1
        return True

    def on_candle(self, candle: Candle, strategy=None) -> None:
        """Process one candle for campaign fills/exits."""
        just_filled = self._process_pending(candle)
        # Don't check SL on the same candle that filled the entry —
        # avoids false immediate SL hits on wide 4H candles
        if not just_filled:
            self._process_active(candle, strategy)

    def _check_fill(self, direction: str, entry_price: float,
                    candle: Candle) -> bool:
        """Check if entry would fill on this candle."""
        if self.fill_mode == "conservative":
            buffer = entry_price * self.fill_buffer_pct
            if direction == "long":
                return candle.low <= (entry_price - buffer)
            else:
                return candle.high >= (entry_price + buffer)
        else:  # optimistic
            if direction == "long":
                return candle.low <= entry_price
            else:
                return candle.high >= entry_price

    def _process_pending(self, candle: Candle) -> bool:
        """Check if pending campaign entry fills or expires. Returns True if filled."""
        if self.pending is None or self.pending.pair != candle.pair:
            return False

        c = self.pending

        # Entry timeout (24h default)
        if candle.timestamp > c.entry_deadline_ms:
            c.phase = "closed"
            c.exit_reason = "entry_timeout"
            c.close_time_ms = candle.timestamp
            self.closed.append(c)
            self.pending = None
            self._campaigns_timeout += 1
            return False

        # Fill check
        if self._check_fill(c.direction, c.entry_price, candle):
            c.phase = "active"
            c.entry_time_ms = candle.timestamp
            c.weighted_entry = c.entry_price
            c.total_size = c.position_size
            c.total_margin = c.initial_margin
            self.active = c
            self.pending = None
            self._campaigns_filled += 1
            return True

        return False

    def _process_active(self, candle: Candle, strategy=None) -> None:
        """Check SL, trailing SL, pending adds, timeout for active campaign."""
        if self.active is None or self.active.pair != candle.pair:
            return

        c = self.active

        # Timeout (7 days)
        duration_ms = candle.timestamp - c.entry_time_ms
        if duration_ms >= settings.HTF_MAX_CAMPAIGN_DURATION * 1000:
            self._close_campaign(c, candle.close, "timeout", candle.timestamp)
            return

        # SL check (priority — always first)
        if c.direction == "long":
            sl_hit = candle.low <= c.current_sl
        else:
            sl_hit = candle.high >= c.current_sl

        if sl_hit:
            self._close_campaign(c, c.current_sl, "trailing_sl", candle.timestamp)
            return

        # Check pending add fill
        if c.pending_add is not None:
            self._check_add_fill(c, candle)

        # Trail SL on 4H swing levels (only on 4H candles)
        if strategy is not None and candle.timeframe == settings.HTF_CAMPAIGN_SIGNAL_TF:
            self._trail_sl(c, strategy)

    def _trail_sl(self, c: SimulatedCampaign, strategy) -> None:
        """Trail SL on 4H swing levels. Only moves SL up (long) or down (short)."""
        swing_highs, swing_lows = strategy.get_htf_swing_levels(c.pair)

        if c.direction == "long" and swing_lows:
            for sl in sorted(swing_lows, key=lambda s: s.timestamp, reverse=True):
                if sl.price > c.current_sl and sl.price < c.weighted_entry:
                    c.current_sl = sl.price
                    break
        elif c.direction == "short" and swing_highs:
            for sh in sorted(swing_highs, key=lambda s: s.timestamp, reverse=True):
                if sh.price < c.current_sl and sh.price > c.weighted_entry:
                    c.current_sl = sh.price
                    break

    def try_add(self, c: SimulatedCampaign, setup: TradeSetup,
                candle: Candle) -> bool:
        """Try to place a pyramid add on active campaign."""
        if c.pending_add is not None:
            return False
        if len(c.adds) >= settings.HTF_MAX_ADDS:
            return False
        if setup.direction != c.direction:
            return False

        # Check profitability (>= HTF_ADD_MIN_RR from initial entry)
        risk = abs(c.weighted_entry - c.sl_price)
        if risk <= 0:
            return False

        if c.direction == "long":
            profit = candle.close - c.weighted_entry
        else:
            profit = c.weighted_entry - candle.close

        if (profit / risk) < settings.HTF_ADD_MIN_RR:
            return False

        # Determine add margin (decreasing: $15, $10, $5)
        add_number = len(c.adds) + 1
        margins = {
            1: settings.HTF_ADD1_MARGIN,
            2: settings.HTF_ADD2_MARGIN,
            3: settings.HTF_ADD3_MARGIN,
        }
        margin = margins.get(add_number, 0)
        if margin <= 0:
            return False

        notional = margin * c.leverage
        add_size = notional / setup.entry_price

        c.pending_add = {
            "add_number": add_number,
            "entry_price": setup.entry_price,
            "margin": margin,
            "size": add_size,
            "deadline_ms": candle.timestamp + 14400 * 1000,  # 4h timeout
        }
        self._adds_attempted += 1
        return True

    def _check_add_fill(self, c: SimulatedCampaign, candle: Candle) -> None:
        """Check if a pending pyramid add fills or times out."""
        add = c.pending_add
        if add is None:
            return

        # Add timeout (4 hours)
        if candle.timestamp > add["deadline_ms"]:
            c.pending_add = None
            return

        # Fill check
        if self._check_fill(c.direction, add["entry_price"], candle):
            # Update weighted entry (VWAP)
            old_notional = c.weighted_entry * c.total_size
            new_notional = add["entry_price"] * add["size"]
            c.total_size += add["size"]
            c.weighted_entry = (old_notional + new_notional) / c.total_size
            c.total_margin += add["margin"]
            c.adds.append(add)
            c.pending_add = None
            self._adds_filled += 1

    def _close_campaign(self, c: SimulatedCampaign, price: float,
                        reason: str, timestamp_ms: int) -> None:
        """Close campaign and compute PnL net of fees."""
        c.phase = "closed"
        c.exit_price = price
        c.close_time_ms = timestamp_ms
        c.exit_reason = reason

        # PnL
        if c.direction == "long":
            c.pnl_usd = (price - c.weighted_entry) * c.total_size
        else:
            c.pnl_usd = (c.weighted_entry - price) * c.total_size

        # Fees (entry + exit notional × fee rate)
        entry_notional = c.weighted_entry * c.total_size
        exit_notional = price * c.total_size
        total_fees = (entry_notional + exit_notional) * settings.TRADING_FEE_RATE
        c.pnl_usd -= total_fees

        self.equity += c.pnl_usd
        self.equity_curve.append((timestamp_ms, self.equity))
        self.closed.append(c)
        self.active = None

        # Mark failed entry so we don't re-enter the same OB
        if c.pnl_usd < 0:
            self._failed_entries.add(
                (c.pair, c.direction, round(c.entry_price, 2))
            )

    def get_closed_campaigns(self) -> list[SimulatedCampaign]:
        """Return closed campaigns excluding entry timeouts."""
        return [c for c in self.closed if c.exit_reason != "entry_timeout"]


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
    # Execution funnel
    pending_created: int = 0
    pending_replaced: int = 0
    pending_filled: int = 0
    fill_rate: float = 0.0
    execution_by_setup: dict = field(default_factory=dict)


def compute_metrics(simulator: TradeSimulator, period_days: float) -> BacktestMetrics:
    """Compute all performance metrics from simulator results."""
    m = BacktestMetrics()
    trades = simulator.get_closed_trades()
    all_closed = simulator.get_all_closed()

    m.total_trades = len(trades)
    m.entry_timeouts = sum(1 for t in all_closed if t.exit_reason == "entry_timeout")

    # Execution funnel from simulator counters
    m.pending_created = simulator._pending_created
    m.pending_replaced = simulator._pending_replaced
    m.pending_filled = simulator._pending_filled
    m.fill_rate = m.pending_filled / m.pending_created if m.pending_created > 0 else 0.0
    for stype, counts in simulator._exec_by_setup.items():
        created = counts["created"]
        m.execution_by_setup[stype] = {
            **counts,
            "fill_rate": counts["filled"] / created if created > 0 else 0.0,
        }

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
# Campaign metrics
# ================================================================

@dataclass
class CampaignMetrics:
    """Computed performance metrics for HTF campaigns."""
    total_campaigns: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_pnl_usd: float = 0.0
    total_pnl_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    profit_factor: float = 0.0
    avg_duration_hours: float = 0.0
    avg_adds: float = 0.0
    entry_timeouts: int = 0
    campaigns_created: int = 0
    campaigns_filled: int = 0
    adds_attempted: int = 0
    adds_filled: int = 0
    by_pair: dict = field(default_factory=dict)
    by_direction: dict = field(default_factory=dict)
    by_setup: dict = field(default_factory=dict)
    exit_reasons: dict = field(default_factory=dict)


def compute_campaign_metrics(csim: CampaignSimulator,
                             period_days: float) -> CampaignMetrics:
    """Compute performance metrics from campaign simulator results."""
    m = CampaignMetrics()
    campaigns = csim.get_closed_campaigns()

    m.total_campaigns = len(campaigns)
    m.entry_timeouts = sum(1 for c in csim.closed if c.exit_reason == "entry_timeout")
    m.campaigns_created = csim._campaigns_created
    m.campaigns_filled = csim._campaigns_filled
    m.adds_attempted = csim._adds_attempted
    m.adds_filled = csim._adds_filled

    if not campaigns:
        return m

    m.wins = sum(1 for c in campaigns if c.pnl_usd > 0)
    m.losses = sum(1 for c in campaigns if c.pnl_usd <= 0)
    m.win_rate = m.wins / m.total_campaigns if m.total_campaigns > 0 else 0.0

    m.total_pnl_usd = sum(c.pnl_usd for c in campaigns)
    m.total_pnl_pct = (m.total_pnl_usd / csim.initial_capital) * 100

    m.max_drawdown_pct = _compute_max_drawdown(csim.equity_curve)

    # Sharpe from daily PnL
    if campaigns and period_days >= 2:
        daily_pnl: dict[str, float] = defaultdict(float)
        for c in campaigns:
            day = datetime.fromtimestamp(
                c.close_time_ms / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%d")
            daily_pnl[day] += c.pnl_usd
        returns = [pnl / csim.initial_capital for pnl in daily_pnl.values()]
        if len(returns) >= 2:
            mean_r = sum(returns) / len(returns)
            variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
            std_r = math.sqrt(variance) if variance > 0 else 0.0
            m.sharpe_ratio = (mean_r / std_r) * math.sqrt(365) if std_r > 0 else 0.0

    # Profit factor
    gross_profit = sum(c.pnl_usd for c in campaigns if c.pnl_usd > 0)
    gross_loss = abs(sum(c.pnl_usd for c in campaigns if c.pnl_usd < 0))
    m.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Average duration
    durations = []
    for c in campaigns:
        if c.entry_time_ms > 0 and c.close_time_ms > 0:
            durations.append((c.close_time_ms - c.entry_time_ms) / (1000 * 3600))
    m.avg_duration_hours = sum(durations) / len(durations) if durations else 0.0

    # Average adds per campaign
    m.avg_adds = sum(len(c.adds) for c in campaigns) / len(campaigns)

    # Breakdowns
    for key_attr, target_dict in [("setup_type", m.by_setup),
                                   ("pair", m.by_pair),
                                   ("direction", m.by_direction)]:
        groups: dict[str, list] = defaultdict(list)
        for c in campaigns:
            groups[getattr(c, key_attr)].append(c)
        for name, group in sorted(groups.items()):
            w = sum(1 for c in group if c.pnl_usd > 0)
            pnl = sum(c.pnl_usd for c in group)
            target_dict[name] = {
                "count": len(group),
                "wins": w,
                "win_rate": w / len(group) if group else 0,
                "pnl": pnl,
            }

    # Exit reasons
    for c in campaigns:
        m.exit_reasons[c.exit_reason] = m.exit_reasons.get(c.exit_reason, 0) + 1

    return m


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
                 period_days: float,
                 setups_found: int, total_evaluated: int,
                 tracker: RejectTracker,
                 setups_deduped: int = 0,
                 ai_stats: dict | None = None) -> None:
    """Print full backtest report to console."""
    print()
    print("=" * 70)
    fill_info = f"fill_mode={simulator.fill_mode}"
    if simulator.fill_probability < 1.0:
        fill_info += f", fill_prob={simulator.fill_probability*100:.0f}%"
    print(f"BACKTEST RESULTS  ({fill_info})")
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

    # -- AI calibration --
    if ai_stats is not None:
        print(f"\n{'='*70}")
        print(f"AI CALIBRATION (Claude evaluation)")
        print(f"{'='*70}")
        sent = ai_stats["total_evaluated_by_ai"]
        approved = ai_stats["ai_approved"]
        rejected = ai_stats["ai_rejected"]
        pre_filtered = ai_stats["ai_pre_filtered"]
        quick_bypass = ai_stats["ai_quick_bypass"]
        errors = ai_stats["ai_errors"]
        print(f"  Setups sent to Claude:  {sent}")
        print(f"  Approved:               {approved}")
        print(f"  Rejected:               {rejected}")
        print(f"  Pre-filtered (no API):  {pre_filtered}")
        print(f"  Quick bypass (C/D/E):   {quick_bypass}")
        if errors > 0:
            print(f"  API errors:             {errors}")
        if sent > 0:
            approval_rate = approved / sent * 100
            print(f"  Claude approval rate:   {approval_rate:.1f}% (target: 30-60%)")
            # Average confidence of approved setups
            approved_decisions = [d for d in ai_stats["ai_decisions"]
                                 if d["result"] == "approved"]
            if approved_decisions:
                avg_conf = sum(d["confidence"] for d in approved_decisions) / len(approved_decisions)
                print(f"  Avg confidence (approved): {avg_conf:.2f}")

    # -- Execution funnel --
    if m.pending_created > 0:
        print(f"\n{'='*70}")
        print(f"EXECUTION FUNNEL")
        print(f"{'='*70}")
        print(f"  Setups detected:     {setups_found}")
        if ai_stats is not None:
            ai_approved = ai_stats.get("ai_approved", 0) + ai_stats.get("ai_quick_bypass", 0)
            print(f"  AI approved:         {ai_approved}")
        print(f"  Risk approved:       {m.pending_created}")
        print(f"  Pending replaced:    {m.pending_replaced}")
        print(f"  Entry timeouts:      {m.entry_timeouts}")
        print(f"  Entries filled:      {m.pending_filled}")
        print(f"  Fill rate:           {m.fill_rate*100:.1f}%")

        if m.execution_by_setup:
            print(f"\n  Per-setup execution:")
            for stype, stats in sorted(m.execution_by_setup.items()):
                print(f"    {stype:<12} created={stats['created']:<4} "
                      f"filled={stats['filled']:<4} timeout={stats['timeout']:<4} "
                      f"replaced={stats['replaced']:<4} "
                      f"fill_rate={stats['fill_rate']*100:5.1f}%")

    # -- Trade simulation --
    # Note: active-trade management (SL/TP/breakeven/trailing) uses OHLC bars.
    # This is an intrabar approximation — we cannot determine the order of
    # high/low within a candle. SL is checked before TP to match live priority.
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
# Campaign report
# ================================================================

def print_campaign_report(m: CampaignMetrics, csim: CampaignSimulator,
                          period_days: float) -> None:
    """Print HTF campaign backtest report."""
    print()
    print("=" * 70)
    print(f"HTF CAMPAIGN BACKTEST  (fill_mode={csim.fill_mode})")
    print("=" * 70)

    print(f"\nCAMPAIGN FUNNEL:")
    print(f"  Campaigns created:   {m.campaigns_created}")
    print(f"  Entry timeouts:      {m.entry_timeouts}")
    print(f"  Campaigns filled:    {m.campaigns_filled}")
    print(f"  Adds attempted:      {m.adds_attempted}")
    print(f"  Adds filled:         {m.adds_filled}")

    campaigns = csim.get_closed_campaigns()
    print(f"\n{'='*70}")
    print(f"CAMPAIGN SIMULATION")
    print(f"{'='*70}")
    print(f"  Initial capital:    ${csim.initial_capital:,.2f}")
    print(f"  Final equity:       ${csim.equity:,.2f}")
    print(f"  Campaigns executed: {m.total_campaigns}")

    if m.total_campaigns == 0:
        print(f"\n  No campaigns executed.")
        print(f"{'='*70}")
        return

    print(f"  Wins / Losses:      {m.wins} / {m.losses}")
    print(f"  Win rate:           {m.win_rate*100:.1f}%")
    print(f"  Avg duration:       {m.avg_duration_hours:.1f}h")
    print(f"  Avg adds/campaign:  {m.avg_adds:.1f}")
    print()

    print(f"PERFORMANCE:")
    print(f"  Total PnL:          ${m.total_pnl_usd:+,.2f} ({m.total_pnl_pct:+.2f}%)")
    print(f"  Profit factor:      {m.profit_factor:.2f}")
    print(f"  Max drawdown:       {m.max_drawdown_pct:.2f}%")
    print(f"  Sharpe ratio:       {m.sharpe_ratio:.2f}")
    print()

    if m.by_setup:
        print(f"BY SETUP TYPE:")
        for name, stats in sorted(m.by_setup.items()):
            print(f"  {name:<12} campaigns={stats['count']:<4} "
                  f"win={stats['win_rate']*100:5.1f}%  "
                  f"PnL=${stats['pnl']:+,.2f}")
        print()

    if m.by_pair:
        print(f"BY PAIR:")
        for name, stats in sorted(m.by_pair.items()):
            print(f"  {name:<12} campaigns={stats['count']:<4} "
                  f"win={stats['win_rate']*100:5.1f}%  "
                  f"PnL=${stats['pnl']:+,.2f}")
        print()

    if m.by_direction:
        print(f"BY DIRECTION:")
        for name, stats in sorted(m.by_direction.items()):
            print(f"  {name:<12} campaigns={stats['count']:<4} "
                  f"win={stats['win_rate']*100:5.1f}%  "
                  f"PnL=${stats['pnl']:+,.2f}")
        print()

    if m.exit_reasons:
        print(f"EXIT REASONS:")
        for reason, count in sorted(m.exit_reasons.items(), key=lambda x: -x[1]):
            pct = count / m.total_campaigns * 100
            print(f"  {reason:<20} {count:>4} ({pct:5.1f}%)")
        print()

    # Campaign log
    if campaigns:
        print(f"CAMPAIGN LOG:")
        print(f"  {'#':<4} {'Entry Time':<17} {'Pair':<10} {'Type':<10} "
              f"{'Dir':<6} {'Entry':>10} {'Exit':>10} {'Adds':>5} "
              f"{'Dur(h)':>7} {'PnL':>10} {'Reason':<14}")
        print(f"  {'-'*110}")
        for i, c in enumerate(campaigns, 1):
            dur_h = ((c.close_time_ms - c.entry_time_ms) / (1000 * 3600)
                     if c.entry_time_ms > 0 else 0)
            print(f"  {i:<4} {_ts_to_str(c.entry_time_ms):<17} {c.pair:<10} "
                  f"{c.setup_type:<10} {c.direction:<6} "
                  f"{c.entry_price:>10.2f} {c.exit_price:>10.2f} "
                  f"{len(c.adds):>5} {dur_h:>7.1f} "
                  f"${c.pnl_usd:>+9.2f} {c.exit_reason:<14}")
        print()

    print("=" * 70)


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

def save_results_json(m: BacktestMetrics, period_days: float,
                      capital: float, pairs: list[str],
                      setups_found: int, setups_deduped: int,
                      ai_stats: dict | None = None) -> str:
    """Save backtest summary to JSON for future reference."""
    results_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "backtest_results")
    os.makedirs(results_dir, exist_ok=True)

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    ai_suffix = "_ai" if ai_stats is not None else ""
    filename = os.path.join(results_dir, f"{ts}_{int(period_days)}d{ai_suffix}.json")

    result = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
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
        "execution_funnel": {
            "pending_created": m.pending_created,
            "pending_replaced": m.pending_replaced,
            "pending_filled": m.pending_filled,
            "entry_timeouts": m.entry_timeouts,
            "fill_rate": round(m.fill_rate * 100, 1),
        },
        "execution_by_setup": m.execution_by_setup,
    }

    # Add AI calibration data if present
    if ai_stats is not None:
        sent = ai_stats["total_evaluated_by_ai"]
        approved_decisions = [d for d in ai_stats["ai_decisions"]
                             if d["result"] == "approved"]
        result["ai_calibration"] = {
            "sent_to_claude": sent,
            "approved": ai_stats["ai_approved"],
            "rejected": ai_stats["ai_rejected"],
            "pre_filtered": ai_stats["ai_pre_filtered"],
            "quick_bypass": ai_stats["ai_quick_bypass"],
            "errors": ai_stats["ai_errors"],
            "approval_rate": round(ai_stats["ai_approved"] / sent * 100, 1) if sent > 0 else 0.0,
            "avg_confidence_approved": (
                round(sum(d["confidence"] for d in approved_decisions) / len(approved_decisions), 3)
                if approved_decisions else 0.0
            ),
        }
        result["ai_decisions"] = ai_stats["ai_decisions"]

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


def save_campaign_results_json(m: CampaignMetrics, period_days: float,
                               capital: float, pairs: list[str]) -> str:
    """Save campaign backtest summary to JSON."""
    results_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "backtest_results")
    os.makedirs(results_dir, exist_ok=True)

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(results_dir, f"{ts}_{int(period_days)}d_campaign.json")

    result = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "mode": "campaign",
        "period_days": round(period_days, 1),
        "capital": capital,
        "pairs": pairs,
        "summary": {
            "total_campaigns": m.total_campaigns,
            "wins": m.wins,
            "losses": m.losses,
            "win_rate": round(m.win_rate * 100, 1),
            "total_pnl_usd": round(m.total_pnl_usd, 2),
            "total_pnl_pct": round(m.total_pnl_pct, 2),
            "max_drawdown_pct": round(m.max_drawdown_pct, 1),
            "sharpe_ratio": round(m.sharpe_ratio, 2),
            "profit_factor": round(m.profit_factor, 2) if m.profit_factor != float("inf") else "inf",
            "avg_duration_hours": round(m.avg_duration_hours, 1),
            "avg_adds": round(m.avg_adds, 1),
        },
        "funnel": {
            "campaigns_created": m.campaigns_created,
            "entry_timeouts": m.entry_timeouts,
            "campaigns_filled": m.campaigns_filled,
            "adds_attempted": m.adds_attempted,
            "adds_filled": m.adds_filled,
        },
        "by_setup": m.by_setup,
        "by_pair": m.by_pair,
        "by_direction": m.by_direction,
        "exit_reasons": m.exit_reasons,
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
                 warmup: int = 50,
                 capital: float = 10000.0, export: bool = False,
                 days: int | None = None, ai_enabled: bool = False,
                 fill_mode: str | None = None,
                 fill_buffer_pct: float | None = None,
                 fill_probability: float | None = None,
                 seed: int = 42,
                 campaign: bool = False,
                 detail: bool = False,
                 overrides: dict | None = None):
    from config.settings import settings

    # Apply settings overrides (for Optuna optimization)
    _originals = {}
    if overrides:
        for key, value in overrides.items():
            if hasattr(settings, key):
                _originals[key] = getattr(settings, key)
                setattr(settings, key, value)

    if pairs is None:
        pairs = settings.TRADING_PAIRS

    all_timeframes = settings.HTF_TIMEFRAMES + settings.LTF_TIMEFRAMES

    # Campaign mode: also load daily candles for HTF bias
    if campaign and "1d" not in all_timeframes:
        all_timeframes = all_timeframes + ["1d"]

    # Connect to PostgreSQL
    pg = PostgresStore()
    if not pg.connect():
        logger.error("Cannot connect to PostgreSQL -- aborting")
        sys.exit(1)

    # Load data — enough for requested days
    load_count = 50000  # Covers 90+ days of 5m data
    data = BacktestDataService()
    data.load_from_postgres(pg, pairs, all_timeframes, count=load_count,
                            load_detail=detail)
    pg.close()

    # AI Service initialization
    ai_service = None
    _loop = None
    if ai_enabled:
        from ai_service import AIService
        ai_service = AIService(data_service=data)
        _loop = asyncio.new_event_loop()
        logger.info("AI calibration mode enabled — Claude will evaluate swing setups")

    # AI stats tracking
    ai_stats = {
        "total_evaluated_by_ai": 0,
        "ai_approved": 0,
        "ai_rejected": 0,
        "ai_pre_filtered": 0,
        "ai_quick_bypass": 0,
        "ai_errors": 0,
        "ai_decisions": [],
    }

    # Set up rejection tracker
    tracker = RejectTracker()
    import loguru
    sink_id = loguru.logger.add(
        tracker.sink,
        filter=lambda record: record["name"].startswith("strategy_"),
        level="DEBUG",
    )

    clock = SimulatedClock()
    fm = fill_mode or settings.BACKTEST_FILL_MODE
    fb = fill_buffer_pct if fill_buffer_pct is not None else settings.BACKTEST_FILL_BUFFER_PCT
    fp = fill_probability if fill_probability is not None else settings.BACKTEST_FILL_PROBABILITY
    simulator = TradeSimulator(initial_capital=capital, fill_mode=fm,
                               fill_buffer_pct=fb, fill_probability=fp,
                               seed=seed,
                               data_service=data if detail else None)

    if fp < 1.0:
        logger.info(f"Fill probability: {fp*100:.0f}% (seed={seed})")

    # Campaign simulator (campaign mode only)
    csim = CampaignSimulator(initial_capital=capital, fill_mode=fm,
                             fill_buffer_pct=fb) if campaign else None

    setups_count = 0
    setups_deduped = 0
    total_evaluated = 0
    total_warmup = 0
    htf_setups_count = 0

    # Campaign dedup cache — prevents re-entering same HTF setup
    _campaign_dedup: dict[tuple, int] = {}
    _CAMPAIGN_DEDUP_TTL_MS = 4 * 3600 * 1000  # 4 hours (one 4H candle)

    # Setup dedup cache — same logic as main.py
    # Key: (pair, direction, setup_type, rounded entry_price)
    # Value: timestamp_ms of last evaluation
    _dedup_cache: dict[tuple, int] = {}
    _DEDUP_TTL_MS = 3600 * 1000  # 1 hour

    for pair in pairs:
        # Campaign mode: include 4H candles for HTF evaluation + LTF for SL granularity
        if campaign:
            campaign_tfs = [settings.HTF_CAMPAIGN_SIGNAL_TF] + settings.LTF_TIMEFRAMES
            trigger_candles = data.get_trigger_candles(pair, campaign_tfs)
        else:
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
                if not campaign:
                    simulator.on_candle(candle)

                # Campaign: process all candles for SL/fill granularity
                if csim is not None:
                    csim.on_candle(candle, strategy)

                is_warmup = i < warmup
                if is_warmup:
                    total_warmup += 1
                    continue

                # Campaign HTF evaluation (4H candles only)
                if csim is not None and candle.timeframe == settings.HTF_CAMPAIGN_SIGNAL_TF:
                    htf_setup = strategy.evaluate_htf(pair, candle)
                    if htf_setup:
                        # Campaign dedup: skip if same setup evaluated recently
                        c_dedup_key = (htf_setup.pair, htf_setup.direction,
                                       htf_setup.setup_type,
                                       round(htf_setup.entry_price, 2))
                        c_last_eval = _campaign_dedup.get(c_dedup_key, 0)
                        if (candle.timestamp - c_last_eval) < _CAMPAIGN_DEDUP_TTL_MS:
                            continue
                        _campaign_dedup[c_dedup_key] = candle.timestamp

                        htf_setups_count += 1
                        if csim.active is not None and csim.pending is None:
                            taken = csim.try_add(csim.active, htf_setup, candle)
                            if verbose and taken:
                                print(f"  [CAMPAIGN ADD] {_ts_to_str(candle.timestamp)} "
                                      f"{htf_setup.setup_type} {htf_setup.direction} "
                                      f"entry={htf_setup.entry_price:.2f}")
                        elif csim.active is None and csim.pending is None:
                            taken = csim.on_setup(htf_setup, candle)
                            if verbose and taken:
                                print(f"  [CAMPAIGN NEW] {_ts_to_str(candle.timestamp)} "
                                      f"{htf_setup.setup_type} {htf_setup.direction} "
                                      f"entry={htf_setup.entry_price:.2f} "
                                      f"sl={htf_setup.sl_price:.2f}")
                    continue  # 4H candles skip intraday evaluation

                # Skip intraday evaluation in campaign-only mode
                if campaign:
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

                    # AI evaluation (when --ai is active)
                    ai_passed = True
                    if ai_enabled and ai_service is not None:
                        if setup.setup_type in QUICK_SETUP_TYPES:
                            # Quick setups bypass Claude (same as production)
                            ai_stats["ai_quick_bypass"] += 1
                        else:
                            # Swing setups go through pre-filter + Claude
                            snapshot = data.get_market_snapshot(setup.pair)
                            pre_filter_reason = _pre_filter_for_claude(setup, snapshot)
                            if pre_filter_reason:
                                ai_stats["ai_pre_filtered"] += 1
                                ai_stats["ai_decisions"].append({
                                    "timestamp": candle.timestamp,
                                    "pair": setup.pair,
                                    "direction": setup.direction,
                                    "setup_type": setup.setup_type,
                                    "result": "pre_filtered",
                                    "reason": pre_filter_reason,
                                    "confidence": 0.0,
                                })
                                ai_passed = False
                                if verbose:
                                    print(f"  [AI PRE-FILTER] {_ts_to_str(candle.timestamp)} "
                                          f"{setup.setup_type} {setup.direction} "
                                          f"— {pre_filter_reason}")
                            else:
                                # Call Claude
                                ai_stats["total_evaluated_by_ai"] += 1
                                try:
                                    decision = _loop.run_until_complete(
                                        ai_service.evaluate(setup, snapshot)
                                    )
                                    ai_stats["ai_decisions"].append({
                                        "timestamp": candle.timestamp,
                                        "pair": setup.pair,
                                        "direction": setup.direction,
                                        "setup_type": setup.setup_type,
                                        "result": "approved" if decision.approved else "rejected",
                                        "confidence": decision.confidence,
                                        "reasoning": decision.reasoning,
                                    })
                                    if decision.approved:
                                        ai_stats["ai_approved"] += 1
                                        if verbose:
                                            print(f"  [AI APPROVED] {_ts_to_str(candle.timestamp)} "
                                                  f"{setup.setup_type} {setup.direction} "
                                                  f"conf={decision.confidence:.2f}")
                                    else:
                                        ai_stats["ai_rejected"] += 1
                                        ai_passed = False
                                        if verbose:
                                            print(f"  [AI REJECTED] {_ts_to_str(candle.timestamp)} "
                                                  f"{setup.setup_type} {setup.direction} "
                                                  f"conf={decision.confidence:.2f} "
                                                  f"— {decision.reasoning[:80]}")
                                except Exception as e:
                                    ai_stats["ai_errors"] += 1
                                    ai_passed = False
                                    logger.error(f"AI evaluation error: {e}")

                    if not ai_passed:
                        continue

                    taken = simulator.on_setup(setup, candle)
                    if verbose:
                        status = "TAKEN" if taken else "SKIP (risk)"
                        print(f"  [SETUP {status}] {_ts_to_str(candle.timestamp)} "
                              f"{candle.timeframe} {setup.setup_type} "
                              f"{setup.direction} entry={setup.entry_price:.2f}")
                elif verbose:
                    reasons = tracker.last_reasons or ["passed_htf_but_no_setup"]
                    print(f"  [--] {_ts_to_str(candle.timestamp)} "
                          f"{candle.timeframe} close={candle.close:.2f} "
                          f"reason={reasons[-1] if reasons else '?'}")

    loguru.logger.remove(sink_id)

    # AI cleanup
    if ai_service is not None and _loop is not None:
        try:
            _loop.run_until_complete(ai_service.close())
        except Exception:
            pass
        _loop.close()
        logger.info(
            f"AI calibration: {ai_stats['total_evaluated_by_ai']} sent to Claude, "
            f"{ai_stats['ai_approved']} approved, {ai_stats['ai_rejected']} rejected, "
            f"{ai_stats['ai_pre_filtered']} pre-filtered, "
            f"{ai_stats['ai_quick_bypass']} quick bypass"
        )

    # Compute period
    all_trigger = []
    period_tfs = ([settings.HTF_CAMPAIGN_SIGNAL_TF] + settings.LTF_TIMEFRAMES
                  if campaign else settings.LTF_TIMEFRAMES)
    for pair in pairs:
        tc = data.get_trigger_candles(pair, period_tfs)
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

    # Restore overridden settings
    if _originals:
        for key, value in _originals.items():
            setattr(settings, key, value)

    # Campaign mode: print campaign results
    if csim is not None:
        campaign_metrics = compute_campaign_metrics(csim, period_days)
        print_campaign_report(campaign_metrics, csim, period_days)
        save_campaign_results_json(campaign_metrics, period_days, capital, pairs)
        return campaign_metrics

    # Compute metrics and print report
    metrics = compute_metrics(simulator, period_days)
    ai_report = ai_stats if ai_enabled else None
    print_report(metrics, simulator, period_days,
                 setups_count, total_evaluated, tracker,
                 setups_deduped=setups_deduped, ai_stats=ai_report)

    # Always save JSON summary
    save_results_json(metrics, period_days, capital, pairs,
                      setups_count, setups_deduped, ai_stats=ai_report)

    # CSV export
    if export:
        trades = simulator.get_closed_trades()
        if trades:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"backtest_results_{ts}.csv"
            export_csv(trades, filename)

    return metrics


def main():
    parser = argparse.ArgumentParser(
        description="Backtest: replay candles + simulate trades")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print per-candle evaluation results")
    parser.add_argument("--pair", type=str, default=None,
                        help="Single pair to test (e.g. BTC/USDT)")
    parser.add_argument("--warmup", type=int, default=50,
                        help="Number of warmup candles to skip (default: 50)")
    parser.add_argument("--capital", type=float, default=10000.0,
                        help="Initial capital in USDT (default: 10000)")
    parser.add_argument("--days", type=int, default=None,
                        help="Limit to last N days of data")
    parser.add_argument("--csv", action="store_true",
                        help="Export results to CSV file")
    parser.add_argument("--ai", action="store_true",
                        help="Enable Claude AI evaluation on swing setups")
    parser.add_argument("--fill-mode", choices=["optimistic", "conservative"],
                        default=None,
                        help="Fill model: optimistic (touch=fill) or conservative (penetrate by buffer)")
    parser.add_argument("--fill-buffer", type=float, default=None,
                        help="Fill buffer %% for conservative mode (default: 0.001 = 0.1%%)")
    parser.add_argument("--fill-prob", type=float, default=None,
                        help="Fill probability 0.0-1.0 (default: 1.0 = always fill)")
    parser.add_argument("--seed", type=int, default=42,
                        help="RNG seed for fill probability (default: 42)")
    parser.add_argument("--detail", action="store_true",
                        help="Timeframe-detail mode: load 1m candles to resolve ambiguous SL/TP ordering")
    parser.add_argument("--campaign", action="store_true",
                        help="Run HTF campaign backtest (4H setups, pyramid adds, trailing SL)")
    args = parser.parse_args()

    pairs = [args.pair] if args.pair else None
    run_backtest(
        pairs=pairs,
        verbose=args.verbose,
        warmup=args.warmup,
        capital=args.capital,
        days=args.days,
        export=args.csv,
        ai_enabled=args.ai,
        fill_mode=args.fill_mode,
        fill_buffer_pct=args.fill_buffer,
        fill_probability=args.fill_prob,
        seed=args.seed,
        campaign=args.campaign,
        detail=args.detail,
    )


if __name__ == "__main__":
    main()
