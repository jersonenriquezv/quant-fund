"""
Tests for Quick Setups (C, D, E).

Setup C: Funding Squeeze — extreme funding + CVD alignment
Setup D: LTF Structure Scalp — CHoCH/BOS on 5m + OB nearby
Setup E: Cascade Reversal — OI drop cascade + CVD reversal
"""

import time

import pytest

from config.settings import settings, QUICK_SETUP_TYPES
from shared.models import (
    Candle, MarketSnapshot, FundingRate, CVDSnapshot, OpenInterest,
    OIFlushEvent, TradeSetup,
)
from strategy_service.quick_setups import QuickSetupEvaluator
from strategy_service.market_structure import (
    MarketStructureState, StructureBreak, SwingPoint,
)
from strategy_service.order_blocks import OrderBlock
from strategy_service.liquidity import PremiumDiscountZone
from risk_service.guardrails import Guardrails
from tests.conftest import make_candle, make_candle_series


# ================================================================
# Fixtures
# ================================================================

@pytest.fixture
def evaluator():
    return QuickSetupEvaluator()


@pytest.fixture
def guardrails():
    return Guardrails()


def _make_snapshot(
    pair="BTC/USDT",
    funding_rate=0.0001,
    buy_volume=500.0,
    sell_volume=400.0,
    oi_flushes=None,
):
    ts = int(time.time() * 1000)
    funding = FundingRate(
        timestamp=ts, pair=pair, rate=funding_rate,
        next_rate=funding_rate, next_funding_time=ts + 28800000,
    )
    cvd = CVDSnapshot(
        timestamp=ts, pair=pair, cvd_5m=50.0, cvd_15m=100.0,
        cvd_1h=400.0, buy_volume=buy_volume, sell_volume=sell_volume,
    )
    return MarketSnapshot(
        pair=pair, timestamp=ts, funding=funding,
        oi=OpenInterest(timestamp=ts, pair=pair, oi_contracts=1000, oi_base=10, oi_usd=1000000),
        cvd=cvd,
        recent_oi_flushes=oi_flushes or [],
    )


def _make_ob(
    direction="bullish",
    entry_price=100.0,
    body_low=99.5,
    body_high=100.5,
    low=99.0,
    high=101.0,
    pair="BTC/USDT",
    timeframe="5m",
    volume_ratio=2.0,
):
    """Create a minimal OrderBlock for testing."""
    break_ = StructureBreak(
        timestamp=int(time.time() * 1000) - 60000,
        break_type="bos", direction=direction,
        break_price=101.0, broken_level=100.0, candle_index=10,
    )
    return OrderBlock(
        timestamp=int(time.time() * 1000) - 30000,
        pair=pair, timeframe=timeframe, direction=direction,
        high=high, low=low, body_high=body_high, body_low=body_low,
        entry_price=entry_price, volume=100.0, volume_ratio=volume_ratio,
        mitigated=False, associated_break=break_,
    )


def _make_pd_zone(zone="discount", pair="BTC/USDT"):
    return PremiumDiscountZone(
        pair=pair, range_high=110.0, range_low=90.0,
        equilibrium=100.0, last_updated_ms=int(time.time() * 1000),
        zone=zone,
    )


def _make_structure_state(
    break_type="choch",
    direction="bullish",
    pair="BTC/USDT",
    timeframe="5m",
):
    break_ = StructureBreak(
        timestamp=int(time.time() * 1000),
        break_type=break_type, direction=direction,
        break_price=101.0, broken_level=100.0, candle_index=45,
    )
    return MarketStructureState(
        pair=pair, timeframe=timeframe, trend=direction,
        swing_highs=[], swing_lows=[],
        structure_breaks=[break_], latest_break=break_,
    )


# ================================================================
# Setup C — Funding Squeeze
# ================================================================

class TestSetupC:
    """Setup C removed 2026-04-13 — signal demoted to confluence booster."""

    def test_setup_c_always_returns_none(self, evaluator):
        """Setup C removed — method returns None unconditionally."""
        snapshot = _make_snapshot(funding_rate=-0.0005, buy_volume=600, sell_volume=400)
        candles = make_candle_series(base_price=100.0, count=20)
        result = evaluator.evaluate_setup_c("BTC/USDT", "bullish", snapshot, 100.0, candles)
        assert result is None


# ================================================================
# Setup D — LTF Structure Scalp
# ================================================================

class TestSetupD:

    def test_choch_plus_ob_long(self, evaluator):
        """CHoCH bullish on 5m + OB nearby + discount zone → long."""
        state = _make_structure_state("choch", "bullish")
        ob = _make_ob(direction="bullish", entry_price=100.0)
        pd = _make_pd_zone("discount")
        # Price stays near 100 — use flat price changes
        candles = make_candle_series(
            base_price=100.0, count=50, timeframe="5m",
            price_changes=[0.0] * 50,
        )

        result = evaluator.evaluate_setup_d(
            "BTC/USDT", "bullish", state, [ob], pd, candles,
        )
        assert result is not None
        assert result.setup_type == "setup_d_choch"
        assert result.direction == "long"
        assert "choch_5m" in result.confluences
        assert any("order_block" in c for c in result.confluences)

    def test_bos_plus_ob_short(self, evaluator):
        """BOS bearish on 5m + OB nearby + premium zone → short."""
        state = _make_structure_state("bos", "bearish", timeframe="5m")
        ob = _make_ob(
            direction="bearish", entry_price=100.0,
            body_low=99.5, body_high=100.5, low=99.0, high=101.0,
        )
        pd = _make_pd_zone("premium")
        candles = make_candle_series(
            base_price=100.0, count=50, timeframe="5m",
            price_changes=[0.0] * 50,
        )

        result = evaluator.evaluate_setup_d(
            "BTC/USDT", "bearish", state, [ob], pd, candles,
        )
        assert result is not None
        assert result.setup_type == "setup_d_bos"
        assert result.direction == "short"
        assert "bos_5m" in result.confluences

    def test_rejects_wrong_timeframe(self, evaluator):
        """Non-5m structure state → reject."""
        state = _make_structure_state("choch", "bullish", timeframe="15m")
        ob = _make_ob(direction="bullish")
        pd = _make_pd_zone("discount")
        candles = make_candle_series(count=50, timeframe="5m")

        result = evaluator.evaluate_setup_d(
            "BTC/USDT", "bullish", state, [ob], pd, candles,
        )
        assert result is None

    def test_rejects_no_ob_nearby(self, evaluator):
        """CHoCH but no OB near price → reject."""
        state = _make_structure_state("choch", "bullish")
        # OB far from current price
        ob = _make_ob(direction="bullish", entry_price=200.0,
                      body_low=199.5, body_high=200.5, low=199.0, high=201.0)
        pd = _make_pd_zone("discount")
        candles = make_candle_series(base_price=100.0, count=50, timeframe="5m")

        result = evaluator.evaluate_setup_d(
            "BTC/USDT", "bullish", state, [ob], pd, candles,
        )
        assert result is None

    def test_rejects_pd_misalignment(self, evaluator):
        """Bullish direction in premium zone → reject (when PD is hard gate)."""
        original = settings.PD_AS_CONFLUENCE
        settings.PD_AS_CONFLUENCE = False
        try:
            state = _make_structure_state("choch", "bullish")
            ob = _make_ob(direction="bullish")
            pd = _make_pd_zone("premium")  # Wrong zone for long
            candles = make_candle_series(base_price=100.0, count=50, timeframe="5m")

            result = evaluator.evaluate_setup_d(
                "BTC/USDT", "bullish", state, [ob], pd, candles,
            )
            assert result is None
        finally:
            settings.PD_AS_CONFLUENCE = original

    def test_rejects_htf_conflict(self, evaluator):
        """Bullish break but bearish HTF → reject."""
        state = _make_structure_state("choch", "bullish")
        ob = _make_ob(direction="bullish")
        pd = _make_pd_zone("discount")
        candles = make_candle_series(count=50, timeframe="5m")

        result = evaluator.evaluate_setup_d(
            "BTC/USDT", "bearish", state, [ob], pd, candles,
        )
        assert result is None


# ================================================================
# Setup E — Cascade Reversal
# ================================================================

class TestSetupE:
    """Setup E removed 2026-04-13 — signal demoted to confluence booster."""

    def test_setup_e_always_returns_none(self, evaluator):
        """Setup E removed — method returns None unconditionally."""
        ts = int(time.time() * 1000)
        liq = OIFlushEvent(
            timestamp=ts - 5000, pair="BTC/USDT", side="long",
            size_usd=500000, price=99000.0, source="oi_proxy",
        )
        snapshot = _make_snapshot(buy_volume=550, sell_volume=450, oi_flushes=[liq])
        candles = make_candle_series(base_price=99.0, count=20, timeframe="5m")
        result = evaluator.evaluate_setup_e("BTC/USDT", "bullish", snapshot, [], candles, 99.0)
        assert result is None


# ================================================================
# Quick Setup Types Constant
# ================================================================

class TestQuickSetupTypes:

    def test_quick_setup_types(self):
        assert "setup_c" in QUICK_SETUP_TYPES
        assert "setup_d_bos" in QUICK_SETUP_TYPES
        assert "setup_d_choch" in QUICK_SETUP_TYPES
        assert "setup_e" in QUICK_SETUP_TYPES
        # setup_d bare string is never emitted by strategy — only the
        # _bos / _choch variants reach the pipeline. Removed in audit fase 2 #12.
        assert "setup_d" not in QUICK_SETUP_TYPES
        assert "setup_h" not in QUICK_SETUP_TYPES  # Removed 2026-04-13
        assert "setup_a" not in QUICK_SETUP_TYPES  # Swing setup
        assert "setup_b" not in QUICK_SETUP_TYPES  # Swing setup


# ================================================================
# R:R Check — Quick vs Swing
# ================================================================

class TestQuickRR:

    def test_quick_setup_allows_lower_rr(self, guardrails):
        """Quick setup with 1.6 R:R passes (min 1.5), swing would fail (min 2.0)."""
        setup_quick = TradeSetup(
            timestamp=int(time.time() * 1000),
            pair="BTC/USDT", direction="long", setup_type="setup_c",
            entry_price=100.0, sl_price=99.0,
            tp1_price=101.0,
            tp2_price=101.6,  # R:R = 1.6
            confluences=["a", "b"], htf_bias="bullish", ob_timeframe="5m",
        )
        passed, reason = guardrails.check_rr_ratio(setup_quick)
        assert passed, f"Quick setup should pass with R:R 1.6: {reason}"

    def test_swing_setup_rejects_low_rr(self, guardrails):
        """Swing setup with 1.8 R:R fails (min 2.0)."""
        setup_swing = TradeSetup(
            timestamp=int(time.time() * 1000),
            pair="BTC/USDT", direction="long", setup_type="setup_a",
            entry_price=100.0, sl_price=99.0,
            tp1_price=101.0,
            tp2_price=101.8,  # R:R = 1.8
            confluences=["a", "b"], htf_bias="bullish", ob_timeframe="5m",
        )
        passed, reason = guardrails.check_rr_ratio(setup_swing)
        assert not passed, f"Swing setup should fail with R:R 1.8: {reason}"


# ================================================================
# Cooldown — QuickSetupEvaluator (via StrategyService)
# ================================================================

class TestQuickCooldown:

    def test_cooldown_blocks_repeated_setup(self):
        """Quick setup cooldown prevents re-triggering within QUICK_SETUP_COOLDOWN."""
        from strategy_service.service import StrategyService

        # Create service with mock data service
        class MockDataService:
            def get_candles(self, *a):
                return []
            def get_market_snapshot(self, *a):
                return None

        svc = StrategyService(MockDataService())

        # Simulate cooldown
        now = time.time()
        svc._quick_setup_last[("BTC/USDT", "setup_c")] = now

        # Should be in cooldown
        assert svc._is_quick_cooldown_active("BTC/USDT", "setup_c", now + 10)

        # Should be expired
        assert not svc._is_quick_cooldown_active(
            "BTC/USDT", "setup_c",
            now + settings.QUICK_SETUP_COOLDOWN + 1,
        )

    def test_cooldown_per_pair_per_type(self):
        """Cooldown is per (pair, setup_type) — different pair is not blocked."""
        from strategy_service.service import StrategyService

        class MockDataService:
            def get_candles(self, *a):
                return []
            def get_market_snapshot(self, *a):
                return None

        svc = StrategyService(MockDataService())
        now = time.time()
        svc._quick_setup_last[("BTC/USDT", "setup_c")] = now

        # Same pair, same type — blocked
        assert svc._is_quick_cooldown_active("BTC/USDT", "setup_c", now + 10)
        # Different pair — not blocked
        assert not svc._is_quick_cooldown_active("ETH/USDT", "setup_c", now + 10)
        # Same pair, different type — not blocked
        assert not svc._is_quick_cooldown_active("BTC/USDT", "setup_d", now + 10)


# ================================================================
# Phase 2: Setup D minimum break displacement
# ================================================================

class TestSetupDDisplacement:
    """Test SETUP_D_MIN_DISPLACEMENT_PCT filter."""

    def test_displacement_zero_allows_all(self, evaluator):
        """Default (0.0) allows any break displacement."""
        original = settings.SETUP_D_MIN_DISPLACEMENT_PCT
        settings.SETUP_D_MIN_DISPLACEMENT_PCT = 0.0
        try:
            state = _make_structure_state("choch", "bullish")
            ob = _make_ob(direction="bullish", entry_price=100.0)
            pd = _make_pd_zone("discount")
            candles = make_candle_series(base_price=100.0, count=50, timeframe="5m",
                                        price_changes=[0.0] * 50)
            result = evaluator.evaluate_setup_d(
                "BTC/USDT", "bullish", state, [ob], pd, candles,
            )
            assert result is not None
            assert result.setup_type == "setup_d_choch"
            assert result.direction == "long"
            # Entry must be inside OB body range (per SETUP_D_ENTRY_PCT)
            assert 99.5 <= result.entry_price <= 100.5
            # SL must be below entry (long) and equal to OB low
            assert result.sl_price == pytest.approx(99.0, abs=0.001)
        finally:
            settings.SETUP_D_MIN_DISPLACEMENT_PCT = original

    def test_displacement_filters_weak_break(self, evaluator):
        """Break with displacement below threshold → rejected."""
        original = settings.SETUP_D_MIN_DISPLACEMENT_PCT
        settings.SETUP_D_MIN_DISPLACEMENT_PCT = 0.05  # 5% — very high
        try:
            # Default break: break_price=101, broken_level=100 → 1% displacement
            state = _make_structure_state("choch", "bullish")
            ob = _make_ob(direction="bullish", entry_price=100.0)
            pd = _make_pd_zone("discount")
            candles = make_candle_series(base_price=100.0, count=50, timeframe="5m",
                                        price_changes=[0.0] * 50)
            result = evaluator.evaluate_setup_d(
                "BTC/USDT", "bullish", state, [ob], pd, candles,
            )
            assert result is None
        finally:
            settings.SETUP_D_MIN_DISPLACEMENT_PCT = original

    def test_displacement_allows_strong_break(self, evaluator):
        """Break with displacement above threshold → allowed."""
        original = settings.SETUP_D_MIN_DISPLACEMENT_PCT
        settings.SETUP_D_MIN_DISPLACEMENT_PCT = 0.005  # 0.5%
        try:
            # break_price=101, broken_level=100 → 1% displacement > 0.5%
            state = _make_structure_state("choch", "bullish")
            ob = _make_ob(direction="bullish", entry_price=100.0)
            pd = _make_pd_zone("discount")
            candles = make_candle_series(base_price=100.0, count=50, timeframe="5m",
                                        price_changes=[0.0] * 50)
            result = evaluator.evaluate_setup_d(
                "BTC/USDT", "bullish", state, [ob], pd, candles,
            )
            assert result is not None
            # Confluence list must include the CHoCH + OB (core signals for setup_d)
            assert "choch_5m" in result.confluences
            assert any("order_block" in c for c in result.confluences)
        finally:
            settings.SETUP_D_MIN_DISPLACEMENT_PCT = original


# ================================================================
# Phase 2: PD_AS_CONFLUENCE on Setup D
# ================================================================

class TestSetupDPDAsConfluence:
    """Test PD_AS_CONFLUENCE flag on Setup D."""

    def test_pd_misaligned_blocks_when_hard_gate(self, evaluator):
        """PD_AS_CONFLUENCE=False: PD misalignment blocks Setup D."""
        original = settings.PD_AS_CONFLUENCE
        settings.PD_AS_CONFLUENCE = False
        try:
            state = _make_structure_state("choch", "bullish")
            ob = _make_ob(direction="bullish", entry_price=100.0)
            pd = _make_pd_zone("premium")  # Wrong for long
            candles = make_candle_series(base_price=100.0, count=50, timeframe="5m",
                                        price_changes=[0.0] * 50)
            result = evaluator.evaluate_setup_d(
                "BTC/USDT", "bullish", state, [ob], pd, candles,
            )
            assert result is None
        finally:
            settings.PD_AS_CONFLUENCE = original

    def test_pd_as_confluence_allows_misaligned(self, evaluator):
        """PD_AS_CONFLUENCE=True: PD misalignment does NOT block Setup D."""
        original = settings.PD_AS_CONFLUENCE
        settings.PD_AS_CONFLUENCE = True
        try:
            state = _make_structure_state("choch", "bullish")
            ob = _make_ob(direction="bullish", entry_price=100.0)
            pd = _make_pd_zone("premium")  # Wrong for long
            candles = make_candle_series(base_price=100.0, count=50, timeframe="5m",
                                        price_changes=[0.0] * 50)
            result = evaluator.evaluate_setup_d(
                "BTC/USDT", "bullish", state, [ob], pd, candles,
            )
            assert result is not None
            # PD zone should NOT be in confluences (misaligned)
            assert not any("pd_zone" in c for c in result.confluences)
        finally:
            settings.PD_AS_CONFLUENCE = original

    def test_pd_as_confluence_adds_aligned_zone(self, evaluator):
        """PD_AS_CONFLUENCE=True: aligned PD zone IS added as confluence."""
        original = settings.PD_AS_CONFLUENCE
        settings.PD_AS_CONFLUENCE = True
        try:
            state = _make_structure_state("choch", "bullish")
            ob = _make_ob(direction="bullish", entry_price=100.0)
            pd = _make_pd_zone("discount")  # Correct for long
            candles = make_candle_series(base_price=100.0, count=50, timeframe="5m",
                                        price_changes=[0.0] * 50)
            result = evaluator.evaluate_setup_d(
                "BTC/USDT", "bullish", state, [ob], pd, candles,
            )
            assert result is not None
            assert "pd_zone_discount" in result.confluences
        finally:
            settings.PD_AS_CONFLUENCE = original


# ================================================================
# Setup D — Structural TP (Batch 4, 2026-04-21)
# ================================================================

from strategy_service.volume_profile import VolumeProfile


def _make_vp(poc=103.0, vah=105.0, val=98.0, hvns=None):
    return VolumeProfile(
        poc_price=poc, vah=vah, val=val,
        high_volume_nodes=hvns or [],
        low_volume_nodes=[], total_volume=10000.0,
        price_low=95.0, price_high=110.0, bin_size=0.1,
        computed_at=int(time.time() * 1000),
    )


class TestSetupDStructuralTP:
    """Batch 4 port — setup_d must use _calculate_tp_levels (structural when
    available, fixed R:R fallback). Prior to Batch 4 it hardcoded fixed R:R
    at TP1_RR_RATIO / SETUP_TP2_RR[variant].
    """

    def test_falls_back_to_fixed_rr_when_no_structural_data(self, evaluator):
        """No HTF swings + no VP → identical output to pre-Batch-4 math."""
        state = _make_structure_state("choch", "bullish")
        ob = _make_ob(direction="bullish", entry_price=100.0)
        pd = _make_pd_zone("discount")
        candles = make_candle_series(
            base_price=100.0, count=50, timeframe="5m",
            price_changes=[0.0] * 50,
        )

        result = evaluator.evaluate_setup_d(
            "BTC/USDT", "bullish", state, [ob], pd, candles,
            snapshot=None,
        )
        assert result is not None
        risk = abs(result.entry_price - result.sl_price)
        expected_tp1 = result.entry_price + risk * settings.TP1_RR_RATIO
        tp2_rr = settings.SETUP_TP2_RR.get("setup_d_choch", settings.TP2_RR_RATIO)
        expected_tp2 = result.entry_price + risk * tp2_rr
        assert result.tp1_price == pytest.approx(expected_tp1, abs=0.001)
        assert result.tp2_price == pytest.approx(expected_tp2, abs=0.001)

    def test_uses_structural_tp2_when_swing_beats_fixed_rr(self, evaluator):
        """HTF swing high above fixed TP2 → tp2 snaps to structural level.

        Risk is tiny in this test (100.0 - 99.0 = 1.0), so fixed TP2 at
        1.5x is 101.5. A swing high at 108 sits far above — must be
        structural_tp2 (beats fixed).
        """
        state = _make_structure_state("choch", "bullish")
        ob = _make_ob(direction="bullish", entry_price=100.0,
                      body_low=99.5, body_high=100.5, low=99.0, high=101.0)
        pd = _make_pd_zone("discount")
        candles = make_candle_series(
            base_price=100.0, count=50, timeframe="5m",
            price_changes=[0.0] * 50,
        )

        swing_highs = [
            SwingPoint(price=103.5, timestamp=1000, index=10, swing_type="high"),
            SwingPoint(price=108.0, timestamp=2000, index=20, swing_type="high"),
        ]

        result = evaluator.evaluate_setup_d(
            "BTC/USDT", "bullish", state, [ob], pd, candles,
            snapshot=None,
            swing_highs_htf=swing_highs,
            swing_lows_htf=[],
        )
        assert result is not None
        # TP2 must be structural, not the tight fixed fallback
        assert result.tp2_price in (103.5, 108.0), (
            f"tp2 must pick a structural swing high, got {result.tp2_price}"
        )
        # And must be strictly above entry (we're long)
        assert result.tp2_price > result.entry_price

    def test_short_uses_swing_lows(self, evaluator):
        """Bearish setup_d: must prefer swing lows below entry."""
        state = _make_structure_state("bos", "bearish", timeframe="5m")
        ob = _make_ob(direction="bearish", entry_price=100.0,
                      body_low=99.5, body_high=100.5, low=99.0, high=101.0)
        pd = _make_pd_zone("premium")
        candles = make_candle_series(
            base_price=100.0, count=50, timeframe="5m",
            price_changes=[0.0] * 50,
        )

        swing_lows = [
            SwingPoint(price=96.5, timestamp=1000, index=10, swing_type="low"),
            SwingPoint(price=92.0, timestamp=2000, index=20, swing_type="low"),
        ]

        result = evaluator.evaluate_setup_d(
            "BTC/USDT", "bearish", state, [ob], pd, candles,
            snapshot=None,
            swing_highs_htf=[],
            swing_lows_htf=swing_lows,
        )
        assert result is not None
        assert result.tp2_price in (96.5, 92.0)
        assert result.tp2_price < result.entry_price

    def test_volume_profile_poc_as_tp_candidate(self, evaluator):
        """VP POC above entry for long → viable TP candidate."""
        state = _make_structure_state("choch", "bullish")
        ob = _make_ob(direction="bullish", entry_price=100.0)
        pd = _make_pd_zone("discount")
        candles = make_candle_series(
            base_price=100.0, count=50, timeframe="5m",
            price_changes=[0.0] * 50,
        )
        vp = _make_vp(poc=104.0, vah=106.0, val=97.0)

        result = evaluator.evaluate_setup_d(
            "BTC/USDT", "bullish", state, [ob], pd, candles,
            snapshot=None,
            volume_profile=vp,
        )
        assert result is not None
        # TP must be at one of: POC 104, VAH 106, or fallback fixed
        # Fixed TP2 = entry + risk*1.5 ≈ 100 + 1.0*1.5 = 101.5
        # With structural candidates [104, 106] both beat fixed → should snap
        assert result.tp2_price in (104.0, 106.0, pytest.approx(101.5, abs=0.01))
        assert result.tp2_price > result.entry_price

    def test_tp2_rr_never_below_fixed_minimum(self, evaluator):
        """If all structural levels are closer than fixed TP2, fixed wins."""
        state = _make_structure_state("choch", "bullish")
        ob = _make_ob(direction="bullish", entry_price=100.0,
                      body_low=99.5, body_high=100.5, low=99.0, high=101.0)
        pd = _make_pd_zone("discount")
        candles = make_candle_series(
            base_price=100.0, count=50, timeframe="5m",
            price_changes=[0.0] * 50,
        )
        # Swing highs too close — fail R:R gate
        swing_highs = [
            SwingPoint(price=100.05, timestamp=1000, index=10, swing_type="high"),
        ]

        result = evaluator.evaluate_setup_d(
            "BTC/USDT", "bullish", state, [ob], pd, candles,
            snapshot=None,
            swing_highs_htf=swing_highs,
            swing_lows_htf=[],
        )
        assert result is not None
        # Should have fallen back to fixed TP2 at ~101.5 (100 + 1.0*1.5)
        risk = abs(result.entry_price - result.sl_price)
        tp2_rr = settings.SETUP_TP2_RR.get("setup_d_choch", settings.TP2_RR_RATIO)
        expected_fixed_tp2 = result.entry_price + risk * tp2_rr
        assert result.tp2_price == pytest.approx(expected_fixed_tp2, abs=0.01)


# ================================================================
# Setup H — REMOVED 2026-04-13 (retail momentum chase, 0/13 WR)
# ================================================================

class TestSetupH:

    def test_setup_h_always_returns_none(self, evaluator):
        """Setup H removed — method returns None unconditionally."""
        state = _make_structure_state("bos", "bullish")
        candles = [Candle(
            timestamp=1_000_000_000_000 + i * 300_000,
            open=100.0, high=100.1, low=99.9, close=100.05,
            volume=10.0, volume_quote=1000.0,
            pair="BTC/USDT", timeframe="5m", confirmed=True,
        ) for i in range(30)]
        result = evaluator.evaluate_setup_h("BTC/USDT", "bullish", state, candles)
        assert result is None
