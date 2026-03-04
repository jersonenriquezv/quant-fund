#!/usr/bin/env python3
"""
Backtester — Replay historical candles through StrategyService.

Loads candles from PostgreSQL (backfilled at startup), replays them
candle-by-candle in chronological order, and reports how often the
strategy produces setups (and why it doesn't).

Usage:
    python scripts/backtest.py
    python scripts/backtest.py --profile aggressive
    python scripts/backtest.py --profile scalping --verbose
    python scripts/backtest.py --pair BTC/USDT --profile scalping
    python scripts/backtest.py --warmup 100
"""

import argparse
import re
import sys
import os
from datetime import datetime, timezone
from unittest.mock import patch

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_service.data_store import PostgresStore
from shared.models import Candle, MarketSnapshot
from strategy_service.service import StrategyService
from shared.logger import setup_logger

logger = setup_logger("backtest")


# ================================================================
# BacktestDataService — mock DataService for replay
# ================================================================

class BacktestDataService:
    """Mock DataService that serves candles up to a simulated time cursor.

    Implements get_candles() and get_market_snapshot() — the only two
    methods StrategyService.evaluate() calls on the data service.
    """

    def __init__(self):
        # {(pair, timeframe): [Candle, ...]} sorted oldest-first
        self._candles: dict[tuple[str, str], list[Candle]] = {}
        self._current_time_ms: int = 0

    def load_from_postgres(self, pg: PostgresStore, pairs: list[str],
                           timeframes: list[str], count: int = 500):
        """Load historical candles from PostgreSQL."""
        for pair in pairs:
            for tf in timeframes:
                candles = pg.load_candles(pair, tf, count)
                if candles:
                    self._candles[(pair, tf)] = candles
                    logger.info(f"Loaded {len(candles)} candles: {pair} {tf} "
                                f"[{_ts_to_str(candles[0].timestamp)} → "
                                f"{_ts_to_str(candles[-1].timestamp)}]")
                else:
                    logger.warning(f"No candles in DB: {pair} {tf}")

    def set_time(self, time_ms: int):
        self._current_time_ms = time_ms

    def get_candles(self, pair: str, timeframe: str,
                    count: int = 100) -> list[Candle]:
        """Return last `count` candles with timestamp <= current cursor."""
        all_candles = self._candles.get((pair, timeframe), [])
        visible = [c for c in all_candles if c.timestamp <= self._current_time_ms]
        return visible[-count:]

    def get_market_snapshot(self, pair: str):
        """No historical OI/CVD/funding — return None."""
        return None

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

    # (regex_pattern, category_name)
    PATTERNS = [
        (r"No HTF bias", "no_htf_bias"),
        (r"Setup A.*no recent sweeps", "setup_a_no_sweeps"),
        (r"Setup A.*no CHoCH", "setup_a_no_choch"),
        (r"Setup A.*CHoCH.*!= HTF", "setup_a_choch_htf_mismatch"),
        (r"Setup A.*no aligned sweep before", "setup_a_no_aligned_sweep"),
        (r"Setup A.*PD misaligned", "setup_a_pd_misaligned"),
        (r"Setup A.*no aligned OBs", "setup_a_no_obs"),
        (r"Setup A.*OBs exist but price not near", "setup_a_ob_price_far"),
        (r"Setup A.*price not near best OB", "setup_a_price_not_near_ob"),
        (r"Setup B.*no BOS", "setup_b_no_bos"),
        (r"Setup B.*BOS.*!= HTF", "setup_b_bos_htf_mismatch"),
        (r"Setup B.*PD misaligned", "setup_b_pd_misaligned"),
        (r"Setup B.*no aligned OBs", "setup_b_no_obs"),
        (r"Setup B.*no aligned FVGs", "setup_b_no_fvgs"),
        (r"Setup B.*no adjacent OB\+FVG", "setup_b_no_ob_fvg_pair"),
        (r"Setup B.*price not near OB", "setup_b_price_not_near_ob"),
    ]

    def __init__(self):
        self.counts: dict[str, int] = {}
        self.last_reasons: list[str] = []
        self._compiled = [(re.compile(p), cat) for p, cat in self.PATTERNS]

    def sink(self, message):
        """Loguru sink — receives log records from strategy_service.*"""
        text = str(message)
        for regex, category in self._compiled:
            if regex.search(text):
                self.counts[category] = self.counts.get(category, 0) + 1
                self.last_reasons.append(category)
                return

    def reset_last(self):
        self.last_reasons.clear()


# ================================================================
# Helpers
# ================================================================

def _ts_to_str(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _ts_to_date(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


# ================================================================
# Main backtest
# ================================================================

def run_backtest(pairs: list[str] | None = None, verbose: bool = False,
                 warmup: int = 50, profile: str = "default"):
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
        logger.error("Cannot connect to PostgreSQL — aborting")
        sys.exit(1)

    # Load data
    data = BacktestDataService()
    data.load_from_postgres(pg, pairs, all_timeframes, count=2000)
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
    setups_found = []
    total_evaluated = 0
    total_warmup = 0

    for pair in pairs:
        trigger_candles = data.get_trigger_candles(pair, settings.LTF_TIMEFRAMES)
        if not trigger_candles:
            logger.warning(f"No trigger candles for {pair}")
            continue

        logger.info(f"Replaying {pair}: {len(trigger_candles)} LTF candles "
                    f"[{_ts_to_str(trigger_candles[0].timestamp)} → "
                    f"{_ts_to_str(trigger_candles[-1].timestamp)}]")

        # Fresh StrategyService per pair — detectors accumulate state
        strategy = StrategyService(data)

        # Patch time.time() in the two files that call it
        with patch("strategy_service.service.time.time", clock), \
             patch("strategy_service.setups.time.time", clock):

            for i, candle in enumerate(trigger_candles):
                clock.set_ms(candle.timestamp)
                data.set_time(candle.timestamp)
                tracker.reset_last()

                setup = strategy.evaluate(pair, candle)

                is_warmup = i < warmup
                if is_warmup:
                    total_warmup += 1
                    if verbose:
                        label = "WARMUP"
                        if setup:
                            label += " SETUP!"
                        print(f"  [{label}] {_ts_to_str(candle.timestamp)} "
                              f"{candle.timeframe} close={candle.close:.2f}")
                    continue

                total_evaluated += 1

                if setup:
                    setups_found.append(setup)
                    if verbose:
                        print(f"  [SETUP] {_ts_to_str(candle.timestamp)} "
                              f"{candle.timeframe} {setup.setup_type} "
                              f"{setup.direction} entry={setup.entry_price:.2f} "
                              f"confluences={setup.confluences}")
                elif verbose:
                    reasons = tracker.last_reasons or ["passed_htf_but_no_setup"]
                    print(f"  [--] {_ts_to_str(candle.timestamp)} "
                          f"{candle.timeframe} close={candle.close:.2f} "
                          f"reason={reasons[-1] if reasons else '?'}")

    loguru.logger.remove(sink_id)

    # ============================================================
    # Print results
    # ============================================================
    print()
    print("=" * 70)
    print(f"BACKTEST RESULTS — profile: {profile}")
    print("=" * 70)

    # Determine period
    all_trigger = []
    for pair in pairs:
        all_trigger.extend(data.get_trigger_candles(pair, settings.LTF_TIMEFRAMES))
    if all_trigger:
        first_ts = min(c.timestamp for c in all_trigger)
        last_ts = max(c.timestamp for c in all_trigger)
        days = (last_ts - first_ts) / (1000 * 86400)
        print(f"Period: {_ts_to_date(first_ts)} to {_ts_to_date(last_ts)} "
              f"({days:.1f} days)")
    else:
        print("Period: no data")

    total_candles = total_warmup + total_evaluated
    print(f"Candles processed: {total_candles} "
          f"(warmup: {total_warmup}, evaluated: {total_evaluated})")
    print()

    # Setups summary
    setup_a = [s for s in setups_found if s.setup_type == "setup_a"]
    setup_b = [s for s in setups_found if s.setup_type == "setup_b"]

    print(f"SETUPS FOUND: {len(setups_found)}")
    print(f"  Setup A: {len(setup_a)}  "
          f"(long: {sum(1 for s in setup_a if s.direction == 'long')}, "
          f"short: {sum(1 for s in setup_a if s.direction == 'short')})")
    print(f"  Setup B: {len(setup_b)}  "
          f"(long: {sum(1 for s in setup_b if s.direction == 'long')}, "
          f"short: {sum(1 for s in setup_b if s.direction == 'short')})")
    if all_trigger:
        setups_per_day = len(setups_found) / max(days, 0.1)
        print(f"  Rate: ~{setups_per_day:.1f} setups/day")
    print()

    # Rejection breakdown
    if tracker.counts and total_evaluated > 0:
        print(f"REJECTION BREAKDOWN ({total_evaluated} evaluations):")
        sorted_reasons = sorted(tracker.counts.items(), key=lambda x: -x[1])
        for reason, count in sorted_reasons:
            pct = count / total_evaluated * 100
            label = reason.replace("_", " ").title()
            print(f"  {label:<40} {count:>5} ({pct:>5.1f}%)")
        print()

    # Detected setups detail
    if setups_found:
        print("DETECTED SETUPS:")
        for i, s in enumerate(setups_found, 1):
            print(f"  #{i:<3} {_ts_to_str(s.timestamp)}  {s.pair:<10} "
                  f"{s.setup_type}  {s.direction:<5}  "
                  f"entry={s.entry_price:.2f}  sl={s.sl_price:.2f}  "
                  f"tp1={s.tp1_price:.2f}")
            print(f"       confluences={s.confluences}")
        print()

    print("=" * 70)

    if not setups_found and total_evaluated > 0:
        print()
        print("Zero setups in the test period. Consider:")
        print("  - Market may genuinely not have aligned conditions")
        print("  - Check if OB_PROXIMITY_PCT (currently "
              f"{settings.OB_PROXIMITY_PCT}) is too tight")
        print("  - Check if BOS_CONFIRMATION_PCT (currently "
              f"{settings.BOS_CONFIRMATION_PCT}) filters too aggressively")
        print("  - Run with --verbose to see per-candle rejection reasons")
        print()

    # Restore default settings
    reset_profile(settings)


def main():
    from config.settings import STRATEGY_PROFILES, Settings

    available = ", ".join(STRATEGY_PROFILES.keys())
    parser = argparse.ArgumentParser(
        description="Replay historical candles through StrategyService")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print per-candle evaluation results")
    parser.add_argument("--pair", type=str, default=None,
                        help="Single pair to test (e.g. BTC/USDT)")
    parser.add_argument("--warmup", type=int, default=50,
                        help="Number of warmup candles to skip (default: 50)")
    parser.add_argument("--profile", type=str, default="default",
                        help=f"Strategy profile ({available})")
    args = parser.parse_args()

    pairs = [args.pair] if args.pair else None
    run_backtest(pairs=pairs, verbose=args.verbose, warmup=args.warmup,
                 profile=args.profile)


if __name__ == "__main__":
    main()
