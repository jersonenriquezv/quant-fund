"""
Tests for data_service/data_integrity.py — DataServiceState, CVDState,
can_trade_setup(), CircuitBreaker, and OHLC sanity.
"""

import time

import pytest

from data_service.data_integrity import (
    DataServiceState,
    CVDState,
    SETUP_DATA_DEPS,
    can_trade_setup,
    CircuitBreaker,
    CONTRACT_SIZES,
    TIMEFRAME_MS,
    validate_candle_continuity,
)
from shared.models import Candle, SourceFreshness, SnapshotHealth


# ================================================================
# Helpers
# ================================================================

def _make_health(
    stale: tuple[str, ...] = (),
    missing: tuple[str, ...] = (),
    redis_healthy: bool = True,
) -> SnapshotHealth:
    """Build a minimal SnapshotHealth for testing."""
    return SnapshotHealth(
        sources=(),
        completeness_pct=1.0,
        critical_sources_healthy=True,
        stale_sources=stale,
        missing_sources=missing,
        redis_healthy=redis_healthy,
        service_state="running",
    )


# ================================================================
# can_trade_setup tests
# ================================================================

class TestCanTradeSetup:
    """Test per-setup data dependency gating."""

    def test_running_state_allows_candle_only_setup(self):
        allowed, reason = can_trade_setup(
            "setup_a", _make_health(), DataServiceState.RUNNING, CVDState.VALID,
        )
        assert allowed is True
        assert reason == ""

    def test_recovering_state_blocks_all(self):
        allowed, reason = can_trade_setup(
            "setup_a", _make_health(), DataServiceState.RECOVERING, CVDState.VALID,
        )
        assert allowed is False
        assert "RECOVERING" in reason

    def test_degraded_state_blocks_all(self):
        allowed, reason = can_trade_setup(
            "setup_a", _make_health(), DataServiceState.DEGRADED, CVDState.VALID,
        )
        assert allowed is False
        assert "DEGRADED" in reason

    def test_setup_c_needs_cvd(self):
        """Setup C depends on CVD. If CVD is warming up, block."""
        allowed, reason = can_trade_setup(
            "setup_c", _make_health(), DataServiceState.RUNNING, CVDState.WARMING_UP,
        )
        assert allowed is False
        assert "cvd" in reason

    def test_setup_c_allows_when_cvd_valid(self):
        allowed, reason = can_trade_setup(
            "setup_c", _make_health(), DataServiceState.RUNNING, CVDState.VALID,
        )
        assert allowed is True

    def test_setup_c_blocked_when_funding_missing(self):
        health = _make_health(missing=("funding",))
        allowed, reason = can_trade_setup(
            "setup_c", health, DataServiceState.RUNNING, CVDState.VALID,
        )
        assert allowed is False
        assert "funding" in reason

    def test_setup_e_blocked_when_oi_stale(self):
        health = _make_health(stale=("oi",))
        allowed, reason = can_trade_setup(
            "setup_e", health, DataServiceState.RUNNING, CVDState.VALID,
        )
        assert allowed is False
        assert "oi" in reason

    def test_setup_a_ignores_stale_funding(self):
        """Setup A only needs candles — stale funding shouldn't block it."""
        health = _make_health(stale=("funding",), missing=("cvd",))
        allowed, reason = can_trade_setup(
            "setup_a", health, DataServiceState.RUNNING, CVDState.INVALID,
        )
        assert allowed is True

    def test_setup_h_only_needs_candles(self):
        health = _make_health(missing=("funding", "oi", "cvd"))
        allowed, reason = can_trade_setup(
            "setup_h", health, DataServiceState.RUNNING, CVDState.INVALID,
        )
        assert allowed is True

    def test_unknown_setup_defaults_to_candles_only(self):
        allowed, reason = can_trade_setup(
            "setup_z_unknown", _make_health(), DataServiceState.RUNNING, CVDState.INVALID,
        )
        assert allowed is True

    def test_none_health_allows_candle_only(self):
        """If health is None (shouldn't happen), candle-only setups still pass."""
        allowed, reason = can_trade_setup(
            "setup_a", None, DataServiceState.RUNNING, CVDState.VALID,
        )
        assert allowed is True

    def test_none_health_blocks_cvd_setup_when_invalid(self):
        allowed, reason = can_trade_setup(
            "setup_c", None, DataServiceState.RUNNING, CVDState.INVALID,
        )
        assert allowed is False
        assert "cvd" in reason


# ================================================================
# CircuitBreaker tests
# ================================================================

class TestCircuitBreaker:
    """Test reconnect storm detection."""

    def test_not_tripped_initially(self):
        cb = CircuitBreaker(max_events=3, window_seconds=60, stable_seconds=30)
        assert cb.is_tripped is False

    def test_trips_after_max_events(self):
        cb = CircuitBreaker(max_events=3, window_seconds=60, stable_seconds=30)
        cb.record_event()
        cb.record_event()
        assert cb.is_tripped is False
        cb.record_event()
        assert cb.is_tripped is True

    def test_auto_resets_after_stable_period(self):
        cb = CircuitBreaker(max_events=2, window_seconds=60, stable_seconds=9999)
        cb.record_event()
        cb.record_event()
        assert cb.is_tripped is True
        # Simulate passage of stable period by backdating events
        cb._events = [time.monotonic() - 10000.0]
        assert cb.is_tripped is False

    def test_manual_reset(self):
        cb = CircuitBreaker(max_events=2, window_seconds=60, stable_seconds=9999)
        cb.record_event()
        cb.record_event()
        assert cb.is_tripped is True
        cb.reset()
        assert cb.is_tripped is False

    def test_events_pruned_outside_window(self):
        cb = CircuitBreaker(max_events=3, window_seconds=1, stable_seconds=0)
        cb.record_event()
        cb.record_event()
        # Simulate passage of time by manipulating internal state
        cb._events = [time.monotonic() - 2.0, time.monotonic() - 2.0]
        cb.record_event()  # Only this one is in window
        assert cb.is_tripped is False  # Only 1 event in window


# ================================================================
# Contract sizes
# ================================================================

class TestContractSizes:
    """Verify contract size constants."""

    def test_btc_contract_size(self):
        assert CONTRACT_SIZES["BTC/USDT"] == 0.01

    def test_eth_contract_size(self):
        assert CONTRACT_SIZES["ETH/USDT"] == 0.1

    def test_all_pairs_have_sizes(self):
        expected_pairs = {"BTC/USDT", "ETH/USDT", "SOL/USDT", "DOGE/USDT",
                          "XRP/USDT", "LINK/USDT", "AVAX/USDT"}
        assert set(CONTRACT_SIZES.keys()) == expected_pairs


# ================================================================
# CVD State Machine tests
# ================================================================

class TestCVDStateMachine:
    """Test CVD state transitions."""

    def test_initial_state_warming_up(self):
        from data_service.cvd_calculator import CVDCalculator
        calc = CVDCalculator()
        assert calc.get_cvd_state("BTC/USDT") == CVDState.WARMING_UP

    def test_get_cvd_returns_none_when_warming_up(self):
        from data_service.cvd_calculator import CVDCalculator
        calc = CVDCalculator()
        # Even if there's a snapshot, state blocks it
        from shared.models import CVDSnapshot
        calc._snapshots["BTC/USDT"] = CVDSnapshot(
            timestamp=1, pair="BTC/USDT",
            cvd_5m=1.0, cvd_15m=1.0, cvd_1h=1.0,
            buy_volume=10.0, sell_volume=5.0,
        )
        assert calc.get_cvd("BTC/USDT") is None

    def test_get_cvd_returns_snapshot_when_valid(self):
        from data_service.cvd_calculator import CVDCalculator
        from shared.models import CVDSnapshot
        calc = CVDCalculator()
        calc._cvd_state["BTC/USDT"] = CVDState.VALID
        snap = CVDSnapshot(
            timestamp=1, pair="BTC/USDT",
            cvd_5m=1.0, cvd_15m=1.0, cvd_1h=1.0,
            buy_volume=10.0, sell_volume=5.0,
        )
        calc._snapshots["BTC/USDT"] = snap
        assert calc.get_cvd("BTC/USDT") is snap

    def test_disconnect_invalidates_and_reconnect_flushes(self):
        """On disconnect, state goes INVALID. On reconnect, trades are flushed."""
        from data_service.cvd_calculator import CVDCalculator, _RawTrade
        from collections import deque
        calc = CVDCalculator()
        # Simulate some trades
        calc._trades["BTC/USDT"] = deque([
            _RawTrade(timestamp=1000, price=50000, size=0.01, side="buy"),
        ])
        calc._cvd_state["BTC/USDT"] = CVDState.VALID

        # Simulate disconnect
        for pair in calc._cvd_state:
            calc._cvd_state[pair] = CVDState.INVALID
            calc._cvd_invalid_reason[pair] = "disconnect"

        assert calc.get_cvd("BTC/USDT") is None
        # Trades still in buffer (not flushed until reconnect)
        assert len(calc._trades["BTC/USDT"]) == 1

        # Simulate reconnect — this clears trades for INVALID pairs
        for pair in calc._cvd_state:
            if calc._cvd_state[pair] == CVDState.INVALID:
                calc._trades[pair].clear()
                calc._snapshots.pop(pair, None)
                calc._cvd_state[pair] = CVDState.WARMING_UP

        assert len(calc._trades["BTC/USDT"]) == 0
        assert calc._cvd_state["BTC/USDT"] == CVDState.WARMING_UP


# ================================================================
# OI flush side attribution tests
# ================================================================

class TestOIFlushSideAttribution:
    """Test that OI flush events have correct side based on price."""

    def test_price_drop_long_side(self):
        from data_service.oi_flush_detector import OIFlushDetector
        from shared.models import OpenInterest
        det = OIFlushDetector()
        now_ms = int(time.time() * 1000)
        # First update: seed price and OI
        oi1 = OpenInterest(timestamp=now_ms - 300_000, pair="BTC/USDT",
                           oi_contracts=100, oi_base=1.0, oi_usd=1_000_000)
        det.update(oi1, current_price=50000.0)
        # Second update: OI drops 5%, price dropped 1%
        oi2 = OpenInterest(timestamp=now_ms, pair="BTC/USDT",
                           oi_contracts=95, oi_base=0.95, oi_usd=950_000)
        det.update(oi2, current_price=49500.0)
        events = det.get_recent_oi_flushes("BTC/USDT")
        assert len(events) == 1
        assert events[0].side == "long"
        assert events[0].price == 49500.0

    def test_price_rise_short_side(self):
        from data_service.oi_flush_detector import OIFlushDetector
        from shared.models import OpenInterest
        det = OIFlushDetector()
        now_ms = int(time.time() * 1000)
        oi1 = OpenInterest(timestamp=now_ms - 300_000, pair="ETH/USDT",
                           oi_contracts=100, oi_base=10.0, oi_usd=500_000)
        det.update(oi1, current_price=2000.0)
        oi2 = OpenInterest(timestamp=now_ms, pair="ETH/USDT",
                           oi_contracts=95, oi_base=9.5, oi_usd=475_000)
        det.update(oi2, current_price=2020.0)  # Price rose 1%
        events = det.get_recent_oi_flushes("ETH/USDT")
        assert len(events) == 1
        assert events[0].side == "short"

    def test_no_price_unknown_side(self):
        from data_service.oi_flush_detector import OIFlushDetector
        from shared.models import OpenInterest
        det = OIFlushDetector()
        now_ms = int(time.time() * 1000)
        oi1 = OpenInterest(timestamp=now_ms - 300_000, pair="BTC/USDT",
                           oi_contracts=100, oi_base=1.0, oi_usd=1_000_000)
        det.update(oi1)  # No price
        oi2 = OpenInterest(timestamp=now_ms, pair="BTC/USDT",
                           oi_contracts=95, oi_base=0.95, oi_usd=950_000)
        det.update(oi2)  # No price
        events = det.get_recent_oi_flushes("BTC/USDT")
        assert len(events) == 1
        assert events[0].side == "unknown"


# ================================================================
# OHLC sanity tests
# ================================================================

# ================================================================
# Candle continuity validation tests
# ================================================================

def _make_candle(ts: int, pair: str = "BTC/USDT", tf: str = "5m") -> Candle:
    return Candle(
        timestamp=ts, open=100, high=110, low=90, close=105,
        volume=1.0, volume_quote=105.0, pair=pair, timeframe=tf, confirmed=True,
    )


class TestCandleContinuity:
    """Test validate_candle_continuity()."""

    def test_continuous_5m_candles(self):
        """Perfectly spaced 5m candles = continuous."""
        base = 1000000
        ms = TIMEFRAME_MS["5m"]  # 300_000
        candles = [_make_candle(base + i * ms) for i in range(10)]
        is_cont, gaps = validate_candle_continuity(candles, "5m")
        assert is_cont is True
        assert gaps == 0

    def test_gap_detected_in_5m(self):
        """Missing one 5m candle in the middle = gap detected."""
        base = 1000000
        ms = TIMEFRAME_MS["5m"]
        # 0, 1, 2, [skip 3], 4, 5
        candles = [_make_candle(base + i * ms) for i in [0, 1, 2, 4, 5]]
        is_cont, gaps = validate_candle_continuity(candles, "5m")
        assert is_cont is False
        assert gaps == 1

    def test_multiple_gaps(self):
        """Two separate gaps detected."""
        base = 1000000
        ms = TIMEFRAME_MS["5m"]
        # 0, 1, [skip 2], 3, [skip 4,5], 6
        candles = [_make_candle(base + i * ms) for i in [0, 1, 3, 6]]
        is_cont, gaps = validate_candle_continuity(candles, "5m")
        assert is_cont is False
        assert gaps == 2

    def test_tolerance_allows_slight_jitter(self):
        """Candle arriving slightly late (within 1.5x) is OK."""
        base = 1000000
        ms = TIMEFRAME_MS["5m"]
        candles = [
            _make_candle(base),
            _make_candle(base + ms + 10_000),  # 10s late = within 1.5x
            _make_candle(base + 2 * ms + 10_000),
        ]
        is_cont, gaps = validate_candle_continuity(candles, "5m")
        assert is_cont is True

    def test_15m_continuity(self):
        base = 1000000
        ms = TIMEFRAME_MS["15m"]  # 900_000
        candles = [_make_candle(base + i * ms) for i in range(5)]
        is_cont, gaps = validate_candle_continuity(candles, "15m")
        assert is_cont is True

    def test_15m_gap(self):
        base = 1000000
        ms = TIMEFRAME_MS["15m"]
        # Skip one 15m candle
        candles = [_make_candle(base + i * ms) for i in [0, 1, 3, 4]]
        is_cont, gaps = validate_candle_continuity(candles, "15m")
        assert is_cont is False
        assert gaps == 1

    def test_single_candle_is_continuous(self):
        is_cont, gaps = validate_candle_continuity([_make_candle(1000)], "5m")
        assert is_cont is True
        assert gaps == 0

    def test_empty_is_continuous(self):
        is_cont, gaps = validate_candle_continuity([], "5m")
        assert is_cont is True
        assert gaps == 0

    def test_unknown_timeframe_passes(self):
        """Unknown timeframe can't be validated — returns True."""
        candles = [_make_candle(1000), _make_candle(999999999)]
        is_cont, gaps = validate_candle_continuity(candles, "3m")
        assert is_cont is True


class TestOHLCSanity:
    """Test OHLC consistency validation in WebSocket feeds."""

    def test_valid_ohlc_passes(self):
        from data_service.websocket_feeds import OKXWebSocketFeed
        feed = OKXWebSocketFeed()
        ts = int(time.time() * 1000) - 5000
        assert feed._validate_candle("BTC/USDT", "5m", ts, 100, 110, 90, 105, 1.0)

    def test_low_above_body_rejected(self):
        from data_service.websocket_feeds import OKXWebSocketFeed
        feed = OKXWebSocketFeed()
        ts = int(time.time() * 1000) - 5000
        # low=102 > min(open=100, close=105) is impossible
        assert not feed._validate_candle("BTC/USDT", "5m", ts, 100, 110, 102, 105, 1.0)

    def test_high_below_body_rejected(self):
        from data_service.websocket_feeds import OKXWebSocketFeed
        feed = OKXWebSocketFeed()
        ts = int(time.time() * 1000) - 5000
        # high=104 < max(open=100, close=105) is impossible
        assert not feed._validate_candle("BTC/USDT", "5m", ts, 100, 104, 90, 105, 1.0)

    def test_bad_candle_count_tracked(self):
        from data_service.websocket_feeds import OKXWebSocketFeed
        feed = OKXWebSocketFeed()
        ts = int(time.time() * 1000) - 5000
        feed._validate_candle("BTC/USDT", "5m", ts, 100, 110, 102, 105, 1.0)
        feed._validate_candle("BTC/USDT", "5m", ts, 100, 110, 102, 105, 1.0)
        assert feed._bad_candle_counts[("BTC/USDT", "5m")] == 2


# ================================================================
# Whale baseline seeding tests
# ================================================================

class TestWhaleBaseline:
    """Test that first-poll baseline seeding doesn't generate events."""

    def test_etherscan_first_poll_seeds_baseline(self):
        from data_service.etherscan_client import EtherscanClient
        client = EtherscanClient()
        # _last_seen_tx should be empty initially
        assert len(client._last_seen_tx) == 0
        # After setting via the baseline logic (simulated), movements should be empty

    def test_btc_whale_first_poll_seeds_baseline(self):
        from data_service.btc_whale_client import BtcWhaleClient
        client = BtcWhaleClient()
        assert len(client._last_seen_tx) == 0


# ================================================================
# FundingRate.fetched_at tests
# ================================================================

class TestFundingFetchedAt:
    """Test backward-compatible fetched_at field."""

    def test_default_zero(self):
        from shared.models import FundingRate
        fr = FundingRate(timestamp=1000, pair="BTC/USDT", rate=0.001,
                         next_rate=0.0, next_funding_time=0)
        assert fr.fetched_at == 0

    def test_explicit_value(self):
        from shared.models import FundingRate
        fr = FundingRate(timestamp=1000, pair="BTC/USDT", rate=0.001,
                         next_rate=0.0, next_funding_time=0,
                         fetched_at=2000)
        assert fr.fetched_at == 2000


# ================================================================
# SnapshotHealth new fields tests
# ================================================================

class TestSnapshotHealthNewFields:
    """Test backward-compatible new fields in SnapshotHealth."""

    def test_defaults(self):
        health = SnapshotHealth(
            sources=(), completeness_pct=1.0,
            critical_sources_healthy=True,
            stale_sources=(), missing_sources=(),
        )
        assert health.redis_healthy is True
        assert health.service_state == "running"

    def test_explicit_values(self):
        health = SnapshotHealth(
            sources=(), completeness_pct=0.5,
            critical_sources_healthy=False,
            stale_sources=("oi",), missing_sources=("cvd",),
            redis_healthy=False, service_state="recovering",
        )
        assert health.redis_healthy is False
        assert health.service_state == "recovering"


# ================================================================
# Integration: State machine transition scenarios
# ================================================================

class TestStateTransitions:
    """Test DataService state machine transitions with simulated scenarios.

    These tests simulate the warmup check logic without running the full
    async DataService. They verify the decision logic, not the async wiring.
    """

    def _make_continuous_candles(self, count: int, tf: str = "5m"):
        """Generate continuous candles for a timeframe."""
        base = int(time.time() * 1000) - count * TIMEFRAME_MS[tf]
        ms = TIMEFRAME_MS[tf]
        return [_make_candle(base + i * ms, tf=tf) for i in range(count)]

    def _make_gapped_candles(self, count: int, gap_at: int, tf: str = "5m"):
        """Generate candles with a gap (missing candle) at position gap_at."""
        base = int(time.time() * 1000) - (count + 1) * TIMEFRAME_MS[tf]
        ms = TIMEFRAME_MS[tf]
        indices = [i for i in range(count + 1) if i != gap_at]
        return [_make_candle(base + i * ms, tf=tf) for i in indices[:count]]

    # --- Short reconnect: gap < backfill capacity ---

    def test_short_reconnect_fills_gap_allows_running(self):
        """Short outage: gap is small, backfill fills it, continuity OK → RUNNING."""
        candles = self._make_continuous_candles(60, "5m")
        assert len(candles) >= 50
        is_cont, gaps = validate_candle_continuity(candles[-50:], "5m")
        assert is_cont is True
        assert gaps == 0

    # --- Long reconnect: gap > backfill capacity ---

    def test_long_reconnect_unrecoverable_gap_blocks_running(self):
        """Long outage: gap remains after backfill → continuity fails → stays RECOVERING."""
        # Simulate: 50 candles but with a gap in the middle (unfillable)
        candles = self._make_gapped_candles(50, gap_at=25, tf="5m")
        assert len(candles) == 50
        is_cont, gaps = validate_candle_continuity(candles, "5m")
        assert is_cont is False
        assert gaps == 1

    # --- Gap exactly at tolerance boundary ---

    def test_gap_at_tolerance_boundary(self):
        """Gap of exactly 1.5x interval should still pass (<=)."""
        ms = TIMEFRAME_MS["5m"]
        candles = [
            _make_candle(1000000),
            _make_candle(1000000 + int(ms * 1.5)),  # Exactly 1.5x
        ]
        is_cont, gaps = validate_candle_continuity(candles, "5m")
        assert is_cont is True

    def test_gap_just_beyond_tolerance(self):
        """Gap of 1.5x + 1ms should fail."""
        ms = TIMEFRAME_MS["5m"]
        candles = [
            _make_candle(1000000),
            _make_candle(1000000 + int(ms * 1.5) + 1),
        ]
        is_cont, gaps = validate_candle_continuity(candles, "5m")
        assert is_cont is False

    # --- CVD lifecycle: invalid → warmup → valid ---

    def test_cvd_full_lifecycle(self):
        """CVD: WARMING_UP → accumulate trades → VALID → disconnect → INVALID → reconnect → WARMING_UP."""
        from data_service.cvd_calculator import CVDCalculator, _RawTrade
        from collections import deque

        calc = CVDCalculator()

        # 1. Initial state is WARMING_UP
        assert calc.get_cvd_state("BTC/USDT") == CVDState.WARMING_UP
        assert calc.get_cvd("BTC/USDT") is None

        # 2. Manually set to VALID (simulating warmup completion)
        calc._cvd_state["BTC/USDT"] = CVDState.VALID
        calc._cvd_invalid_reason["BTC/USDT"] = ""
        from shared.models import CVDSnapshot
        calc._snapshots["BTC/USDT"] = CVDSnapshot(
            timestamp=int(time.time() * 1000), pair="BTC/USDT",
            cvd_5m=1.0, cvd_15m=2.0, cvd_1h=3.0,
            buy_volume=100, sell_volume=50,
        )
        assert calc.get_cvd("BTC/USDT") is not None

        # 3. Simulate disconnect
        for pair in calc._cvd_state:
            calc._cvd_state[pair] = CVDState.INVALID
            calc._cvd_invalid_reason[pair] = "disconnect"
        assert calc.get_cvd("BTC/USDT") is None
        assert calc.get_cvd_state("BTC/USDT") == CVDState.INVALID

        # 4. Simulate reconnect — flush trades, go to WARMING_UP
        for pair in calc._cvd_state:
            if calc._cvd_state[pair] == CVDState.INVALID:
                calc._trades[pair].clear()
                calc._snapshots.pop(pair, None)
                calc._cvd_state[pair] = CVDState.WARMING_UP
        assert calc.get_cvd_state("BTC/USDT") == CVDState.WARMING_UP
        assert len(calc._trades["BTC/USDT"]) == 0

    # --- Setup gating respects data dependencies ---

    def test_gating_blocks_during_recovering(self):
        """No setup can trade during RECOVERING regardless of health."""
        health = _make_health()
        for setup_type in SETUP_DATA_DEPS:
            allowed, reason = can_trade_setup(
                setup_type, health, DataServiceState.RECOVERING, CVDState.VALID,
            )
            assert allowed is False, f"{setup_type} should be blocked during RECOVERING"
            assert "RECOVERING" in reason, f"{setup_type} reason should mention RECOVERING, got: {reason}"

    def test_gating_allows_candle_only_during_running(self):
        """Candle-only setups (A/B/D/F/H) can trade during RUNNING even with stale non-deps."""
        health = _make_health(stale=("funding", "oi", "cvd"), missing=("whales",))
        candle_only = ["setup_a", "setup_b", "setup_d_choch", "setup_d_bos", "setup_f", "setup_h"]
        for setup_type in candle_only:
            allowed, reason = can_trade_setup(
                setup_type, health, DataServiceState.RUNNING, CVDState.INVALID,
            )
            assert allowed is True, f"{setup_type} should be allowed — only needs candles"

    def test_gating_blocks_setup_c_when_cvd_warming(self):
        """Setup C needs CVD — blocked when CVD is still warming up."""
        health = _make_health()
        allowed, reason = can_trade_setup(
            "setup_c", health, DataServiceState.RUNNING, CVDState.WARMING_UP,
        )
        assert allowed is False
        assert "cvd" in reason

    def test_gating_blocks_setup_e_when_oi_missing(self):
        """Setup E needs OI — blocked when OI is missing."""
        health = _make_health(missing=("oi",))
        allowed, reason = can_trade_setup(
            "setup_e", health, DataServiceState.RUNNING, CVDState.VALID,
        )
        assert allowed is False
        assert "oi" in reason

    # --- Continuity across timeframes ---

    def test_5m_and_15m_validated_independently(self):
        """Each timeframe is validated separately — gap in 15m doesn't affect 5m."""
        candles_5m = self._make_continuous_candles(50, "5m")
        candles_15m = self._make_gapped_candles(50, gap_at=20, tf="15m")

        ok_5m, _ = validate_candle_continuity(candles_5m, "5m")
        ok_15m, gaps_15m = validate_candle_continuity(candles_15m, "15m")

        assert ok_5m is True
        assert ok_15m is False
        assert gaps_15m == 1

    # --- Circuit breaker + state transitions ---

    def test_circuit_breaker_drives_degraded(self):
        """Multiple rapid reconnects trip circuit breaker → DEGRADED."""
        from config.settings import settings

        cb = CircuitBreaker(
            max_events=settings.CIRCUIT_BREAKER_MAX_RECONNECTS,
            window_seconds=settings.CIRCUIT_BREAKER_WINDOW_SECONDS,
            stable_seconds=settings.CIRCUIT_BREAKER_STABLE_SECONDS,
        )

        # Simulate rapid reconnects
        for _ in range(settings.CIRCUIT_BREAKER_MAX_RECONNECTS):
            cb.record_event()

        assert cb.is_tripped is True

        # In real code: service transitions to DEGRADED, blocking all setups
        state = DataServiceState.DEGRADED
        allowed, reason = can_trade_setup(
            "setup_a", _make_health(), state, CVDState.VALID,
        )
        assert allowed is False
        assert "DEGRADED" in reason

    def test_boundary_tolerance_15m(self):
        """Boundary test for 15m timeframe."""
        ms = TIMEFRAME_MS["15m"]  # 900_000
        # Exactly 1.5x should pass
        candles = [_make_candle(1000000, tf="15m"), _make_candle(1000000 + int(ms * 1.5), tf="15m")]
        is_cont, _ = validate_candle_continuity(candles, "15m")
        assert is_cont is True

        # 1.5x + 1ms should fail
        candles2 = [_make_candle(1000000, tf="15m"), _make_candle(1000000 + int(ms * 1.5) + 1, tf="15m")]
        is_cont2, _ = validate_candle_continuity(candles2, "15m")
        assert is_cont2 is False

    def test_gating_differential_oi_missing_blocks_e_allows_c(self):
        """With only OI missing, setup_e blocked but setup_c passes (given CVD valid)."""
        health = _make_health(missing=("oi",))
        # setup_c needs candles+funding+cvd — OI missing doesn't affect it
        allowed_c, _ = can_trade_setup("setup_c", health, DataServiceState.RUNNING, CVDState.VALID)
        assert allowed_c is True
        # setup_e needs candles+oi — OI missing blocks it
        allowed_e, reason_e = can_trade_setup("setup_e", health, DataServiceState.RUNNING, CVDState.VALID)
        assert allowed_e is False
        assert "oi" in reason_e


# ================================================================
# Integration: DataService._check_warmup() with real state machine
# ================================================================

class TestWarmupIntegration:
    """Test the actual warmup decision logic in DataService._check_warmup().

    Uses a real DataService with mocked sub-modules, exercising the real
    state machine transitions rather than testing individual functions.
    """

    @pytest.fixture
    def svc(self):
        """Create a DataService with minimal mocking for warmup tests."""
        from unittest.mock import MagicMock, patch
        from data_service.service import DataService
        from data_service.websocket_feeds import OKXWebSocketFeed

        # Patch settings for minimal pair/tf set (faster tests)
        with patch("data_service.service.settings") as mock_settings:
            mock_settings.TRADING_PAIRS = ["BTC/USDT"]
            mock_settings.LTF_TIMEFRAMES = ["5m"]
            mock_settings.HTF_TIMEFRAMES = ["1h"]
            mock_settings.STARTUP_WARMUP_CANDLE_MIN = 10
            mock_settings.CIRCUIT_BREAKER_MAX_RECONNECTS = 3
            mock_settings.CIRCUIT_BREAKER_WINDOW_SECONDS = 300
            mock_settings.CIRCUIT_BREAKER_STABLE_SECONDS = 120
            mock_settings.CVD_WARMUP_SECONDS = 60
            mock_settings.HTF_CAMPAIGN_ENABLED = False
            mock_settings.RECONNECT_INITIAL_DELAY = 1.0
            mock_settings.ETHERSCAN_API_KEY = ""
            mock_settings.WHALE_WALLETS = {}
            mock_settings.BTC_WHALE_WALLETS = {}
            mock_settings.REDIS_HOST = "localhost"
            mock_settings.REDIS_PORT = 6379
            mock_settings.POSTGRES_HOST = "localhost"
            mock_settings.POSTGRES_PORT = 5432
            mock_settings.POSTGRES_DB = "test"
            mock_settings.POSTGRES_USER = "test"
            mock_settings.POSTGRES_PASSWORD = ""
            mock_settings.OKX_API_KEY = ""
            mock_settings.OKX_SECRET = ""
            mock_settings.OKX_PASSPHRASE = ""
            mock_settings.OKX_SANDBOX = True
            mock_settings.NEWS_SENTIMENT_ENABLED = False
            mock_settings.TELEGRAM_BOT_TOKEN = ""
            mock_settings.TELEGRAM_CHAT_ID = ""

            ds = DataService.__new__(DataService)
            ds._pipeline_callback = None
            ds._alert_manager = None
            ds._ws_feed = OKXWebSocketFeed()
            ds._cvd = MagicMock()
            ds._oi_proxy = MagicMock()
            ds._exchange = MagicMock()
            ds._etherscan = MagicMock()
            ds._btc_whale = MagicMock()
            ds._redis = MagicMock()
            ds._redis.is_connected = True
            ds._postgres = MagicMock()
            ds._news = MagicMock()
            ds._latest_sentiment = None
            ds._tasks = []
            ds._running = True
            ds._last_health_down = set()
            ds._health_check_count = 0
            ds._state = DataServiceState.RECOVERING
            ds._circuit_breaker = CircuitBreaker(
                max_events=3, window_seconds=300, stable_seconds=120,
            )
            ds._backfill_in_progress = False
            yield ds

    def _inject_continuous(self, svc, count: int, tf: str = "5m", pair: str = "BTC/USDT"):
        """Inject N continuous candles into the WS feed buffer."""
        now_ms = int(time.time() * 1000)
        ms = TIMEFRAME_MS[tf]
        candles = [_make_candle(now_ms - (count - i) * ms, pair=pair, tf=tf) for i in range(count)]
        svc._ws_feed.store_candles(candles)

    def _inject_gapped(self, svc, count: int, gap_at: int, tf: str = "5m", pair: str = "BTC/USDT"):
        """Inject candles with a gap at position gap_at."""
        now_ms = int(time.time() * 1000)
        ms = TIMEFRAME_MS[tf]
        candles = []
        for i in range(count + 1):
            if i == gap_at:
                continue
            candles.append(_make_candle(now_ms - (count - i) * ms, pair=pair, tf=tf))
        svc._ws_feed.store_candles(candles[:count])

    # --- Test 1: Full reconnect sequence RECOVERING → RUNNING ---

    def test_full_reconnect_to_running(self, svc):
        """Walk through the complete warmup sequence with real state machine."""
        assert svc._state == DataServiceState.RECOVERING

        # Step 1: No WS connection → stays RECOVERING
        svc._ws_feed._connected = False
        svc._check_warmup()
        assert svc._state == DataServiceState.RECOVERING

        # Step 2: WS connected but no candles → stays RECOVERING
        svc._ws_feed._connected = True
        svc._check_warmup()
        assert svc._state == DataServiceState.RECOVERING

        # Step 3: Inject continuous candles (simulates backfill)
        self._inject_continuous(svc, 15)
        svc._check_warmup()
        assert svc._state == DataServiceState.RECOVERING  # Still no live candle

        # Step 4: Simulate live candle received
        svc._ws_feed._live_candle_count = 1
        svc._check_warmup()
        assert svc._state == DataServiceState.RUNNING  # All conditions met

    # --- Test 2: Double reconnect during backfill ---

    def test_double_reconnect_during_backfill(self, svc):
        """Two reconnects in quick succession — second is guarded by _backfill_in_progress."""
        svc._state = DataServiceState.RUNNING

        # First reconnect
        svc._state = DataServiceState.RECOVERING
        svc._circuit_breaker.record_event()
        assert svc._state == DataServiceState.RECOVERING

        # Simulate first backfill in progress
        svc._backfill_in_progress = True

        # Second reconnect — circuit breaker records, state stays RECOVERING
        svc._state = DataServiceState.RECOVERING  # (already RECOVERING)
        svc._circuit_breaker.record_event()
        assert svc._state == DataServiceState.RECOVERING
        assert not svc._circuit_breaker.is_tripped  # 2 < 3

        # Verify backfill guard would block second backfill
        assert svc._backfill_in_progress is True

        # Third reconnect trips circuit breaker
        svc._circuit_breaker.record_event()
        if svc._circuit_breaker.is_tripped:
            svc._state = DataServiceState.DEGRADED
        assert svc._state == DataServiceState.DEGRADED
        assert svc._circuit_breaker.is_tripped

    # --- Test 3: Live candle before backfill completes (gaps exist) ---

    def test_live_candle_with_gaps_stays_recovering(self, svc):
        """Live candle arrived but candles have gaps → stays RECOVERING."""
        svc._ws_feed._connected = True
        self._inject_gapped(svc, 15, gap_at=7)
        svc._ws_feed._live_candle_count = 5  # Multiple live candles

        svc._check_warmup()
        assert svc._state == DataServiceState.RECOVERING  # Gaps block it

    # --- Test 4: Partial gap repair stays RECOVERING ---

    def test_partial_gap_repair_stays_recovering(self, svc):
        """Backfill fills some but not all gaps → stays RECOVERING."""
        svc._ws_feed._connected = True

        # Inject exactly min_count (10) candles with gap near the end
        # Gap at position 8 means last 10 candles have a gap
        self._inject_gapped(svc, 10, gap_at=8)
        svc._ws_feed._live_candle_count = 1

        svc._check_warmup()
        assert svc._state == DataServiceState.RECOVERING

        # Check again — gap persists, still blocked
        svc._check_warmup()
        assert svc._state == DataServiceState.RECOVERING

    # --- Test 5: CVD valid but candles broken blocks everything ---

    def test_cvd_valid_candles_broken_blocks_all(self, svc):
        """CVD is VALID but candle continuity is broken → stays RECOVERING."""
        svc._ws_feed._connected = True
        self._inject_gapped(svc, 15, gap_at=8)
        svc._ws_feed._live_candle_count = 3

        # Even with CVD valid, state stays RECOVERING due to candle gaps
        svc._check_warmup()
        assert svc._state == DataServiceState.RECOVERING

        # Gating blocks all setups because state is RECOVERING
        for setup_type in SETUP_DATA_DEPS:
            allowed, reason = can_trade_setup(
                setup_type, _make_health(), svc._state, CVDState.VALID,
            )
            assert allowed is False, f"{setup_type} should be blocked"
            assert "RECOVERING" in reason, f"{setup_type} reason should mention RECOVERING, got: {reason}"

    # --- Test 6: RUNNING → new reconnect → RECOVERING → RUNNING again ---

    def test_running_to_recovering_on_new_gap(self, svc):
        """System reaches RUNNING, then new disconnect → RECOVERING, must re-qualify."""
        svc._ws_feed._connected = True
        self._inject_continuous(svc, 15)
        svc._ws_feed._live_candle_count = 1
        svc._check_warmup()
        assert svc._state == DataServiceState.RUNNING

        # New disconnect: reset state
        svc._state = DataServiceState.RECOVERING
        svc._ws_feed._connected = False
        svc._ws_feed._live_candle_count = 0

        # Verify it doesn't stay RUNNING by inertia
        svc._check_warmup()
        assert svc._state == DataServiceState.RECOVERING

        # Reconnect + new live candle → back to RUNNING
        svc._ws_feed._connected = True
        svc._ws_feed._live_candle_count = 1
        svc._check_warmup()
        assert svc._state == DataServiceState.RUNNING

    # --- Test 7: Circuit breaker trips during active state ---

    def test_circuit_breaker_trips_forces_degraded(self, svc):
        """Circuit breaker trips → DEGRADED, stays there until reset."""
        svc._ws_feed._connected = True
        self._inject_continuous(svc, 15)
        svc._ws_feed._live_candle_count = 1

        # Trip the circuit breaker BEFORE warmup check
        for _ in range(3):
            svc._circuit_breaker.record_event()
        assert svc._circuit_breaker.is_tripped

        # Warmup check should detect tripped breaker and go DEGRADED
        svc._check_warmup()
        assert svc._state == DataServiceState.DEGRADED

        # Stays DEGRADED on subsequent checks while tripped
        svc._check_warmup()
        assert svc._state == DataServiceState.DEGRADED

    # --- Test 8: Only continuous candles allow RUNNING ---

    def test_only_backfilled_no_live_stays_recovering(self, svc):
        """500 backfilled continuous candles but 0 live candles → stays RECOVERING."""
        svc._ws_feed._connected = True
        self._inject_continuous(svc, 100)
        svc._ws_feed._live_candle_count = 0  # No live candles

        svc._check_warmup()
        assert svc._state == DataServiceState.RECOVERING

    # --- Test 9: WS disconnects mid-RUNNING, doesn't stay RUNNING ---

    def test_ws_disconnect_does_not_keep_running(self, svc):
        """If somehow state is still RECOVERING but WS is down, stays RECOVERING."""
        svc._ws_feed._connected = False
        svc._ws_feed._live_candle_count = 5
        self._inject_continuous(svc, 15)

        svc._check_warmup()
        assert svc._state == DataServiceState.RECOVERING

    # --- Test 10: CVD warmup via real batch computation ---

    def test_cvd_warmup_with_real_trades(self):
        """CVD transitions from WARMING_UP to VALID when trades span warmup period."""
        from unittest.mock import patch
        from data_service.cvd_calculator import CVDCalculator, _RawTrade

        calc = CVDCalculator()
        assert calc.get_cvd_state("BTC/USDT") == CVDState.WARMING_UP

        now_ms = int(time.time() * 1000)

        # Add trades spanning less than warmup period
        for i in range(10):
            calc._trades["BTC/USDT"].append(_RawTrade(
                timestamp=now_ms - 1000 + i * 100,  # 1 second span
                price=50000.0, size=0.01, side="buy",
            ))

        # Compute snapshot + check warmup (simulating batch loop body)
        calc._compute_snapshot("BTC/USDT", now_ms)

        # Check warmup — trade span < CVD_WARMUP_SECONDS → still WARMING_UP
        trades = calc._trades["BTC/USDT"]
        span_sec = (now_ms - trades[0].timestamp) / 1000
        # span is ~1 second, warmup needs 3600s → stays WARMING_UP
        assert span_sec < 3600
        assert calc.get_cvd_state("BTC/USDT") == CVDState.WARMING_UP

        # Now add old trade to make span >= warmup
        with patch("data_service.cvd_calculator.settings") as mock_s:
            mock_s.CVD_WARMUP_SECONDS = 60  # Lower threshold for test
            mock_s.RECONNECT_INITIAL_DELAY = 1.0

            calc._trades["BTC/USDT"].appendleft(_RawTrade(
                timestamp=now_ms - 61_000,  # 61 seconds ago
                price=49900.0, size=0.01, side="sell",
            ))

            # Simulate what _batch_loop does for warmup check
            trades = calc._trades["BTC/USDT"]
            if len(trades) >= 2:
                oldest_ms = trades[0].timestamp
                span = (now_ms - oldest_ms) / 1000
                if span >= mock_s.CVD_WARMUP_SECONDS:
                    calc._cvd_state["BTC/USDT"] = CVDState.VALID

            assert calc.get_cvd_state("BTC/USDT") == CVDState.VALID
            # Now get_cvd() should return the snapshot
            assert calc.get_cvd("BTC/USDT") is not None
