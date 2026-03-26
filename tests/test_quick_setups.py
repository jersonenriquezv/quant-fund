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

    def test_long_funding_squeeze(self, evaluator):
        """Negative funding + buy dominance > 55% + bullish HTF → long."""
        snapshot = _make_snapshot(
            funding_rate=-0.0005,  # Very negative
            buy_volume=600, sell_volume=400,  # 60% buy dominance
        )
        candles = make_candle_series(base_price=100.0, count=20)
        result = evaluator.evaluate_setup_c(
            "BTC/USDT", "bullish", snapshot, 100.0, candles,
        )
        assert result is not None
        assert result.setup_type == "setup_c"
        assert result.direction == "long"
        assert result.sl_price < result.entry_price
        assert result.tp1_price > result.entry_price
        assert len(result.confluences) >= 2

    def test_short_funding_squeeze(self, evaluator):
        """Positive funding + sell dominance > 55% + bearish HTF → short."""
        snapshot = _make_snapshot(
            funding_rate=0.0005,
            buy_volume=400, sell_volume=600,  # 40% buy dominance
        )
        candles = make_candle_series(base_price=100.0, count=20)
        result = evaluator.evaluate_setup_c(
            "ETH/USDT", "bearish", snapshot, 100.0, candles,
        )
        assert result is not None
        assert result.direction == "short"
        assert result.sl_price > result.entry_price
        assert result.tp1_price < result.entry_price

    def test_rejects_normal_funding(self, evaluator):
        """Normal funding rate → no setup."""
        snapshot = _make_snapshot(
            funding_rate=0.0001,  # Not extreme
            buy_volume=600, sell_volume=400,
        )
        candles = make_candle_series(count=20)
        result = evaluator.evaluate_setup_c(
            "BTC/USDT", "bullish", snapshot, 100.0, candles,
        )
        assert result is None

    def test_rejects_htf_conflict(self, evaluator):
        """Negative funding (long signal) + bearish HTF → reject."""
        snapshot = _make_snapshot(
            funding_rate=-0.0005,
            buy_volume=600, sell_volume=400,
        )
        candles = make_candle_series(count=20)
        result = evaluator.evaluate_setup_c(
            "BTC/USDT", "bearish", snapshot, 100.0, candles,
        )
        assert result is None

    def test_rejects_cvd_misalignment(self, evaluator):
        """Negative funding but buy dominance too low → reject."""
        snapshot = _make_snapshot(
            funding_rate=-0.0005,
            buy_volume=500, sell_volume=500,  # 50% — below 55% threshold
        )
        candles = make_candle_series(count=20)
        result = evaluator.evaluate_setup_c(
            "BTC/USDT", "bullish", snapshot, 100.0, candles,
        )
        assert result is None

    def test_rejects_missing_data(self, evaluator):
        """No snapshot → no setup."""
        candles = make_candle_series(count=20)
        result = evaluator.evaluate_setup_c(
            "BTC/USDT", "bullish", None, 100.0, candles,
        )
        assert result is None

    def test_sl_distance(self, evaluator):
        """SL is 0.5% from entry."""
        snapshot = _make_snapshot(
            funding_rate=-0.0005,
            buy_volume=600, sell_volume=400,
        )
        candles = make_candle_series(base_price=50000.0, count=20)
        result = evaluator.evaluate_setup_c(
            "BTC/USDT", "bullish", snapshot, 50000.0, candles,
        )
        assert result is not None
        expected_sl = 50000.0 * (1 - settings.MOMENTUM_SL_PCT)
        assert abs(result.sl_price - expected_sl) < 0.01


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

    def test_long_cascade_reversal(self, evaluator):
        """Long liquidation cascade + CVD reversal → long entry."""
        ts = int(time.time() * 1000)
        liq = OIFlushEvent(
            timestamp=ts - 5000,  # 5 sec ago
            pair="BTC/USDT", side="long", size_usd=500000,
            price=99000.0, source="oi_proxy",
        )
        snapshot = _make_snapshot(
            funding_rate=0.0001,
            buy_volume=550, sell_volume=450,  # 55% buy dom > 50% threshold
            oi_flushes=[liq],
        )
        candles = make_candle_series(base_price=99.0, count=20, timeframe="5m")

        result = evaluator.evaluate_setup_e(
            "BTC/USDT", "bullish", snapshot, [], candles, 99.0,
        )
        assert result is not None
        assert result.setup_type == "setup_e"
        assert result.direction == "long"
        assert any("cascade" in c for c in result.confluences)
        assert any("cvd_reversal" in c for c in result.confluences)

    def test_short_cascade_reversal(self, evaluator):
        """Short liquidation cascade + CVD reversal → short entry."""
        ts = int(time.time() * 1000)
        liq = OIFlushEvent(
            timestamp=ts - 5000,
            pair="BTC/USDT", side="short", size_usd=500000,
            price=101000.0, source="oi_proxy",
        )
        snapshot = _make_snapshot(
            funding_rate=0.0001,
            buy_volume=450, sell_volume=550,  # 45% buy dom < 50% threshold
            oi_flushes=[liq],
        )
        candles = make_candle_series(base_price=101.0, count=20, timeframe="5m")

        result = evaluator.evaluate_setup_e(
            "BTC/USDT", "bearish", snapshot, [], candles, 101.0,
        )
        assert result is not None
        assert result.direction == "short"

    def test_rejects_no_cascade(self, evaluator):
        """No liquidation events → reject."""
        snapshot = _make_snapshot(oi_flushes=[])
        candles = make_candle_series(count=20, timeframe="5m")

        result = evaluator.evaluate_setup_e(
            "BTC/USDT", "bullish", snapshot, [], candles, 100.0,
        )
        assert result is None

    def test_rejects_old_cascade(self, evaluator):
        """Cascade older than 15 minutes → reject."""
        ts = int(time.time() * 1000)
        liq = OIFlushEvent(
            timestamp=ts - 1_200_000,  # 20 min ago
            pair="BTC/USDT", side="long", size_usd=500000,
            price=99000.0, source="oi_proxy",
        )
        snapshot = _make_snapshot(
            buy_volume=550, sell_volume=450,
            oi_flushes=[liq],
        )
        candles = make_candle_series(count=20, timeframe="5m")

        result = evaluator.evaluate_setup_e(
            "BTC/USDT", "bullish", snapshot, [], candles, 100.0,
        )
        assert result is None

    def test_rejects_wrong_cvd_direction(self, evaluator):
        """Long cascade but buy dominance too low → reject."""
        ts = int(time.time() * 1000)
        liq = OIFlushEvent(
            timestamp=ts - 5000,
            pair="BTC/USDT", side="long", size_usd=500000,
            price=99000.0, source="oi_proxy",
        )
        snapshot = _make_snapshot(
            buy_volume=400, sell_volume=600,  # 40% — below 50% threshold
            oi_flushes=[liq],
        )
        candles = make_candle_series(count=20, timeframe="5m")

        result = evaluator.evaluate_setup_e(
            "BTC/USDT", "bullish", snapshot, [], candles, 100.0,
        )
        assert result is None

    def test_uses_ob_as_entry_anchor(self, evaluator):
        """When OB is nearby, use OB entry price instead of current price."""
        ts = int(time.time() * 1000)
        liq = OIFlushEvent(
            timestamp=ts - 5000,
            pair="BTC/USDT", side="long", size_usd=500000,
            price=99000.0, source="oi_proxy",
        )
        snapshot = _make_snapshot(
            buy_volume=550, sell_volume=450,
            oi_flushes=[liq],
        )
        ob = _make_ob(direction="bullish", entry_price=99.5, low=98.5)
        candles = make_candle_series(base_price=99.5, count=20, timeframe="5m")

        result = evaluator.evaluate_setup_e(
            "BTC/USDT", "bullish", snapshot, [ob], candles, 99.5,
        )
        assert result is not None
        assert result.entry_price == 99.5
        assert any("order_block" in c for c in result.confluences)

    def test_rejects_htf_conflict(self, evaluator):
        """Long cascade reversal but bearish HTF → reject."""
        ts = int(time.time() * 1000)
        liq = OIFlushEvent(
            timestamp=ts - 5000,
            pair="BTC/USDT", side="long", size_usd=500000,
            price=99000.0, source="oi_proxy",
        )
        snapshot = _make_snapshot(
            buy_volume=550, sell_volume=450,
            oi_flushes=[liq],
        )
        candles = make_candle_series(count=20, timeframe="5m")

        result = evaluator.evaluate_setup_e(
            "BTC/USDT", "bearish", snapshot, [], candles, 100.0,
        )
        assert result is None


# ================================================================
# Quick Setup Types Constant
# ================================================================

class TestQuickSetupTypes:

    def test_quick_setup_types(self):
        assert "setup_c" in QUICK_SETUP_TYPES
        assert "setup_d" in QUICK_SETUP_TYPES
        assert "setup_d_bos" in QUICK_SETUP_TYPES
        assert "setup_d_choch" in QUICK_SETUP_TYPES
        assert "setup_e" in QUICK_SETUP_TYPES
        assert "setup_h" in QUICK_SETUP_TYPES
        assert "setup_a" not in QUICK_SETUP_TYPES
        assert "setup_b" not in QUICK_SETUP_TYPES


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
# Setup H — Momentum/Impulse Entry
# ================================================================

class TestSetupH:

    def _make_impulse_candles(
        self,
        direction="bullish",
        base_price=100.0,
        count=30,
        impulse_count=5,
        impulse_move=0.5,
        impulse_volume=30.0,
        normal_volume=10.0,
        timeframe="5m",
    ):
        """Create candle series with a volume impulse at the end."""
        candles = []
        ts = 1_000_000_000_000
        interval = 300_000 if timeframe == "5m" else 900_000
        price = base_price

        # Normal candles (before impulse)
        for i in range(count - impulse_count):
            delta = 0.05  # Small random movement
            candles.append(Candle(
                timestamp=ts + i * interval,
                open=price, high=price + 0.1, low=price - 0.1,
                close=price + delta,
                volume=normal_volume, volume_quote=normal_volume * price,
                pair="BTC/USDT", timeframe=timeframe, confirmed=True,
            ))
            price += delta

        # Impulse candles
        step = impulse_move / impulse_count
        for i in range(impulse_count):
            idx = count - impulse_count + i
            if direction == "bullish":
                open_p = price
                close_p = price + step
            else:
                open_p = price
                close_p = price - step

            candles.append(Candle(
                timestamp=ts + idx * interval,
                open=open_p,
                high=max(open_p, close_p) + 0.05,
                low=min(open_p, close_p) - 0.05,
                close=close_p,
                volume=impulse_volume,
                volume_quote=impulse_volume * price,
                pair="BTC/USDT", timeframe=timeframe, confirmed=True,
            ))
            price = close_p

        return candles

    def test_bullish_impulse_detected(self, evaluator):
        """Bullish impulse with volume spike + BOS → setup_h long."""
        candles = self._make_impulse_candles(
            direction="bullish", base_price=100.0,
            impulse_move=1.2, impulse_volume=30.0, normal_volume=10.0,
        )
        state = _make_structure_state("bos", "bullish")

        result = evaluator.evaluate_setup_h(
            "BTC/USDT", "bullish", state, candles,
        )
        assert result is not None
        assert result.setup_type == "setup_h"
        assert result.direction == "long"
        assert result.entry_price == candles[-1].close
        assert result.sl_price < result.entry_price
        assert any("impulse_move" in c for c in result.confluences)
        assert any("volume_spike" in c for c in result.confluences)
        assert "bos_confirmed" in result.confluences

    def test_bearish_impulse_detected(self, evaluator):
        """Bearish impulse with volume spike + BOS → setup_h short."""
        candles = self._make_impulse_candles(
            direction="bearish", base_price=100.0,
            impulse_move=1.2, impulse_volume=30.0, normal_volume=10.0,
        )
        state = _make_structure_state("bos", "bearish")

        result = evaluator.evaluate_setup_h(
            "BTC/USDT", "bearish", state, candles,
        )
        assert result is not None
        assert result.direction == "short"
        assert result.sl_price > result.entry_price

    def test_rejects_no_volume_spike(self, evaluator):
        """Directional move without volume spike → reject."""
        candles = self._make_impulse_candles(
            direction="bullish", impulse_volume=10.0, normal_volume=10.0,
        )
        state = _make_structure_state("bos", "bullish")

        result = evaluator.evaluate_setup_h(
            "BTC/USDT", "bullish", state, candles,
        )
        assert result is None

    def test_rejects_small_move(self, evaluator):
        """Volume spike but move too small → reject."""
        candles = self._make_impulse_candles(
            direction="bullish", impulse_move=0.1,  # 0.1% < 0.3% threshold
            impulse_volume=30.0, normal_volume=10.0,
        )
        state = _make_structure_state("bos", "bullish")

        result = evaluator.evaluate_setup_h(
            "BTC/USDT", "bullish", state, candles,
        )
        assert result is None

    def test_rejects_htf_conflict(self, evaluator):
        """Bullish impulse but bearish HTF → reject."""
        candles = self._make_impulse_candles(direction="bullish")
        state = _make_structure_state("bos", "bullish")

        result = evaluator.evaluate_setup_h(
            "BTC/USDT", "bearish", state, candles,
        )
        assert result is None

    def test_rejects_no_bos(self, evaluator):
        """Impulse without any structure break → reject."""
        candles = self._make_impulse_candles(direction="bullish")
        # Structure state with bearish break only (no bullish BOS)
        state = _make_structure_state("bos", "bearish")

        result = evaluator.evaluate_setup_h(
            "BTC/USDT", "bullish", state, candles,
        )
        assert result is None

    def test_rejects_choppy_candles(self, evaluator):
        """Alternating candle colors → no directional impulse → reject."""
        candles = self._make_impulse_candles(
            direction="bullish", impulse_volume=30.0, normal_volume=10.0,
        )
        # Override impulse candles: make 3 of 5 bearish (only 40% bullish < 60% threshold)
        n = settings.SETUP_H_MIN_IMPULSE_CANDLES
        for i in range(-n, -n + 3):  # First 3 of 5 impulse candles → bearish
            c = candles[i]
            candles[i] = Candle(
                timestamp=c.timestamp, open=c.close, high=c.high,
                low=c.low, close=c.open,  # Reversed = bearish
                volume=c.volume, volume_quote=c.volume_quote,
                pair=c.pair, timeframe=c.timeframe, confirmed=True,
            )
        state = _make_structure_state("bos", "bullish")

        result = evaluator.evaluate_setup_h(
            "BTC/USDT", "bullish", state, candles,
        )
        assert result is None

    def test_sl_capped_at_max(self, evaluator):
        """SL distance capped at SETUP_H_MAX_SL_PCT."""
        # Create impulse with very wide move so OB would be far away
        candles = self._make_impulse_candles(
            direction="bullish", base_price=100.0,
            impulse_move=5.0, impulse_volume=30.0, normal_volume=10.0,
            count=30, impulse_count=5,
        )
        state = _make_structure_state("bos", "bullish")

        result = evaluator.evaluate_setup_h(
            "BTC/USDT", "bullish", state, candles,
        )
        if result is not None:
            sl_dist_pct = abs(result.entry_price - result.sl_price) / result.entry_price
            assert sl_dist_pct <= settings.SETUP_H_MAX_SL_PCT + 0.001

    def test_rejects_decelerating_impulse(self, evaluator):
        """Impulse where last 2 candles have tiny bodies vs first 3 → reject."""
        candles = self._make_impulse_candles(
            direction="bullish", base_price=100.0,
            impulse_move=0.5, impulse_volume=30.0, normal_volume=10.0,
        )
        # Override last 2 impulse candles: make bodies tiny (doji-like)
        n = settings.SETUP_H_MIN_IMPULSE_CANDLES
        for i in [-2, -1]:
            c = candles[i]
            mid = (c.open + c.close) / 2
            candles[i] = Candle(
                timestamp=c.timestamp, open=mid, high=c.high,
                low=c.low, close=mid + 0.001,  # Tiny body
                volume=c.volume, volume_quote=c.volume_quote,
                pair=c.pair, timeframe=c.timeframe, confirmed=True,
            )
        state = _make_structure_state("bos", "bullish")

        result = evaluator.evaluate_setup_h(
            "BTC/USDT", "bullish", state, candles,
        )
        assert result is None

    def test_rejects_extended_move(self, evaluator):
        """Impulse with >1.5% total move → reject as already extended."""
        candles = self._make_impulse_candles(
            direction="bullish", base_price=100.0,
            impulse_move=2.0,  # 2.0% move > 1.5% threshold
            impulse_volume=30.0, normal_volume=10.0,
        )
        state = _make_structure_state("bos", "bullish")

        result = evaluator.evaluate_setup_h(
            "BTC/USDT", "bullish", state, candles,
        )
        assert result is None

    def test_rejects_volume_decay(self, evaluator):
        """Impulse where volume drops off in last 2 candles → reject."""
        candles = self._make_impulse_candles(
            direction="bullish", base_price=100.0,
            impulse_move=0.5, impulse_volume=30.0, normal_volume=10.0,
        )
        # Override last 2 impulse candles: drop volume to < 50% of first 3
        for i in [-2, -1]:
            c = candles[i]
            candles[i] = Candle(
                timestamp=c.timestamp, open=c.open, high=c.high,
                low=c.low, close=c.close,
                volume=5.0,  # 5.0 < 50% of 30.0
                volume_quote=5.0 * c.close,
                pair=c.pair, timeframe=c.timeframe, confirmed=True,
            )
        state = _make_structure_state("bos", "bullish")

        result = evaluator.evaluate_setup_h(
            "BTC/USDT", "bullish", state, candles,
        )
        assert result is None

    def test_passes_fresh_impulse(self, evaluator):
        """Healthy impulse with consistent bodies and volume passes all filters."""
        candles = self._make_impulse_candles(
            direction="bullish", base_price=100.0,
            impulse_move=1.2,  # 1.2% — within 0.3%-1.5% window, SL > 0.8%
            impulse_volume=30.0, normal_volume=10.0,
        )
        state = _make_structure_state("bos", "bullish")

        result = evaluator.evaluate_setup_h(
            "BTC/USDT", "bullish", state, candles,
        )
        assert result is not None
        assert result.setup_type == "setup_h"
        assert result.direction == "long"

    def test_works_on_15m_candles(self, evaluator):
        """Setup H works on 15m timeframe too."""
        candles = self._make_impulse_candles(
            direction="bullish", timeframe="15m",
            impulse_move=1.2, impulse_volume=30.0, normal_volume=10.0,
        )
        state = _make_structure_state("bos", "bullish", timeframe="15m")

        result = evaluator.evaluate_setup_h(
            "BTC/USDT", "bullish", state, candles,
        )
        assert result is not None
        assert result.ob_timeframe == "15m"
