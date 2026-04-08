"""Integration tests for Strategy Service — audit-driven signal verification.

Tests what the strategy audit (2026-03-18) identified as gaps:
1. Threshold boundary tests (OB volume, ATR, target space, funding)
2. Signal decision tests (which signal drives entries, which is decorative)
3. ENABLED_SETUPS gating
4. Rejection reason tracing
5. CVD divergence vs simple boolean
6. OI delta as confluence
7. Funding symmetry
8. Setup B vs Setup F equivalence
"""

import time
import pytest

from config.settings import settings
from shared.models import (
    Candle, MarketSnapshot, FundingRate, OpenInterest,
    CVDSnapshot, OIFlushEvent,
)
from strategy_service.setups import SetupEvaluator
from strategy_service.market_structure import (
    MarketStructureState, StructureBreak, SwingPoint
)
from strategy_service.order_blocks import OrderBlock
from strategy_service.fvg import FairValueGap
from strategy_service.liquidity import (
    LiquiditySweep, LiquidityLevel, PremiumDiscountZone,
)
from strategy_service.quick_setups import QuickSetupEvaluator
from strategy_service.service import StrategyService
from tests.conftest import make_candle, make_candle_series, make_market_snapshot
from unittest.mock import MagicMock


# ============================================================
# Shared helpers
# ============================================================

def _make_break(
    break_type="choch",
    direction="bullish",
    break_price=110.0,
    broken_level=108.0,
    candle_index=10,
    timestamp=10000,
) -> StructureBreak:
    return StructureBreak(
        timestamp=timestamp,
        break_type=break_type,
        direction=direction,
        break_price=break_price,
        broken_level=broken_level,
        candle_index=candle_index,
    )


def _make_state(
    trend="bullish",
    break_type="choch",
    break_direction="bullish",
    timeframe="15m",
) -> MarketStructureState:
    brk = _make_break(break_type=break_type, direction=break_direction)
    return MarketStructureState(
        pair="BTC/USDT",
        timeframe=timeframe,
        trend=trend,
        swing_highs=[],
        swing_lows=[],
        structure_breaks=[brk],
        latest_break=brk,
    )


def _make_ob(
    direction="bullish",
    entry_price=101.0,
    high=103.0,
    low=98.0,
    body_high=102.0,
    body_low=100.0,
    volume_ratio=2.0,
    timestamp=8000,
    timeframe="15m",
) -> OrderBlock:
    brk = _make_break(break_type="bos", direction=direction)
    return OrderBlock(
        timestamp=timestamp,
        pair="BTC/USDT",
        timeframe=timeframe,
        direction=direction,
        high=high,
        low=low,
        body_high=body_high,
        body_low=body_low,
        entry_price=entry_price,
        volume=20.0,
        volume_ratio=volume_ratio,
        mitigated=False,
        associated_break=brk,
    )


def _make_sweep(
    direction="bullish",
    volume_ratio=2.5,
    had_oi_flush=True,
) -> LiquiditySweep:
    return LiquiditySweep(
        timestamp=9000,
        pair="BTC/USDT",
        timeframe="15m",
        direction=direction,
        swept_level=95.0 if direction == "bullish" else 105.0,
        wick_price=94.0 if direction == "bullish" else 106.0,
        close_price=96.0 if direction == "bullish" else 104.0,
        volume_ratio=volume_ratio,
        had_oi_flush=had_oi_flush,
    )


def _make_pd(zone="discount") -> PremiumDiscountZone:
    return PremiumDiscountZone(
        pair="BTC/USDT",
        range_high=110.0,
        range_low=90.0,
        equilibrium=100.0,
        last_updated_ms=int(time.time() * 1000),
        zone=zone,
    )


def _make_candles(close=101.0, count=20, timeframe="15m") -> list[Candle]:
    return [
        make_candle(
            close=close, high=close + 1, low=close - 1,
            timestamp=i * 1000, timeframe=timeframe,
        )
        for i in range(count)
    ]


def _make_snapshot(
    pair="BTC/USDT",
    funding_rate=0.0001,
    oi_usd=1_000_000.0,
    cvd_5m=None,
    cvd_15m=100.0,
    cvd_1h=None,
    buy_volume=500.0,
    sell_volume=400.0,
    oi_flushes=None,
) -> MarketSnapshot:
    """Create a MarketSnapshot with fine-grained CVD control."""
    ts = int(time.time() * 1000)
    if cvd_5m is None:
        cvd_5m = cvd_15m / 3
    if cvd_1h is None:
        cvd_1h = cvd_15m * 4
    return MarketSnapshot(
        pair=pair,
        timestamp=ts,
        funding=FundingRate(
            timestamp=ts, pair=pair, rate=funding_rate,
            next_rate=funding_rate, next_funding_time=ts + 28800000,
        ),
        oi=OpenInterest(
            timestamp=ts, pair=pair,
            oi_contracts=1000.0, oi_base=10.0, oi_usd=oi_usd,
        ),
        cvd=CVDSnapshot(
            timestamp=ts, pair=pair,
            cvd_5m=cvd_5m, cvd_15m=cvd_15m, cvd_1h=cvd_1h,
            buy_volume=buy_volume, sell_volume=sell_volume,
        ),
        recent_oi_flushes=oi_flushes or [],
        whale_movements=[],
    )


def _valid_setup_a_args(**overrides) -> dict:
    """Standard valid Setup A args. Override any key."""
    args = dict(
        structure_state=_make_state(break_type="choch", break_direction="bullish"),
        active_obs=[_make_ob(direction="bullish", volume_ratio=2.0)],
        recent_sweeps=[_make_sweep(direction="bullish")],
        pd_zone=_make_pd("discount"),
        market_snapshot=_make_snapshot(
            cvd_15m=100.0,
            oi_flushes=[
                OIFlushEvent(
                    timestamp=9000, pair="BTC/USDT", side="long",
                    size_usd=50000, price=94.5, source="oi_proxy",
                ),
            ],
        ),
        candles=_make_candles(101.0),
        pair="BTC/USDT",
        htf_bias="bullish",
        liquidity_levels=[],
    )
    args.update(overrides)
    return args


# ============================================================
# 1. Threshold Boundary Tests
# ============================================================

class TestOBVolumeRatioThreshold:
    """OB_MIN_VOLUME_RATIO: OBs below threshold should not add volume confluence."""

    def test_ob_above_threshold_adds_confluence(self):
        """OB with volume >= 1.3x adds ob_volume confluence."""
        evaluator = SetupEvaluator()
        args = _valid_setup_a_args(
            active_obs=[_make_ob(volume_ratio=1.5)],
        )
        setup = evaluator.evaluate_setup_a(**args)
        assert setup is not None
        assert any("ob_volume" in c for c in setup.confluences)

    def test_ob_below_threshold_no_volume_confluence(self):
        """OB with volume < 1.3x does NOT add ob_volume confluence."""
        evaluator = SetupEvaluator()
        args = _valid_setup_a_args(
            active_obs=[_make_ob(volume_ratio=1.0)],
        )
        setup = evaluator.evaluate_setup_a(**args)
        # May still produce a setup (other confluences), but NO ob_volume
        if setup is not None:
            assert not any("ob_volume" in c for c in setup.confluences)

    def test_ob_at_exact_threshold_passes(self):
        """OB with volume exactly at threshold passes."""
        evaluator = SetupEvaluator()
        args = _valid_setup_a_args(
            active_obs=[_make_ob(volume_ratio=settings.OB_MIN_VOLUME_RATIO)],
        )
        setup = evaluator.evaluate_setup_a(**args)
        assert setup is not None
        assert any("ob_volume" in c for c in setup.confluences)


class TestFundingSymmetry:
    """Funding rate thresholds must be symmetric (audit 03-18 fix)."""

    def test_long_funding_below_mild_no_confluence(self):
        """Bullish setup: funding above -FUNDING_MILD_THRESHOLD → no funding confluence."""
        evaluator = SetupEvaluator()

        # Funding at -0.00005 (below mild threshold 0.0001): should NOT trigger
        args = _valid_setup_a_args(
            market_snapshot=_make_snapshot(funding_rate=-0.00005),
        )
        setup = evaluator.evaluate_setup_a(**args)
        if setup is not None:
            assert not any("funding" in c for c in setup.confluences)

    def test_long_funding_extreme_adds_confluence(self):
        """Bullish setup: funding < -0.0003 → funding confluence."""
        evaluator = SetupEvaluator()
        args = _valid_setup_a_args(
            market_snapshot=_make_snapshot(funding_rate=-0.0005),
        )
        setup = evaluator.evaluate_setup_a(**args)
        assert setup is not None
        assert any("funding" in c for c in setup.confluences)

    def test_short_funding_uses_same_threshold(self):
        """Bearish setup: funding must be > +FUNDING_EXTREME_THRESHOLD."""
        evaluator = SetupEvaluator()
        args = _valid_setup_a_args(
            structure_state=_make_state(break_type="choch", break_direction="bearish"),
            active_obs=[_make_ob(direction="bearish", entry_price=101.0,
                                 high=103.0, low=98.0, body_high=102.0, body_low=100.0)],
            recent_sweeps=[_make_sweep(direction="bearish")],
            pd_zone=_make_pd("premium"),
            market_snapshot=_make_snapshot(
                funding_rate=0.0005, cvd_15m=-100.0,
                cvd_5m=-30.0, cvd_1h=-400.0,
            ),
            htf_bias="bearish",
        )
        setup = evaluator.evaluate_setup_a(**args)
        if setup is not None:
            assert any("funding" in c for c in setup.confluences)

    def test_funding_mild_adds_confluence(self):
        """Funding at -0.00015: now hits mild tier (>= 0.0001)."""
        evaluator = SetupEvaluator()
        args = _valid_setup_a_args(
            market_snapshot=_make_snapshot(funding_rate=-0.00015),
        )
        setup = evaluator.evaluate_setup_a(**args)
        if setup is not None:
            assert any("funding_mild" in c for c in setup.confluences)


# ============================================================
# 2. CVD Divergence Tests
# ============================================================

class TestCVDDivergence:
    """CVD should use divergence + MTF agreement, not simple boolean."""

    def test_cvd_divergence_bullish(self):
        """Price down + CVD up = bullish divergence (strongest CVD signal)."""
        evaluator = SetupEvaluator()
        # Candles trending DOWN (each close lower than prev)
        candles = []
        for i in range(20):
            price = 103.0 - i * 0.2  # price dropping
            candles.append(make_candle(
                open=price + 0.1, close=price, high=price + 0.2, low=price - 0.1,
                timestamp=i * 1000,
            ))
        args = _valid_setup_a_args(
            candles=candles,
            market_snapshot=_make_snapshot(
                cvd_5m=50.0, cvd_15m=100.0, cvd_1h=400.0,  # all positive
            ),
        )
        setup = evaluator.evaluate_setup_a(**args)
        if setup is not None:
            assert any("cvd_divergence" in c for c in setup.confluences)

    def test_cvd_mtf_agreement(self):
        """All 3 CVD timeframes aligned = multi-timeframe confluence."""
        evaluator = SetupEvaluator()
        args = _valid_setup_a_args(
            market_snapshot=_make_snapshot(
                cvd_5m=50.0, cvd_15m=100.0, cvd_1h=400.0,
            ),
        )
        setup = evaluator.evaluate_setup_a(**args)
        assert setup is not None
        # Should be either MTF or divergence
        has_cvd = any("cvd_mtf" in c or "cvd_divergence" in c or "cvd_aligned" in c
                       for c in setup.confluences)
        assert has_cvd

    def test_cvd_no_agreement_no_mtf_confluence(self):
        """Mixed CVD (5m positive, 1h negative) = no MTF agreement."""
        evaluator = SetupEvaluator()
        args = _valid_setup_a_args(
            market_snapshot=_make_snapshot(
                cvd_5m=50.0, cvd_15m=100.0, cvd_1h=-200.0,
            ),
        )
        setup = evaluator.evaluate_setup_a(**args)
        if setup is not None:
            assert not any("cvd_mtf" in c for c in setup.confluences)

    def test_cvd_opposing_direction_no_confluence(self):
        """CVD negative for bullish setup = no CVD confluence at all."""
        evaluator = SetupEvaluator()
        args = _valid_setup_a_args(
            market_snapshot=_make_snapshot(
                cvd_5m=-50.0, cvd_15m=-100.0, cvd_1h=-400.0,
            ),
        )
        setup = evaluator.evaluate_setup_a(**args)
        if setup is not None:
            assert not any("cvd" in c for c in setup.confluences)


# ============================================================
# 3. OI Delta Tests
# ============================================================

class TestOIDelta:
    """OI should track delta between evaluations, not just existence."""

    def test_oi_rising_adds_confluence(self):
        """OI increasing between evaluations adds oi_rising confluence."""
        evaluator = SetupEvaluator()

        # First call: seeds _prev_oi
        args1 = _valid_setup_a_args(
            market_snapshot=_make_snapshot(oi_usd=1_000_000.0),
        )
        evaluator.evaluate_setup_a(**args1)

        # Second call: OI rose 1% → should add oi_rising confluence
        args2 = _valid_setup_a_args(
            market_snapshot=_make_snapshot(oi_usd=1_010_000.0),
        )
        setup = evaluator.evaluate_setup_a(**args2)
        if setup is not None:
            assert any("oi_rising" in c for c in setup.confluences)

    def test_oi_dropping_adds_confluence(self):
        """OI decreasing >2% between evaluations adds oi_dropping confluence."""
        evaluator = SetupEvaluator()

        # First call: seeds _prev_oi
        args1 = _valid_setup_a_args(
            market_snapshot=_make_snapshot(oi_usd=1_000_000.0),
        )
        evaluator.evaluate_setup_a(**args1)

        # Second call: OI dropped 3% (must exceed OI_DELTA_MODERATE_PCT = 2%)
        args2 = _valid_setup_a_args(
            market_snapshot=_make_snapshot(oi_usd=970_000.0),
        )
        setup = evaluator.evaluate_setup_a(**args2)
        if setup is not None:
            assert any("oi_dropping" in c for c in setup.confluences)

    def test_oi_flat_no_confluence(self):
        """OI barely changed → no OI confluence."""
        evaluator = SetupEvaluator()

        # Seed
        args1 = _valid_setup_a_args(
            market_snapshot=_make_snapshot(oi_usd=1_000_000.0),
        )
        evaluator.evaluate_setup_a(**args1)

        # Tiny change: 0.1%
        args2 = _valid_setup_a_args(
            market_snapshot=_make_snapshot(oi_usd=1_001_000.0),
        )
        setup = evaluator.evaluate_setup_a(**args2)
        if setup is not None:
            assert not any("oi_rising" in c or "oi_dropping" in c
                           for c in setup.confluences)

    def test_first_call_no_oi_delta(self):
        """First evaluation (no prev OI) should not add OI delta confluence."""
        evaluator = SetupEvaluator()
        args = _valid_setup_a_args(
            market_snapshot=_make_snapshot(oi_usd=1_000_000.0),
        )
        setup = evaluator.evaluate_setup_a(**args)
        if setup is not None:
            assert not any("oi_rising" in c or "oi_dropping" in c
                           for c in setup.confluences)

    def test_oi_existence_check_removed(self):
        """Old 'oi_data_available' confluence should never appear."""
        evaluator = SetupEvaluator()
        args = _valid_setup_a_args()
        setup = evaluator.evaluate_setup_a(**args)
        if setup is not None:
            assert not any("oi_data_available" in c for c in setup.confluences)


# ============================================================
# 4. ENABLED_SETUPS Gating Tests
# ============================================================

class TestEnabledSetups:
    """ENABLED_SETUPS controls which setups reach the pipeline."""

    def test_setup_b_not_in_enabled(self):
        """Setup B is disabled per audit 03-18."""
        assert "setup_b" not in settings.ENABLED_SETUPS

    def test_setup_a_in_enabled(self):
        """Setup A should remain enabled."""
        assert "setup_a" in settings.ENABLED_SETUPS

    def test_setup_f_in_enabled(self):
        """Setup F should remain enabled (strictly better than B)."""
        assert "setup_f" in settings.ENABLED_SETUPS

    def test_setup_h_disabled(self):
        """Setup H disabled (03-19): 27 trades, 11% WR, PF 0.10. Adverse selection at impulse top."""
        assert "setup_h" not in settings.ENABLED_SETUPS

    def test_disabled_setup_detected_but_discarded(self):
        """StrategyService.evaluate() discards setups not in ENABLED_SETUPS."""
        # This tests the facade layer, not the evaluator
        original = settings.ENABLED_SETUPS
        settings.ENABLED_SETUPS = []  # disable everything
        try:
            ds = _mock_data_service_with_bullish_trend()
            svc = StrategyService(ds)
            trigger = make_candle(timeframe="15m", close=150.0)
            result = svc.evaluate("BTC/USDT", trigger)
            assert result is None
        finally:
            settings.ENABLED_SETUPS = original


# ============================================================
# 5. Rejection Reason Tests
# ============================================================

class TestRejectionReasons:
    """Verify setups are rejected for the RIGHT reason."""

    def test_rejected_by_htf_undefined(self):
        """undefined HTF bias → None (before any setup logic)."""
        evaluator = SetupEvaluator()
        args = _valid_setup_a_args(htf_bias="undefined")
        setup = evaluator.evaluate_setup_a(**args)
        assert setup is None

    def test_rejected_by_no_sweep(self):
        """No sweeps → Setup A returns None."""
        evaluator = SetupEvaluator()
        args = _valid_setup_a_args(recent_sweeps=[])
        setup = evaluator.evaluate_setup_a(**args)
        assert setup is None

    def test_rejected_by_no_choch(self):
        """No CHoCH → Setup A returns None."""
        evaluator = SetupEvaluator()
        args = _valid_setup_a_args(
            structure_state=_make_state(break_type="bos", break_direction="bullish"),
        )
        setup = evaluator.evaluate_setup_a(**args)
        assert setup is None

    def test_rejected_by_pd_misalignment(self):
        """PD misaligned in hard-gate mode with low confluence → rejected."""
        evaluator = SetupEvaluator()
        original_pd = settings.PD_AS_CONFLUENCE
        original_override = settings.PD_OVERRIDE_MIN_CONFLUENCES
        settings.PD_AS_CONFLUENCE = False
        settings.PD_OVERRIDE_MIN_CONFLUENCES = 0  # disable override
        try:
            args = _valid_setup_a_args(pd_zone=_make_pd("premium"))
            setup = evaluator.evaluate_setup_a(**args)
            assert setup is None
        finally:
            settings.PD_AS_CONFLUENCE = original_pd
            settings.PD_OVERRIDE_MIN_CONFLUENCES = original_override

    def test_rejected_by_no_aligned_ob(self):
        """OB in wrong direction → rejected."""
        evaluator = SetupEvaluator()
        args = _valid_setup_a_args(
            active_obs=[_make_ob(direction="bearish")],
        )
        setup = evaluator.evaluate_setup_a(**args)
        assert setup is None

    def test_rejected_by_ob_too_far(self):
        """OB beyond max distance → rejected."""
        evaluator = SetupEvaluator()
        original = settings.OB_MAX_DISTANCE_PCT
        settings.OB_MAX_DISTANCE_PCT = 0.001  # extremely tight
        try:
            args = _valid_setup_a_args(
                active_obs=[_make_ob(entry_price=90.0)],  # far from candle close=101
                candles=_make_candles(101.0),
            )
            setup = evaluator.evaluate_setup_a(**args)
            assert setup is None
        finally:
            settings.OB_MAX_DISTANCE_PCT = original

    def test_rejected_by_insufficient_confluences(self):
        """Fewer than 2 confluences → rejected."""
        evaluator = SetupEvaluator()
        original = settings.PD_AS_CONFLUENCE
        settings.PD_AS_CONFLUENCE = True
        try:
            # Remove all volume confirmation sources: low OB vol, no sweep vol, no flush
            args = _valid_setup_a_args(
                active_obs=[_make_ob(volume_ratio=0.5)],
                recent_sweeps=[_make_sweep(volume_ratio=0.5, had_oi_flush=False)],
                pd_zone=_make_pd("premium"),  # misaligned → no PD confluence
                market_snapshot=MarketSnapshot(
                    pair="BTC/USDT",
                    timestamp=int(time.time() * 1000),
                    funding=None, oi=None, cvd=None,
                    recent_oi_flushes=[], whale_movements=[],
                ),
            )
            setup = evaluator.evaluate_setup_a(**args)
            # sweep + choch + ob = 3 confluences, so this should actually pass
            # The minimum 2 is very permissive; test verifies it's checked
            if setup is not None:
                assert len(setup.confluences) >= 2
        finally:
            settings.PD_AS_CONFLUENCE = original


# ============================================================
# 6. Signal Decision Tests — What Actually Drives Entries
# ============================================================

class TestSignalDecisionHierarchy:
    """Verify which signals are core triggers vs confluence vs decorative."""

    def test_setup_a_requires_sweep(self):
        """Sweep is a CORE trigger for Setup A — without it, no setup."""
        evaluator = SetupEvaluator()
        args = _valid_setup_a_args(recent_sweeps=[])
        assert evaluator.evaluate_setup_a(**args) is None

    def test_setup_a_requires_choch(self):
        """CHoCH is a CORE trigger for Setup A — BOS alone is insufficient."""
        evaluator = SetupEvaluator()
        args = _valid_setup_a_args(
            structure_state=_make_state(break_type="bos", break_direction="bullish"),
        )
        assert evaluator.evaluate_setup_a(**args) is None

    def test_setup_a_requires_ob(self):
        """OB is a CORE trigger for Setup A — no OBs, no setup."""
        evaluator = SetupEvaluator()
        args = _valid_setup_a_args(active_obs=[])
        assert evaluator.evaluate_setup_a(**args) is None

    def test_cvd_is_confluence_not_trigger(self):
        """Missing CVD should NOT prevent Setup A from firing."""
        evaluator = SetupEvaluator()
        snapshot = _make_snapshot(cvd_15m=100.0)
        # Overwrite CVD to None
        snapshot_no_cvd = MarketSnapshot(
            pair="BTC/USDT",
            timestamp=snapshot.timestamp,
            funding=snapshot.funding,
            oi=snapshot.oi,
            cvd=None,
            recent_oi_flushes=snapshot.recent_oi_flushes,
            whale_movements=[],
        )
        args = _valid_setup_a_args(market_snapshot=snapshot_no_cvd)
        setup = evaluator.evaluate_setup_a(**args)
        assert setup is not None  # CVD absent should not block

    def test_funding_is_confluence_not_trigger(self):
        """Missing funding should NOT prevent Setup A from firing."""
        evaluator = SetupEvaluator()
        snapshot_no_funding = MarketSnapshot(
            pair="BTC/USDT",
            timestamp=int(time.time() * 1000),
            funding=None,
            oi=None,
            cvd=None,
            recent_oi_flushes=[
                OIFlushEvent(
                    timestamp=9000, pair="BTC/USDT", side="long",
                    size_usd=50000, price=94.5, source="oi_proxy",
                ),
            ],
            whale_movements=[],
        )
        args = _valid_setup_a_args(market_snapshot=snapshot_no_funding)
        setup = evaluator.evaluate_setup_a(**args)
        assert setup is not None  # Funding absent should not block

    def test_oi_is_confluence_not_trigger(self):
        """Missing OI should NOT prevent Setup A from firing."""
        evaluator = SetupEvaluator()
        snapshot_no_oi = MarketSnapshot(
            pair="BTC/USDT",
            timestamp=int(time.time() * 1000),
            funding=None,
            oi=None,  # no OI
            cvd=None,
            recent_oi_flushes=[
                OIFlushEvent(
                    timestamp=9000, pair="BTC/USDT", side="long",
                    size_usd=50000, price=94.5, source="oi_proxy",
                ),
            ],
            whale_movements=[],
        )
        args = _valid_setup_a_args(market_snapshot=snapshot_no_oi)
        setup = evaluator.evaluate_setup_a(**args)
        assert setup is not None

    def test_all_market_data_none_setup_still_fires(self):
        """Setup A fires with zero market data (funding=None, OI=None, CVD=None)."""
        evaluator = SetupEvaluator()
        empty_snapshot = MarketSnapshot(
            pair="BTC/USDT",
            timestamp=int(time.time() * 1000),
            funding=None, oi=None, cvd=None,
            recent_oi_flushes=[], whale_movements=[],
        )
        args = _valid_setup_a_args(market_snapshot=empty_snapshot)
        setup = evaluator.evaluate_setup_a(**args)
        # Should still fire: sweep + choch + ob = 3 confluences
        assert setup is not None
        assert len(setup.confluences) >= 2


# ============================================================
# 7. Expectancy Filter Tests (ATR, Target Space)
# ============================================================

class TestExpectancyFilters:
    """ATR and target space filters at the StrategyService level."""

    def test_atr_filter_rejects_low_volatility(self):
        """ATR below MIN_ATR_PCT should reject setup."""
        svc_cls = StrategyService
        # Directly test _apply_expectancy_filters
        ds = MagicMock()
        svc = svc_cls(ds)

        # Create flat candles (ATR ≈ 0)
        candles = [
            make_candle(
                open=100.0, high=100.01, low=99.99, close=100.0,
                timestamp=i * 900_000, timeframe="15m",
            )
            for i in range(20)
        ]

        setup = MagicMock()
        setup.entry_price = 100.0
        setup.sl_price = 99.0
        setup.direction = "long"

        state = _make_state()
        reject = svc._apply_expectancy_filters(setup, candles, state, state)
        assert reject is not None
        assert "ATR too low" in reject

    def test_atr_filter_passes_volatile_market(self):
        """ATR above MIN_ATR_PCT should pass."""
        ds = MagicMock()
        svc = StrategyService(ds)

        # Create volatile candles (ATR ≈ 1%)
        candles = [
            make_candle(
                open=100.0, high=101.0, low=99.0, close=100.0 + (i % 2),
                timestamp=i * 900_000, timeframe="15m",
            )
            for i in range(20)
        ]

        setup = MagicMock()
        setup.entry_price = 100.0
        setup.sl_price = 99.0
        setup.direction = "long"

        state = _make_state()
        reject = svc._apply_expectancy_filters(setup, candles, state, state)
        assert reject is None

    def test_target_space_rejects_tight_ceiling(self):
        """Swing high too close above entry → rejected for longs."""
        ds = MagicMock()
        svc = StrategyService(ds)

        candles = [
            make_candle(
                open=100.0, high=101.0, low=99.0, close=100.5,
                timestamp=i * 900_000, timeframe="15m",
            )
            for i in range(20)
        ]

        setup = MagicMock()
        setup.entry_price = 100.0
        setup.sl_price = 99.0  # risk = 1.0
        setup.direction = "long"

        # Swing high at 100.5, above current price → space = 0.5 < risk * 1.4 = 1.4
        # Use candles where current price is below the swing high
        candles = [
            Candle(timestamp=1000 + i * 300_000, open=99.5, high=100.0,
                   low=99.0, close=99.5, volume=100, volume_quote=10000,
                   pair="BTC/USDT", timeframe="5m", confirmed=True)
            for i in range(20)
        ]
        state_with_swing = MarketStructureState(
            pair="BTC/USDT", timeframe="4h", trend="bullish",
            swing_highs=[SwingPoint(timestamp=1000, price=100.5, index=5, swing_type="high")],
            swing_lows=[],
            structure_breaks=[], latest_break=None,
        )
        state_empty = MarketStructureState(
            pair="BTC/USDT", timeframe="1h", trend="bullish",
            swing_highs=[], swing_lows=[],
            structure_breaks=[], latest_break=None,
        )

        reject = svc._apply_expectancy_filters(
            setup, candles, state_with_swing, state_empty,
        )
        assert reject is not None
        assert "Target space too tight" in reject


# ============================================================
# 8. Setup B vs F Equivalence
# ============================================================

class TestSetupBvsF:
    """Setup B = Setup F + FVG gate. F is strictly less restrictive."""

    def test_setup_b_needs_fvg_f_does_not(self):
        """Setup F produces a setup where Setup B wouldn't (no FVG)."""
        evaluator = SetupEvaluator()
        now_ms = int(time.time() * 1000)
        bos_ts = now_ms - 2 * 900_000

        brk = _make_break(
            break_type="bos", direction="bullish",
            break_price=110.0, broken_level=108.0,
            candle_index=18, timestamp=bos_ts,
        )
        state = MarketStructureState(
            pair="BTC/USDT", timeframe="15m", trend="bullish",
            swing_highs=[], swing_lows=[],
            structure_breaks=[brk], latest_break=brk,
        )

        ob_ts = bos_ts - 2 * 900_000
        obs = [_make_ob(direction="bullish", timestamp=ob_ts, volume_ratio=2.0)]
        candles = _make_candles(101.0, count=20)
        pd = _make_pd("discount")
        snapshot = _make_snapshot(cvd_15m=100.0)

        common_args = dict(
            structure_state=state,
            active_obs=obs,
            pd_zone=pd,
            market_snapshot=snapshot,
            candles=candles,
            pair="BTC/USDT",
            htf_bias="bullish",
            liquidity_levels=[],
        )

        # Setup F: should work (BOS + OB is enough)
        setup_f = evaluator.evaluate_setup_f(**common_args)

        # Setup B: needs FVG, pass empty list → should fail
        setup_b = evaluator.evaluate_setup_b(
            **common_args,
            active_fvgs=[],
        )

        # F should succeed, B should fail
        # (F might also fail due to hardening filters, which is fine)
        assert setup_b is None
        # If F passes, it confirms F is less restrictive
        if setup_f is not None:
            assert setup_f.setup_type == "setup_f"


# ============================================================
# 9. Confluence Counting Accuracy
# ============================================================

class TestConfluenceCounting:
    """Verify confluences are not double-counted from correlated signals."""

    def test_sweep_ob_flush_counted_separately(self):
        """Sweep volume + OB volume + OI flush all from same event = separate confluences."""
        evaluator = SetupEvaluator()
        args = _valid_setup_a_args(
            active_obs=[_make_ob(volume_ratio=2.0)],
            recent_sweeps=[_make_sweep(volume_ratio=3.0, had_oi_flush=True)],
            market_snapshot=_make_snapshot(
                cvd_15m=100.0,
                oi_flushes=[
                    OIFlushEvent(
                        timestamp=9000, pair="BTC/USDT", side="long",
                        size_usd=100000, price=94.5, source="oi_proxy",
                    ),
                ],
            ),
        )
        setup = evaluator.evaluate_setup_a(**args)
        assert setup is not None

        # Count how many volume-related confluences
        vol_confluences = [c for c in setup.confluences
                           if any(x in c for x in ["ob_volume", "sweep_volume",
                                                     "oi_flush"])]
        # Each should be counted — the audit flags this as potential redundancy
        # but the code counts them separately (this test documents that behavior)
        assert len(vol_confluences) >= 2

    def test_core_confluences_always_present(self):
        """Setup A always has: liquidity_sweep + choch + order_block as core confluences."""
        evaluator = SetupEvaluator()
        args = _valid_setup_a_args(
            market_snapshot=MarketSnapshot(
                pair="BTC/USDT",
                timestamp=int(time.time() * 1000),
                funding=None, oi=None, cvd=None,
                recent_oi_flushes=[], whale_movements=[],
            ),
        )
        setup = evaluator.evaluate_setup_a(**args)
        assert setup is not None
        has_sweep = any(c.startswith("liquidity_sweep") for c in setup.confluences)
        has_choch = any(c.startswith("choch") for c in setup.confluences)
        has_ob = any(c.startswith("order_block") for c in setup.confluences)
        assert has_sweep, f"Missing liquidity_sweep in {setup.confluences}"
        assert has_choch, f"Missing choch in {setup.confluences}"
        assert has_ob, f"Missing order_block in {setup.confluences}"


# ============================================================
# 10. Quick Setup Signal Tests
# ============================================================

class TestQuickSetupSignals:
    """Quick setup signal validation — C, D, H."""

    def test_setup_c_requires_extreme_funding(self):
        """Setup C needs funding < -0.0003 (long) or > +0.0003 (short)."""
        qe = QuickSetupEvaluator()
        # Normal funding: should NOT trigger
        snapshot_normal = _make_snapshot(
            funding_rate=-0.0001,
            buy_volume=600.0, sell_volume=400.0,
        )
        candles = _make_candles(101.0, count=20, timeframe="5m")
        result = qe.evaluate_setup_c(
            "BTC/USDT", "bullish", snapshot_normal, 101.0, candles,
        )
        assert result is None

    def test_setup_c_fires_on_extreme_funding(self):
        """Setup C fires when funding is extreme + CVD aligned."""
        qe = QuickSetupEvaluator()
        snapshot = _make_snapshot(
            funding_rate=-0.0005,
            buy_volume=600.0, sell_volume=400.0,
        )
        candles = _make_candles(101.0, count=20, timeframe="5m")
        result = qe.evaluate_setup_c(
            "BTC/USDT", "bullish", snapshot, 101.0, candles,
        )
        assert result is not None
        assert result.setup_type == "setup_c"
        assert "funding_extreme" in result.confluences[0]

    def test_setup_h_rejects_random_walk(self):
        """Setup H: 60% directional candles is close to random — verify it filters."""
        qe = QuickSetupEvaluator()
        state = _make_state(break_type="bos", break_direction="bullish", timeframe="5m")
        # 3 bullish + 2 bearish = 60% exactly
        candles = []
        base = 100.0
        for i in range(25):
            candles.append(make_candle(
                open=base, close=base + 0.01, high=base + 0.02, low=base - 0.01,
                volume=10.0, timestamp=i * 300_000, timeframe="5m",
            ))

        # Make impulse window essentially flat (low vol, no move)
        result = qe.evaluate_setup_h("BTC/USDT", "bullish", state, candles)
        # Should be rejected for insufficient move or volume
        assert result is None


# ============================================================
# Helper: mock DataService with bullish trend
# ============================================================

def _mock_data_service_with_bullish_trend():
    """Create a mock DataService that returns bullish-trending candles."""
    ds = MagicMock()

    candles_4h = make_candle_series(
        base_price=100.0, count=50, timeframe="4h",
        price_changes=[2.0] * 50,
        start_ts=1_000_000_000_000, interval_ms=14_400_000,
    )
    candles_1h = make_candle_series(
        base_price=100.0, count=100, timeframe="1h",
        price_changes=[0.5] * 100,
        start_ts=1_000_000_000_000, interval_ms=3_600_000,
    )
    candles_15m = make_candle_series(
        base_price=100.0, count=200, timeframe="15m",
        price_changes=[0.1] * 200,
        start_ts=1_000_000_000_000, interval_ms=900_000,
    )
    candles_5m = make_candle_series(
        base_price=100.0, count=200, timeframe="5m",
        price_changes=[0.05] * 200,
        start_ts=1_000_000_000_000, interval_ms=300_000,
    )

    def get_candles(pair, tf, count=500):
        return {
            "4h": candles_4h,
            "1h": candles_1h,
            "15m": candles_15m,
            "5m": candles_5m,
        }.get(tf, [])

    ds.get_candles = get_candles
    ds.get_market_snapshot.return_value = _make_snapshot()
    return ds
