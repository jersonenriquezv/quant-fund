"""Tests for strategy_service.setups — Setup A/B, confluence, TP calculation."""

import time
import pytest
from tests.conftest import make_candle, make_market_snapshot
from strategy_service.setups import SetupEvaluator
from strategy_service.market_structure import (
    MarketStructureState, StructureBreak, SwingPoint,
)
from strategy_service.order_blocks import OrderBlock
from strategy_service.fvg import FairValueGap
from strategy_service.liquidity import (
    LiquiditySweep, PremiumDiscountZone,
)
from shared.models import OIFlushEvent
from config.settings import settings


# ============================================================
# Fixtures / helpers
# ============================================================

def _make_structure_state(
    trend="bullish",
    break_type="choch",
    break_direction="bullish",
) -> MarketStructureState:
    """Create a MarketStructureState with a single break."""
    brk = StructureBreak(
        timestamp=10000,
        break_type=break_type,
        direction=break_direction,
        break_price=110.0,
        broken_level=108.0,
        candle_index=10,
    )
    return MarketStructureState(
        pair="BTC/USDT",
        timeframe="15m",
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
    brk = StructureBreak(
        timestamp=10000, break_type="bos", direction=direction,
        break_price=110.0, broken_level=108.0, candle_index=10,
    )
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


def _make_fvg(
    direction="bullish",
    high=103.0,
    low=100.5,
    timeframe="15m",
) -> FairValueGap:
    return FairValueGap(
        timestamp=7000,
        pair="BTC/USDT",
        timeframe=timeframe,
        direction=direction,
        high=high,
        low=low,
        size_pct=0.025,
        filled_pct=0.0,
        fully_filled=False,
    )


def _make_pd_zone(zone="discount") -> PremiumDiscountZone:
    return PremiumDiscountZone(
        pair="BTC/USDT",
        range_high=110.0,
        range_low=90.0,
        equilibrium=100.0,
        last_updated_ms=int(time.time() * 1000),
        zone=zone,
    )


def _make_candles_near_ob(entry_price=101.0) -> list:
    """Create candles where the last close is near the OB entry."""
    return [
        make_candle(close=entry_price, high=entry_price + 1,
                    low=entry_price - 1, timestamp=i * 1000)
        for i in range(20)
    ]


# ============================================================
# Setup A tests
# ============================================================

class TestSetupA:
    """Test Setup A — Liquidity Sweep + CHoCH + Order Block."""

    def test_valid_setup_a_long(self):
        """Full valid Setup A should produce a TradeSetup."""
        evaluator = SetupEvaluator()

        state = _make_structure_state(
            trend="bullish", break_type="choch", break_direction="bullish",
        )
        obs = [_make_ob(direction="bullish", entry_price=101.0)]
        sweeps = [_make_sweep(direction="bullish")]
        pd = _make_pd_zone("discount")
        snapshot = make_market_snapshot(
            cvd_15m=100.0,
            oi_flushes=[
                OIFlushEvent(
                    timestamp=9000, pair="BTC/USDT", side="long",
                    size_usd=50000, price=94.5, source="oi_proxy",
                ),
            ],
        )
        candles = _make_candles_near_ob(101.0)

        setup = evaluator.evaluate_setup_a(
            structure_state=state,
            active_obs=obs,
            recent_sweeps=sweeps,
            pd_zone=pd,
            market_snapshot=snapshot,
            candles=candles,
            pair="BTC/USDT",
            htf_bias="bullish",
            liquidity_levels=[],
        )

        assert setup is not None
        assert setup.setup_type == "setup_a"
        assert setup.direction == "long"
        # Entry at SETUP_A_ENTRY_PCT (0.65) of OB body: 100 + 0.65*(102-100) = 101.3
        assert setup.entry_price == 101.3
        assert setup.sl_price == 98.0  # OB low
        assert len(setup.confluences) >= 2

    def test_setup_a_no_htf_bias(self):
        """No HTF bias → no setup."""
        evaluator = SetupEvaluator()

        state = _make_structure_state(break_type="choch", break_direction="bullish")
        setup = evaluator.evaluate_setup_a(
            structure_state=state, active_obs=[_make_ob()],
            recent_sweeps=[_make_sweep()], pd_zone=_make_pd_zone("discount"),
            market_snapshot=None, candles=_make_candles_near_ob(),
            pair="BTC/USDT", htf_bias="undefined", liquidity_levels=[],
        )
        assert setup is None

    def test_setup_a_no_sweep(self):
        """No liquidity sweep → no Setup A."""
        evaluator = SetupEvaluator()

        state = _make_structure_state(break_type="choch", break_direction="bullish")
        setup = evaluator.evaluate_setup_a(
            structure_state=state, active_obs=[_make_ob()],
            recent_sweeps=[], pd_zone=_make_pd_zone("discount"),
            market_snapshot=None, candles=_make_candles_near_ob(),
            pair="BTC/USDT", htf_bias="bullish", liquidity_levels=[],
        )
        assert setup is None

    def test_setup_a_no_choch(self):
        """No CHoCH → no Setup A."""
        evaluator = SetupEvaluator()

        # Only BOS, no CHoCH
        state = _make_structure_state(break_type="bos", break_direction="bullish")
        setup = evaluator.evaluate_setup_a(
            structure_state=state, active_obs=[_make_ob()],
            recent_sweeps=[_make_sweep()], pd_zone=_make_pd_zone("discount"),
            market_snapshot=None, candles=_make_candles_near_ob(),
            pair="BTC/USDT", htf_bias="bullish", liquidity_levels=[],
        )
        assert setup is None

    def test_setup_a_wrong_pd_zone(self):
        """Long in premium → no setup (when confluences < override threshold)."""
        evaluator = SetupEvaluator()

        state = _make_structure_state(break_type="choch", break_direction="bullish")
        # Disable PD override and PD_AS_CONFLUENCE so PD misalignment blocks the trade
        original = settings.PD_OVERRIDE_MIN_CONFLUENCES
        original_pd = settings.PD_AS_CONFLUENCE
        settings.PD_OVERRIDE_MIN_CONFLUENCES = 0
        settings.PD_AS_CONFLUENCE = False
        try:
            setup = evaluator.evaluate_setup_a(
                structure_state=state, active_obs=[_make_ob()],
                recent_sweeps=[_make_sweep()],
                pd_zone=_make_pd_zone("premium"),  # Wrong for long
                market_snapshot=None, candles=_make_candles_near_ob(),
                pair="BTC/USDT", htf_bias="bullish", liquidity_levels=[],
            )
            assert setup is None
        finally:
            settings.PD_OVERRIDE_MIN_CONFLUENCES = original
            settings.PD_AS_CONFLUENCE = original_pd

    def test_setup_a_pd_override_with_high_confluence(self):
        """Long in premium allowed when confluences >= PD_OVERRIDE_MIN_CONFLUENCES."""
        evaluator = SetupEvaluator()

        state = _make_structure_state(break_type="choch", break_direction="bullish")
        original = settings.PD_OVERRIDE_MIN_CONFLUENCES
        settings.PD_OVERRIDE_MIN_CONFLUENCES = 5
        try:
            setup = evaluator.evaluate_setup_a(
                structure_state=state, active_obs=[_make_ob()],
                recent_sweeps=[_make_sweep()],
                pd_zone=_make_pd_zone("premium"),  # Wrong for long
                market_snapshot=None, candles=_make_candles_near_ob(),
                pair="BTC/USDT", htf_bias="bullish", liquidity_levels=[],
            )
            # With 7 confluences (sweep, choch, ob, pd, volume, sweep_vol, oi_flush),
            # the PD override kicks in and the setup is allowed
            assert setup is not None
            assert setup.direction == "long"
        finally:
            settings.PD_OVERRIDE_MIN_CONFLUENCES = original


# ============================================================
# Setup B tests
# ============================================================

class TestSetupB:
    """Test Setup B — BOS + FVG + Order Block."""

    def test_valid_setup_b_long(self):
        """Full valid Setup B should produce a TradeSetup."""
        evaluator = SetupEvaluator()

        state = _make_structure_state(
            trend="bullish", break_type="bos", break_direction="bullish",
        )
        # OB zone: 98-103, FVG zone: 100.5-103 → overlapping
        obs = [_make_ob(direction="bullish", entry_price=101.0,
                        high=103.0, low=98.0, body_high=102.0, body_low=100.0)]
        fvgs = [_make_fvg(direction="bullish", high=103.0, low=100.5)]
        pd = _make_pd_zone("discount")
        snapshot = make_market_snapshot(cvd_15m=100.0)
        candles = _make_candles_near_ob(101.0)

        setup = evaluator.evaluate_setup_b(
            structure_state=state,
            active_obs=obs,
            active_fvgs=fvgs,
            pd_zone=pd,
            market_snapshot=snapshot,
            candles=candles,
            pair="BTC/USDT",
            htf_bias="bullish",
            liquidity_levels=[],
        )

        assert setup is not None
        assert setup.setup_type == "setup_b"
        assert setup.direction == "long"
        assert len(setup.confluences) >= 2

    def test_setup_b_no_bos(self):
        """No BOS → no Setup B."""
        evaluator = SetupEvaluator()

        state = _make_structure_state(break_type="choch", break_direction="bullish")
        setup = evaluator.evaluate_setup_b(
            structure_state=state, active_obs=[_make_ob()],
            active_fvgs=[_make_fvg()], pd_zone=_make_pd_zone("discount"),
            market_snapshot=None, candles=_make_candles_near_ob(),
            pair="BTC/USDT", htf_bias="bullish", liquidity_levels=[],
        )
        assert setup is None

    def test_setup_b_no_fvg(self):
        """No FVG → no Setup B."""
        evaluator = SetupEvaluator()

        state = _make_structure_state(break_type="bos", break_direction="bullish")
        setup = evaluator.evaluate_setup_b(
            structure_state=state, active_obs=[_make_ob()],
            active_fvgs=[], pd_zone=_make_pd_zone("discount"),
            market_snapshot=None, candles=_make_candles_near_ob(),
            pair="BTC/USDT", htf_bias="bullish", liquidity_levels=[],
        )
        assert setup is None

    def test_setup_b_fvg_not_adjacent_to_ob(self):
        """FVG far from OB → no Setup B."""
        evaluator = SetupEvaluator()

        state = _make_structure_state(break_type="bos", break_direction="bullish")
        obs = [_make_ob(direction="bullish", high=103.0, low=98.0)]
        # FVG far away from OB
        fvgs = [_make_fvg(direction="bullish", high=120.0, low=118.0)]

        setup = evaluator.evaluate_setup_b(
            structure_state=state, active_obs=obs, active_fvgs=fvgs,
            pd_zone=_make_pd_zone("discount"), market_snapshot=None,
            candles=_make_candles_near_ob(), pair="BTC/USDT",
            htf_bias="bullish", liquidity_levels=[],
        )
        assert setup is None


# ============================================================
# SL Direction Validation tests
# ============================================================

class TestSLValidation:
    """Test SL direction check catches inverted SL."""

    def test_bearish_sl_must_be_above_entry(self):
        assert SetupEvaluator._validate_sl_direction(100.0, 105.0, "bearish") is True
        assert SetupEvaluator._validate_sl_direction(100.0, 95.0, "bearish") is False

    def test_bullish_sl_must_be_below_entry(self):
        assert SetupEvaluator._validate_sl_direction(100.0, 95.0, "bullish") is True
        assert SetupEvaluator._validate_sl_direction(100.0, 105.0, "bullish") is False

    def test_sl_equal_to_entry_rejected(self):
        assert SetupEvaluator._validate_sl_direction(100.0, 100.0, "bearish") is False
        assert SetupEvaluator._validate_sl_direction(100.0, 100.0, "bullish") is False


# ============================================================
# Confluence tests
# ============================================================

class TestConfluence:
    """Test minimum confluence requirement."""

    def test_minimum_2_confluences_required(self):
        """Setup must have at least 2 confluences (non-negotiable)."""
        evaluator = SetupEvaluator()
        assert evaluator._check_confluence_minimum(["one"]) is False
        assert evaluator._check_confluence_minimum(["one", "two"]) is True
        assert evaluator._check_confluence_minimum(["a", "b", "c"]) is True
        assert evaluator._check_confluence_minimum([]) is False


# ============================================================
# TP Calculation tests
# ============================================================

class TestTPCalculation:
    """Test take profit level calculation."""

    def test_bullish_tp_levels(self):
        """TP1=1:1, TP2=1:2 (no liquidity levels)."""
        evaluator = SetupEvaluator()

        entry = 100.0
        sl = 97.0  # Risk = 3.0
        tp1, tp2 = evaluator._calculate_tp_levels(
            entry, sl, "bullish", [],
        )

        assert tp1 == pytest.approx(103.0, abs=0.01)   # 100 + 3*1.0
        assert tp2 == pytest.approx(106.0, abs=0.01)   # 100 + 3*2.0

    def test_bearish_tp_levels(self):
        """Bearish TP levels go below entry."""
        evaluator = SetupEvaluator()

        entry = 100.0
        sl = 103.0  # Risk = 3.0
        tp1, tp2 = evaluator._calculate_tp_levels(
            entry, sl, "bearish", [],
        )

        assert tp1 == pytest.approx(97.0, abs=0.01)
        assert tp2 == pytest.approx(94.0, abs=0.01)


# ============================================================
# PD Alignment tests
# ============================================================

class TestPDAlignment:
    """Test premium/discount zone alignment."""

    def test_long_in_discount_allowed(self):
        evaluator = SetupEvaluator()
        pd = _make_pd_zone("discount")
        assert evaluator._check_pd_alignment(pd, "bullish") is True

    def test_long_in_premium_blocked(self):
        evaluator = SetupEvaluator()
        pd = _make_pd_zone("premium")
        assert evaluator._check_pd_alignment(pd, "bullish") is False

    def test_short_in_premium_allowed(self):
        evaluator = SetupEvaluator()
        pd = _make_pd_zone("premium")
        assert evaluator._check_pd_alignment(pd, "bearish") is True

    def test_short_in_discount_blocked(self):
        evaluator = SetupEvaluator()
        pd = _make_pd_zone("discount")
        assert evaluator._check_pd_alignment(pd, "bearish") is False

    def test_equilibrium_allowed(self):
        """Equilibrium trades are allowed (ALLOW_EQUILIBRIUM_TRADES=True)."""
        evaluator = SetupEvaluator()
        pd = _make_pd_zone("equilibrium")
        assert evaluator._check_pd_alignment(pd, "bullish") is True
        assert evaluator._check_pd_alignment(pd, "bearish") is True

    def test_no_pd_data_allowed(self):
        """Missing PD data should not block the trade."""
        evaluator = SetupEvaluator()
        assert evaluator._check_pd_alignment(None, "bullish") is True


# ============================================================
# FVG adjacency tests
# ============================================================

class TestFVGAdjacency:
    """Test FVG-OB adjacency check."""

    def test_overlapping_zones(self):
        evaluator = SetupEvaluator()
        fvg = _make_fvg(high=103.0, low=100.0)
        ob = _make_ob(high=102.0, low=99.0)
        assert evaluator._is_fvg_adjacent_to_ob(fvg, ob) is True

    def test_non_overlapping_far_zones(self):
        evaluator = SetupEvaluator()
        fvg = _make_fvg(high=120.0, low=118.0)
        ob = _make_ob(high=103.0, low=98.0)
        assert evaluator._is_fvg_adjacent_to_ob(fvg, ob) is False

    def test_adjacent_zones(self):
        """Zones within 0.1% of each other are considered adjacent."""
        evaluator = SetupEvaluator()
        # OB top at 103, FVG bottom at 103.05 → gap = 0.05/100.5 ≈ 0.05%
        fvg = _make_fvg(high=106.0, low=103.05)
        ob = _make_ob(high=103.0, low=98.0)
        assert evaluator._is_fvg_adjacent_to_ob(fvg, ob) is True


# ============================================================
# Blended R:R tests
# ============================================================

class TestRR:
    """Test simple R:R calculation to tp2."""

    def test_standard_rr(self):
        """Entry=100, SL=97, TP2=106 → R:R = 6/3 = 2.0."""
        evaluator = SetupEvaluator()
        rr = evaluator._compute_rr(100.0, 97.0, 106.0)
        assert rr == pytest.approx(2.0, abs=0.01)

    def test_short_rr(self):
        """Short: Entry=100, SL=103, TP2=94 → R:R = 6/3 = 2.0."""
        evaluator = SetupEvaluator()
        rr = evaluator._compute_rr(100.0, 103.0, 94.0)
        assert rr == pytest.approx(2.0, abs=0.01)

    def test_zero_risk_returns_zero(self):
        """Zero risk (entry == SL) should return 0.0."""
        evaluator = SetupEvaluator()
        rr = evaluator._compute_rr(100.0, 100.0, 106.0)
        assert rr == 0.0


# ============================================================
# OB Proximity tests (legacy — _is_price_near_ob still used by notifier)
# ============================================================

class TestOBProximity:
    """Test price-based OB proximity check."""

    def test_price_inside_ob_body(self):
        """Price inside OB body is always near."""
        evaluator = SetupEvaluator()
        ob = _make_ob(body_high=102.0, body_low=100.0)
        assert evaluator._is_price_near_ob(101.0, ob) is True

    def test_price_within_margin(self):
        """Price just outside body but within OB_PROXIMITY_PCT margin."""
        evaluator = SetupEvaluator()
        ob = _make_ob(body_high=102.0, body_low=100.0)
        # OB_PROXIMITY_PCT = 0.003, price=100 → margin = 0.3
        # extended_low = 100.0 - 0.3 = 99.7
        assert evaluator._is_price_near_ob(99.8, ob) is True

    def test_price_too_far(self):
        """Price outside margin should fail."""
        evaluator = SetupEvaluator()
        ob = _make_ob(body_high=102.0, body_low=100.0)
        # margin at price 95 = 95*0.003 = 0.285
        # extended_low = 100 - 0.285 = 99.715 → 95 < 99.715
        assert evaluator._is_price_near_ob(95.0, ob) is False


# ============================================================
# Zone-based OB selection tests
# ============================================================

class TestZoneBasedOB:
    """Test zone-based OB selection (no proximity requirement)."""

    def test_ob_within_max_distance(self):
        """OB within OB_MAX_DISTANCE_PCT is accepted."""
        evaluator = SetupEvaluator()
        ob = _make_ob(entry_price=99.0)  # 1% from price=100
        assert evaluator._is_ob_within_range(100.0, ob) is True

    def test_ob_beyond_max_distance(self):
        """OB beyond OB_MAX_DISTANCE_PCT is rejected."""
        evaluator = SetupEvaluator()
        ob = _make_ob(entry_price=90.0)  # 10% from price=100
        assert evaluator._is_ob_within_range(100.0, ob) is False

    def test_ob_at_exact_boundary(self):
        """OB exactly at OB_MAX_DISTANCE_PCT boundary is accepted."""
        evaluator = SetupEvaluator()
        original = settings.OB_MAX_DISTANCE_PCT
        settings.OB_MAX_DISTANCE_PCT = 0.04
        try:
            # OB_MAX_DISTANCE_PCT = 0.04 → at price 100, max distance = 4
            ob = _make_ob(entry_price=96.0)
            assert evaluator._is_ob_within_range(100.0, ob) is True
        finally:
            settings.OB_MAX_DISTANCE_PCT = original

    def test_find_best_ob_uses_composite_score(self):
        """_find_best_ob uses composite scoring (volume + freshness + proximity + size)."""
        evaluator = SetupEvaluator()
        now_ms = int(time.time() * 1000)
        # OB with high volume but far from price
        ob_far = _make_ob(entry_price=92.0, volume_ratio=3.0, timestamp=now_ms - 3600000,
                          body_high=93.0, body_low=91.0, high=94.0, low=90.0)
        # OB with moderate volume but close to price and fresh
        ob_close = _make_ob(entry_price=99.0, volume_ratio=2.0, timestamp=now_ms - 60000,
                            body_high=100.0, body_low=98.0, high=101.0, low=97.0)
        best = evaluator._find_best_ob([ob_far, ob_close], 100.0, "bullish")
        # Close + fresh OB should win despite lower volume
        assert best is ob_close

    def test_find_best_ob_rejects_micro_body(self):
        """OBs with body < OB_MIN_BODY_PCT are filtered out."""
        evaluator = SetupEvaluator()
        now_ms = int(time.time() * 1000)
        # Micro-OB: body = 0.01 / 100 = 0.01% (below 0.1% threshold)
        ob_micro = _make_ob(entry_price=100.005, volume_ratio=3.0, timestamp=now_ms,
                            body_high=100.01, body_low=100.0, high=100.5, low=99.5)
        best = evaluator._find_best_ob([ob_micro], 100.0, "bullish")
        assert best is None

    def test_setup_a_creates_without_proximity(self):
        """Setup A created when OB is within range but price not adjacent."""
        evaluator = SetupEvaluator()
        original = settings.OB_MAX_DISTANCE_PCT
        settings.OB_MAX_DISTANCE_PCT = 0.05
        state = _make_structure_state(
            trend="bullish", break_type="choch", break_direction="bullish",
        )
        # OB at 97 — price at 101 is 4% away (within 5% max distance)
        obs = [_make_ob(direction="bullish", entry_price=97.0,
                        body_high=98.0, body_low=96.0, high=99.0, low=95.0)]
        sweeps = [_make_sweep(direction="bullish")]
        pd = _make_pd_zone("discount")
        snapshot = make_market_snapshot(
            cvd_15m=100.0,
            oi_flushes=[
                OIFlushEvent(
                    timestamp=9000, pair="BTC/USDT", side="long",
                    size_usd=50000, price=94.5, source="oi_proxy",
                ),
            ],
        )
        candles = _make_candles_near_ob(101.0)

        setup = evaluator.evaluate_setup_a(
            structure_state=state, active_obs=obs,
            recent_sweeps=sweeps, pd_zone=pd,
            market_snapshot=snapshot, candles=candles,
            pair="BTC/USDT", htf_bias="bullish", liquidity_levels=[],
        )
        assert setup is not None
        # Entry at SETUP_A_ENTRY_PCT (0.65) of OB body: 96 + 0.65*(98-96) = 97.3
        assert setup.entry_price == 97.3
        settings.OB_MAX_DISTANCE_PCT = original

    def test_setup_a_rejects_ob_beyond_max_distance(self):
        """Setup A rejected when OB is beyond OB_MAX_DISTANCE_PCT."""
        evaluator = SetupEvaluator()
        state = _make_structure_state(
            trend="bullish", break_type="choch", break_direction="bullish",
        )
        # OB at 80 — price at 101 is ~21% away (way beyond 5%)
        obs = [_make_ob(direction="bullish", entry_price=80.0,
                        body_high=81.0, body_low=79.0, high=82.0, low=78.0)]
        sweeps = [_make_sweep(direction="bullish")]
        pd = _make_pd_zone("discount")
        candles = _make_candles_near_ob(101.0)

        setup = evaluator.evaluate_setup_a(
            structure_state=state, active_obs=obs,
            recent_sweeps=sweeps, pd_zone=pd,
            market_snapshot=None, candles=candles,
            pair="BTC/USDT", htf_bias="bullish", liquidity_levels=[],
        )
        assert setup is None


# ============================================================
# Bidirectional trading tests
# ============================================================

class TestBidirectionalTrading:
    """Test counter-trend setups (LTF opposes HTF)."""

    def test_counter_trend_setup_a_allowed(self):
        """Setup A with bullish CHoCH + bearish HTF should be created."""
        evaluator = SetupEvaluator()
        state = _make_structure_state(
            trend="bullish", break_type="choch", break_direction="bullish",
        )
        obs = [_make_ob(direction="bullish", entry_price=101.0)]
        sweeps = [_make_sweep(direction="bullish")]
        pd = _make_pd_zone("discount")
        snapshot = make_market_snapshot(
            cvd_15m=100.0,
            oi_flushes=[
                OIFlushEvent(
                    timestamp=9000, pair="BTC/USDT", side="long",
                    size_usd=50000, price=94.5, source="oi_proxy",
                ),
            ],
        )
        candles = _make_candles_near_ob(101.0)

        setup = evaluator.evaluate_setup_a(
            structure_state=state, active_obs=obs,
            recent_sweeps=sweeps, pd_zone=pd,
            market_snapshot=snapshot, candles=candles,
            pair="BTC/USDT", htf_bias="bearish",  # Counter-trend
            liquidity_levels=[],
        )
        assert setup is not None
        assert setup.direction == "long"
        assert setup.htf_bias == "bearish"

    def test_counter_trend_setup_b_allowed(self):
        """Setup B with bullish BOS + bearish HTF should be created."""
        evaluator = SetupEvaluator()
        state = _make_structure_state(
            trend="bullish", break_type="bos", break_direction="bullish",
        )
        obs = [_make_ob(direction="bullish", entry_price=101.0,
                        high=103.0, low=98.0, body_high=102.0, body_low=100.0)]
        fvgs = [_make_fvg(direction="bullish", high=103.0, low=100.5)]
        pd = _make_pd_zone("discount")
        snapshot = make_market_snapshot(cvd_15m=100.0)
        candles = _make_candles_near_ob(101.0)

        setup = evaluator.evaluate_setup_b(
            structure_state=state, active_obs=obs,
            active_fvgs=fvgs, pd_zone=pd,
            market_snapshot=snapshot, candles=candles,
            pair="BTC/USDT", htf_bias="bearish",  # Counter-trend
            liquidity_levels=[],
        )
        assert setup is not None
        assert setup.direction == "long"
        assert setup.htf_bias == "bearish"


# ============================================================
# Temporal ordering tests (Setup A)
# ============================================================

class TestTemporalOrdering:
    """Test that Setup A enforces sweep before CHoCH."""

    def test_sweep_after_choch_rejected(self):
        """Sweep timestamp > CHoCH timestamp → no Setup A."""
        evaluator = SetupEvaluator()

        # CHoCH at timestamp 5000, candle_index=5
        choch = StructureBreak(
            timestamp=5000, break_type="choch", direction="bullish",
            break_price=110.0, broken_level=108.0, candle_index=5,
        )
        state = MarketStructureState(
            pair="BTC/USDT", timeframe="15m", trend="bullish",
            swing_highs=[], swing_lows=[],
            structure_breaks=[choch], latest_break=choch,
        )

        # Sweep at timestamp 9000 (AFTER CHoCH) — should be rejected
        sweep = LiquiditySweep(
            timestamp=9000, pair="BTC/USDT", timeframe="15m",
            direction="bullish", swept_level=95.0, wick_price=94.0,
            close_price=96.0, volume_ratio=2.5, had_oi_flush=True,
        )

        obs = [_make_ob(direction="bullish", entry_price=101.0)]
        pd = _make_pd_zone("discount")
        candles = _make_candles_near_ob(101.0)
        snapshot = make_market_snapshot(cvd_15m=100.0)

        setup = evaluator.evaluate_setup_a(
            structure_state=state, active_obs=obs,
            recent_sweeps=[sweep], pd_zone=pd,
            market_snapshot=snapshot, candles=candles,
            pair="BTC/USDT", htf_bias="bullish", liquidity_levels=[],
        )
        assert setup is None

    def test_sweep_before_choch_accepted(self):
        """Sweep timestamp < CHoCH timestamp → valid for Setup A."""
        evaluator = SetupEvaluator()

        # CHoCH at timestamp 10000, candle_index=10
        choch = StructureBreak(
            timestamp=10000, break_type="choch", direction="bullish",
            break_price=110.0, broken_level=108.0, candle_index=10,
        )
        state = MarketStructureState(
            pair="BTC/USDT", timeframe="15m", trend="bullish",
            swing_highs=[], swing_lows=[],
            structure_breaks=[choch], latest_break=choch,
        )

        # Sweep at timestamp 8000 (BEFORE CHoCH)
        sweep = LiquiditySweep(
            timestamp=8000, pair="BTC/USDT", timeframe="15m",
            direction="bullish", swept_level=95.0, wick_price=94.0,
            close_price=96.0, volume_ratio=2.5, had_oi_flush=True,
        )

        obs = [_make_ob(direction="bullish", entry_price=101.0)]
        pd = _make_pd_zone("discount")
        candles = _make_candles_near_ob(101.0)
        snapshot = make_market_snapshot(
            cvd_15m=100.0,
            oi_flushes=[
                OIFlushEvent(
                    timestamp=8000, pair="BTC/USDT", side="long",
                    size_usd=50000, price=94.5, source="oi_proxy",
                ),
            ],
        )

        setup = evaluator.evaluate_setup_a(
            structure_state=state, active_obs=obs,
            recent_sweeps=[sweep], pd_zone=pd,
            market_snapshot=snapshot, candles=candles,
            pair="BTC/USDT", htf_bias="bullish", liquidity_levels=[],
        )
        assert setup is not None
        assert setup.setup_type == "setup_a"


# ============================================================
# Phase 2: PD_AS_CONFLUENCE tests
# ============================================================

class TestPDAsConfluence:
    """Test PD_AS_CONFLUENCE flag — PD zone as confluence instead of hard gate."""

    def test_pd_misaligned_blocks_by_default(self):
        """Default: PD misalignment blocks the trade."""
        evaluator = SetupEvaluator()
        state = _make_structure_state(break_type="choch", break_direction="bullish")
        original_override = settings.PD_OVERRIDE_MIN_CONFLUENCES
        original_pd = settings.PD_AS_CONFLUENCE
        settings.PD_OVERRIDE_MIN_CONFLUENCES = 0
        settings.PD_AS_CONFLUENCE = False
        try:
            setup = evaluator.evaluate_setup_a(
                structure_state=state, active_obs=[_make_ob()],
                recent_sweeps=[_make_sweep()],
                pd_zone=_make_pd_zone("premium"),
                market_snapshot=None, candles=_make_candles_near_ob(),
                pair="BTC/USDT", htf_bias="bullish", liquidity_levels=[],
            )
            assert setup is None
        finally:
            settings.PD_OVERRIDE_MIN_CONFLUENCES = original_override
            settings.PD_AS_CONFLUENCE = original_pd

    def test_pd_as_confluence_allows_misaligned(self):
        """PD_AS_CONFLUENCE=True: PD misalignment does NOT block trade."""
        evaluator = SetupEvaluator()
        state = _make_structure_state(break_type="choch", break_direction="bullish")
        original = settings.PD_AS_CONFLUENCE
        settings.PD_AS_CONFLUENCE = True
        try:
            setup = evaluator.evaluate_setup_a(
                structure_state=state, active_obs=[_make_ob()],
                recent_sweeps=[_make_sweep()],
                pd_zone=_make_pd_zone("premium"),
                market_snapshot=make_market_snapshot(
                    cvd_15m=100.0,
                    oi_flushes=[
                        OIFlushEvent(
                            timestamp=9000, pair="BTC/USDT", side="long",
                            size_usd=50000, price=94.5, source="oi_proxy",
                        ),
                    ],
                ),
                candles=_make_candles_near_ob(),
                pair="BTC/USDT", htf_bias="bullish", liquidity_levels=[],
            )
            assert setup is not None
            # PD zone should NOT be in confluences (misaligned)
            assert not any("pd_zone" in c for c in setup.confluences)
        finally:
            settings.PD_AS_CONFLUENCE = original

    def test_pd_as_confluence_adds_aligned_zone(self):
        """PD_AS_CONFLUENCE=True: aligned PD zone IS added as confluence."""
        evaluator = SetupEvaluator()
        state = _make_structure_state(break_type="choch", break_direction="bullish")
        original = settings.PD_AS_CONFLUENCE
        settings.PD_AS_CONFLUENCE = True
        try:
            setup = evaluator.evaluate_setup_a(
                structure_state=state, active_obs=[_make_ob()],
                recent_sweeps=[_make_sweep()],
                pd_zone=_make_pd_zone("discount"),
                market_snapshot=make_market_snapshot(
                    cvd_15m=100.0,
                    oi_flushes=[
                        OIFlushEvent(
                            timestamp=9000, pair="BTC/USDT", side="long",
                            size_usd=50000, price=94.5, source="oi_proxy",
                        ),
                    ],
                ),
                candles=_make_candles_near_ob(),
                pair="BTC/USDT", htf_bias="bullish", liquidity_levels=[],
            )
            assert setup is not None
            assert "pd_zone_discount" in setup.confluences
        finally:
            settings.PD_AS_CONFLUENCE = original


# ============================================================
# Phase 2: SETUP_A_MODE tests
# ============================================================

class TestSetupAMode:
    """Test SETUP_A_MODE — continuation, reversal, or both."""

    def _make_valid_setup_a_args(self, choch_direction, htf_bias):
        """Build args for evaluate_setup_a with given choch direction and htf_bias."""
        state = _make_structure_state(break_type="choch", break_direction=choch_direction)
        sweep_dir = choch_direction
        pd_zone_type = "discount" if choch_direction == "bullish" else "premium"
        return dict(
            structure_state=state,
            active_obs=[_make_ob(direction=choch_direction)],
            recent_sweeps=[_make_sweep(direction=sweep_dir)],
            pd_zone=_make_pd_zone(pd_zone_type),
            market_snapshot=make_market_snapshot(
                cvd_15m=100.0 if choch_direction == "bullish" else -100.0,
                oi_flushes=[
                    OIFlushEvent(
                        timestamp=9000, pair="BTC/USDT", side="long",
                        size_usd=50000, price=94.5, source="oi_proxy",
                    ),
                ],
            ),
            candles=_make_candles_near_ob(),
            pair="BTC/USDT",
            htf_bias=htf_bias,
            liquidity_levels=[],
        )

    def test_continuation_mode_allows_aligned(self):
        """continuation: CHoCH bullish + HTF bullish → allowed."""
        evaluator = SetupEvaluator()
        original = settings.SETUP_A_MODE
        settings.SETUP_A_MODE = "continuation"
        try:
            setup = evaluator.evaluate_setup_a(
                **self._make_valid_setup_a_args("bullish", "bullish")
            )
            assert setup is not None
        finally:
            settings.SETUP_A_MODE = original

    def test_continuation_mode_blocks_counter(self):
        """continuation: CHoCH bullish + HTF bearish → blocked."""
        evaluator = SetupEvaluator()
        original = settings.SETUP_A_MODE
        settings.SETUP_A_MODE = "continuation"
        try:
            setup = evaluator.evaluate_setup_a(
                **self._make_valid_setup_a_args("bullish", "bearish")
            )
            assert setup is None
        finally:
            settings.SETUP_A_MODE = original

    def test_reversal_mode_allows_counter(self):
        """reversal: CHoCH bullish + HTF bearish → allowed."""
        evaluator = SetupEvaluator()
        original = settings.SETUP_A_MODE
        settings.SETUP_A_MODE = "reversal"
        try:
            setup = evaluator.evaluate_setup_a(
                **self._make_valid_setup_a_args("bullish", "bearish")
            )
            assert setup is not None
        finally:
            settings.SETUP_A_MODE = original

    def test_reversal_mode_blocks_aligned(self):
        """reversal: CHoCH bullish + HTF bullish → blocked."""
        evaluator = SetupEvaluator()
        original = settings.SETUP_A_MODE
        settings.SETUP_A_MODE = "reversal"
        try:
            setup = evaluator.evaluate_setup_a(
                **self._make_valid_setup_a_args("bullish", "bullish")
            )
            assert setup is None
        finally:
            settings.SETUP_A_MODE = original

    def test_both_mode_allows_all(self):
        """both (default): allows aligned and counter-trend."""
        evaluator = SetupEvaluator()
        original = settings.SETUP_A_MODE
        settings.SETUP_A_MODE = "both"
        try:
            aligned = evaluator.evaluate_setup_a(
                **self._make_valid_setup_a_args("bullish", "bullish")
            )
            counter = evaluator.evaluate_setup_a(
                **self._make_valid_setup_a_args("bullish", "bearish")
            )
            assert aligned is not None
            assert counter is not None
        finally:
            settings.SETUP_A_MODE = original


# ============================================================
# Setup F Hardening tests
# ============================================================

def _make_setup_f_args(
    bos_candle_index=18,
    bos_direction="bullish",
    bos_break_price=110.0,
    bos_broken_level=108.0,
    ob_timestamp=None,
    ob_entry_price=101.0,
    ob_volume_ratio=2.0,
    pd_zone_type="discount",
    htf_bias="bullish",
    num_candles=20,
    candle_close=101.0,
    bos_timestamp=None,
):
    """Build args for evaluate_setup_f with sensible defaults for a valid Setup F."""
    now_ms = int(time.time() * 1000)
    # BOS timestamp: compute from candle_index and candle spacing (15m = 900s)
    if bos_timestamp is None:
        bos_timestamp = now_ms - (num_candles - 1 - bos_candle_index) * 900_000
    brk = StructureBreak(
        timestamp=bos_timestamp,
        break_type="bos",
        direction=bos_direction,
        break_price=bos_break_price,
        broken_level=bos_broken_level,
        candle_index=bos_candle_index,
    )
    state = MarketStructureState(
        pair="BTC/USDT", timeframe="15m", trend=bos_direction,
        swing_highs=[], swing_lows=[],
        structure_breaks=[brk], latest_break=brk,
    )
    # OB timestamp near the BOS by default
    if ob_timestamp is None:
        ob_timestamp = bos_timestamp - 2 * 900_000  # 2 candles before BOS
    ob_brk = StructureBreak(
        timestamp=ob_timestamp, break_type="bos", direction=bos_direction,
        break_price=bos_break_price, broken_level=bos_broken_level,
        candle_index=bos_candle_index,
    )
    ob = OrderBlock(
        timestamp=ob_timestamp, pair="BTC/USDT", timeframe="15m",
        direction=bos_direction, high=103.0, low=98.0,
        body_high=102.0, body_low=100.0, entry_price=ob_entry_price,
        volume=20.0, volume_ratio=ob_volume_ratio, mitigated=False,
        associated_break=ob_brk,
    )
    candles = [
        make_candle(
            close=candle_close, high=candle_close + 1,
            low=candle_close - 1,
            timestamp=now_ms - (num_candles - 1 - i) * 900_000,
        )
        for i in range(num_candles)
    ]
    pd = _make_pd_zone(pd_zone_type)
    return dict(
        structure_state=state,
        active_obs=[ob],
        pd_zone=pd,
        market_snapshot=None,
        candles=candles,
        pair="BTC/USDT",
        htf_bias=htf_bias,
        liquidity_levels=[],
    )


class TestMaxSLPct:
    """Test MAX_SL_PCT cap rejects setups with SL too far from entry."""

    def test_sl_within_max_passes(self):
        """SL distance 2% < MAX_SL_PCT (4%) should pass."""
        evaluator = SetupEvaluator()
        assert evaluator._check_sl_distance(100.0, 98.0, "BTC/USDT", "Test") is True

    def test_sl_exceeds_max_rejected(self):
        """SL distance 5% > MAX_SL_PCT (4%) should be rejected."""
        evaluator = SetupEvaluator()
        assert evaluator._check_sl_distance(100.0, 95.0, "BTC/USDT", "Test") is False

    def test_sl_at_exact_max_passes(self):
        """SL distance exactly 4% should pass (not strictly greater)."""
        evaluator = SetupEvaluator()
        assert evaluator._check_sl_distance(100.0, 96.0, "BTC/USDT", "Test") is True

    def test_sl_too_close_rejected(self):
        """SL distance 0.1% < MIN_RISK_DISTANCE_PCT (0.5%) should be rejected."""
        evaluator = SetupEvaluator()
        assert evaluator._check_sl_distance(100.0, 99.9, "BTC/USDT", "Test") is False


class TestSetupFHardening:
    """Test Setup F hardening filters."""

    def test_valid_setup_f_passes_all_filters(self):
        """A well-formed Setup F with fresh BOS, close OB, good score passes."""
        evaluator = SetupEvaluator()
        args = _make_setup_f_args()
        setup = evaluator.evaluate_setup_f(**args)
        assert setup is not None
        assert setup.setup_type == "setup_f"
        assert setup.direction == "long"

    def test_stale_bos_rejected(self):
        """BOS older than SETUP_F_MAX_BOS_AGE_CANDLES is rejected."""
        evaluator = SetupEvaluator()
        # BOS at candle_index=0, 19 candles ago in a 20-candle list → gap=19
        # With max=20, this would pass. Set max to 5 to force rejection.
        original = settings.SETUP_F_MAX_BOS_AGE_CANDLES
        settings.SETUP_F_MAX_BOS_AGE_CANDLES = 5
        try:
            args = _make_setup_f_args(bos_candle_index=0, num_candles=20)
            setup = evaluator.evaluate_setup_f(**args)
            assert setup is None
        finally:
            settings.SETUP_F_MAX_BOS_AGE_CANDLES = original

    def test_fresh_bos_accepted(self):
        """BOS within SETUP_F_MAX_BOS_AGE_CANDLES passes."""
        evaluator = SetupEvaluator()
        # BOS at candle_index=18, 1 candle ago → fresh
        args = _make_setup_f_args(bos_candle_index=18, num_candles=20)
        setup = evaluator.evaluate_setup_f(**args)
        assert setup is not None

    def test_bos_displacement_too_small_rejected(self):
        """BOS with displacement below SETUP_F_MIN_BOS_DISPLACEMENT_PCT is rejected."""
        evaluator = SetupEvaluator()
        # broken_level=108, break_price=108.1 → displacement=0.1/108=0.0009 < 0.002
        args = _make_setup_f_args(bos_break_price=108.1, bos_broken_level=108.0)
        setup = evaluator.evaluate_setup_f(**args)
        assert setup is None

    def test_bos_displacement_sufficient_accepted(self):
        """BOS with adequate displacement passes."""
        evaluator = SetupEvaluator()
        # broken_level=108, break_price=110 → displacement=2/108=0.0185 > 0.002
        args = _make_setup_f_args(bos_break_price=110.0, bos_broken_level=108.0)
        setup = evaluator.evaluate_setup_f(**args)
        assert setup is not None

    def test_ob_too_far_from_bos_rejected(self):
        """OB timestamped far from BOS is filtered out."""
        evaluator = SetupEvaluator()
        original = settings.SETUP_F_MAX_OB_BOS_GAP_CANDLES
        settings.SETUP_F_MAX_OB_BOS_GAP_CANDLES = 10  # Explicit for test
        try:
            now_ms = int(time.time() * 1000)
            bos_ts = now_ms - 1 * 900_000  # BOS 1 candle ago
            # OB 20 candles before BOS (>10 candle gap)
            ob_ts = bos_ts - 20 * 900_000
            args = _make_setup_f_args(
                bos_candle_index=18, bos_timestamp=bos_ts, ob_timestamp=ob_ts,
            )
            setup = evaluator.evaluate_setup_f(**args)
            assert setup is None
        finally:
            settings.SETUP_F_MAX_OB_BOS_GAP_CANDLES = original

    def test_ob_near_bos_accepted(self):
        """OB within SETUP_F_MAX_OB_BOS_GAP_CANDLES of BOS passes."""
        evaluator = SetupEvaluator()
        now_ms = int(time.time() * 1000)
        bos_ts = now_ms - 1 * 900_000
        ob_ts = bos_ts - 3 * 900_000  # 3 candles before BOS (within 10)
        args = _make_setup_f_args(
            bos_candle_index=18, bos_timestamp=bos_ts, ob_timestamp=ob_ts,
        )
        setup = evaluator.evaluate_setup_f(**args)
        assert setup is not None

    def test_low_ob_score_rejected(self):
        """OB with composite score below SETUP_F_MIN_OB_SCORE is rejected."""
        evaluator = SetupEvaluator()
        original = settings.SETUP_F_MIN_OB_SCORE
        settings.SETUP_F_MIN_OB_SCORE = 0.99  # Impossibly high
        try:
            args = _make_setup_f_args()
            setup = evaluator.evaluate_setup_f(**args)
            assert setup is None
        finally:
            settings.SETUP_F_MIN_OB_SCORE = original

    def test_cvd_in_confluences(self):
        """CVD should appear in Setup F confluences (audit 03-18: enriched signals)."""
        evaluator = SetupEvaluator()
        args = _make_setup_f_args()
        args["market_snapshot"] = make_market_snapshot(cvd_15m=500.0)
        setup = evaluator.evaluate_setup_f(**args)
        assert setup is not None
        assert any("cvd" in c for c in setup.confluences)

    def test_funding_in_confluences(self):
        """Funding should appear in Setup F confluences (audit 03-18: enriched signals)."""
        evaluator = SetupEvaluator()
        args = _make_setup_f_args()
        args["market_snapshot"] = make_market_snapshot(funding_rate=-0.001)
        setup = evaluator.evaluate_setup_f(**args)
        assert setup is not None
        assert any("funding" in c for c in setup.confluences)

    def test_entry_too_far_rejected(self):
        """Entry >SETUP_F_MAX_ENTRY_DISTANCE_PCT from current price is rejected."""
        evaluator = SetupEvaluator()
        original = settings.SETUP_F_MAX_ENTRY_DISTANCE_PCT
        settings.SETUP_F_MAX_ENTRY_DISTANCE_PCT = 0.001  # 0.1% — very tight
        try:
            # OB entry at 101, candle close at 101 → 0% distance → passes
            # OB entry at 95, candle close at 101 → ~6% → rejected
            args = _make_setup_f_args(ob_entry_price=95.0, candle_close=101.0)
            setup = evaluator.evaluate_setup_f(**args)
            assert setup is None
        finally:
            settings.SETUP_F_MAX_ENTRY_DISTANCE_PCT = original

    def test_min_3_confluences_required(self):
        """Setup F requires SETUP_F_MIN_CONFLUENCES (3) — BOS + OB alone not enough."""
        evaluator = SetupEvaluator()
        # Use PD_AS_CONFLUENCE=True so PD is only added when aligned, not always
        original_pd = settings.PD_AS_CONFLUENCE
        original_min = settings.SETUP_F_MIN_CONFLUENCES
        settings.PD_AS_CONFLUENCE = True
        settings.SETUP_F_MIN_CONFLUENCES = 3
        try:
            # pd_zone=premium for bullish direction → PD misaligned → no PD confluence
            # volume_ratio=0.5 → below OB_MIN_VOLUME_RATIO (1.2) → no volume confluence
            # Only BOS + OB = 2 confluences < 3
            args = _make_setup_f_args(
                pd_zone_type="premium", ob_volume_ratio=0.5,
            )
            setup = evaluator.evaluate_setup_f(**args)
            assert setup is None
        finally:
            settings.PD_AS_CONFLUENCE = original_pd
            settings.SETUP_F_MIN_CONFLUENCES = original_min

    def test_3_confluences_with_pd_passes(self):
        """BOS + OB + PD aligned = 3 confluences → passes."""
        evaluator = SetupEvaluator()
        args = _make_setup_f_args(
            pd_zone_type="discount", ob_volume_ratio=0.5,
        )
        setup = evaluator.evaluate_setup_f(**args)
        assert setup is not None
        assert len(setup.confluences) >= 3


# ============================================================
# Setup B Hardening tests (BOS age + entry distance + direction fix)
# ============================================================

def _make_setup_b_args(
    bos_candle_index=18,
    bos_direction="bullish",
    num_candles=20,
    candle_close=101.0,
    ob_entry_price=101.0,
    ob_volume_ratio=2.0,
    fvg_high=103.0,
    fvg_low=100.5,
    pd_zone_type="discount",
    htf_bias="bullish",
):
    """Build args for evaluate_setup_b with sensible defaults for a valid Setup B."""
    now_ms = int(time.time() * 1000)
    bos_timestamp = now_ms - (num_candles - 1 - bos_candle_index) * 900_000
    brk = StructureBreak(
        timestamp=bos_timestamp,
        break_type="bos",
        direction=bos_direction,
        break_price=110.0,
        broken_level=108.0,
        candle_index=bos_candle_index,
    )
    state = MarketStructureState(
        pair="BTC/USDT", timeframe="15m", trend=bos_direction,
        swing_highs=[], swing_lows=[],
        structure_breaks=[brk], latest_break=brk,
    )
    ob_brk = StructureBreak(
        timestamp=bos_timestamp, break_type="bos", direction=bos_direction,
        break_price=110.0, broken_level=108.0, candle_index=bos_candle_index,
    )
    ob = OrderBlock(
        timestamp=now_ms - 2 * 900_000, pair="BTC/USDT", timeframe="15m",
        direction=bos_direction, high=103.0, low=98.0,
        body_high=102.0, body_low=100.0, entry_price=ob_entry_price,
        volume=20.0, volume_ratio=ob_volume_ratio, mitigated=False,
        associated_break=ob_brk,
    )
    fvg = FairValueGap(
        timestamp=now_ms - 2 * 900_000, pair="BTC/USDT", timeframe="15m",
        direction=bos_direction, high=fvg_high, low=fvg_low,
        size_pct=0.025, filled_pct=0.0, fully_filled=False,
    )
    candles = [
        make_candle(
            close=candle_close, high=candle_close + 1,
            low=candle_close - 1,
            timestamp=now_ms - (num_candles - 1 - i) * 900_000,
        )
        for i in range(num_candles)
    ]
    pd = _make_pd_zone(pd_zone_type)
    snapshot = make_market_snapshot(cvd_15m=100.0)
    return dict(
        structure_state=state,
        active_obs=[ob],
        active_fvgs=[fvg],
        pd_zone=pd,
        market_snapshot=snapshot,
        candles=candles,
        pair="BTC/USDT",
        htf_bias=htf_bias,
        liquidity_levels=[],
    )


class TestSetupBHardening:
    """Test Setup B freshness filters (BOS age + entry distance) and direction fix."""

    def test_valid_setup_b_passes_all_filters(self):
        """A well-formed Setup B with fresh BOS + close entry passes."""
        evaluator = SetupEvaluator()
        args = _make_setup_b_args()
        setup = evaluator.evaluate_setup_b(**args)
        assert setup is not None
        assert setup.setup_type == "setup_b"

    def test_stale_bos_rejected(self):
        """BOS older than SETUP_B_MAX_BOS_AGE_CANDLES is rejected."""
        evaluator = SetupEvaluator()
        original = settings.SETUP_B_MAX_BOS_AGE_CANDLES
        settings.SETUP_B_MAX_BOS_AGE_CANDLES = 5
        try:
            # BOS at candle 0 with 20 candles → age = 19 > 5
            args = _make_setup_b_args(bos_candle_index=0, num_candles=20)
            setup = evaluator.evaluate_setup_b(**args)
            assert setup is None
        finally:
            settings.SETUP_B_MAX_BOS_AGE_CANDLES = original

    def test_fresh_bos_accepted(self):
        """BOS within SETUP_B_MAX_BOS_AGE_CANDLES passes."""
        evaluator = SetupEvaluator()
        # BOS at candle 18 with 20 candles → age = 1 < 12
        args = _make_setup_b_args(bos_candle_index=18, num_candles=20)
        setup = evaluator.evaluate_setup_b(**args)
        assert setup is not None

    def test_entry_too_far_rejected(self):
        """Entry > SETUP_B_MAX_ENTRY_DISTANCE_PCT from current price is rejected."""
        evaluator = SetupEvaluator()
        original = settings.SETUP_B_MAX_ENTRY_DISTANCE_PCT
        settings.SETUP_B_MAX_ENTRY_DISTANCE_PCT = 0.001  # 0.1% — very tight
        try:
            # candle_close=95 → entry ~102 (bullish FVG) → ~7% distance → rejected
            args = _make_setup_b_args(candle_close=95.0)
            setup = evaluator.evaluate_setup_b(**args)
            assert setup is None
        finally:
            settings.SETUP_B_MAX_ENTRY_DISTANCE_PCT = original

    def test_entry_close_accepted(self):
        """Entry within SETUP_B_MAX_ENTRY_DISTANCE_PCT passes."""
        evaluator = SetupEvaluator()
        # candle_close=101 → entry ~102.375 (bullish FVG) → ~1.4% < 2%
        args = _make_setup_b_args(candle_close=101.0)
        setup = evaluator.evaluate_setup_b(**args)
        assert setup is not None

    def test_direction_bug_fixed_bullish(self):
        """Bullish entry uses fvg.low + range * pct (not fvg.high - range * pct)."""
        evaluator = SetupEvaluator()
        # FVG: high=103.0, low=100.5, range=2.5, FVG_ENTRY_PCT=0.75
        # Correct (bullish): 100.5 + 2.5 * 0.75 = 102.375
        # Buggy (old):       103.0 - 2.5 * 0.75 = 101.125
        args = _make_setup_b_args(bos_direction="bullish", fvg_high=103.0, fvg_low=100.5)
        setup = evaluator.evaluate_setup_b(**args)
        assert setup is not None
        expected = 100.5 + 2.5 * settings.FVG_ENTRY_PCT
        assert abs(setup.entry_price - expected) < 0.01, (
            f"Expected entry ~{expected:.2f} (bullish), got {setup.entry_price:.2f}"
        )
