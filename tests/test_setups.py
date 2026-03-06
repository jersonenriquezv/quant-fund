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
    LiquiditySweep, LiquidityLevel, PremiumDiscountZone,
)
from shared.models import LiquidationEvent
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
    had_liquidations=True,
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
        had_liquidations=had_liquidations,
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
            liquidations=[
                LiquidationEvent(
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
        assert setup.entry_price == 101.0
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
        """Long in premium → no setup."""
        evaluator = SetupEvaluator()

        state = _make_structure_state(break_type="choch", break_direction="bullish")
        setup = evaluator.evaluate_setup_a(
            structure_state=state, active_obs=[_make_ob()],
            recent_sweeps=[_make_sweep()],
            pd_zone=_make_pd_zone("premium"),  # Wrong for long
            market_snapshot=None, candles=_make_candles_near_ob(),
            pair="BTC/USDT", htf_bias="bullish", liquidity_levels=[],
        )
        assert setup is None


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
        """TP1=1:1, TP2=1:2, TP3=1:3 (no liquidity levels)."""
        evaluator = SetupEvaluator()

        entry = 100.0
        sl = 97.0  # Risk = 3.0
        tp1, tp2, tp3 = evaluator._calculate_tp_levels(
            entry, sl, "bullish", [],
        )

        assert tp1 == pytest.approx(103.0, abs=0.01)   # 100 + 3*1.0
        assert tp2 == pytest.approx(106.0, abs=0.01)   # 100 + 3*2.0
        assert tp3 == pytest.approx(109.0, abs=0.01)   # 100 + 3*3.0 (fallback)

    def test_bearish_tp_levels(self):
        """Bearish TP levels go below entry."""
        evaluator = SetupEvaluator()

        entry = 100.0
        sl = 103.0  # Risk = 3.0
        tp1, tp2, tp3 = evaluator._calculate_tp_levels(
            entry, sl, "bearish", [],
        )

        assert tp1 == pytest.approx(97.0, abs=0.01)
        assert tp2 == pytest.approx(94.0, abs=0.01)
        assert tp3 == pytest.approx(91.0, abs=0.01)

    def test_tp3_uses_liquidity_level(self):
        """TP3 should target the next liquidity level if available."""
        evaluator = SetupEvaluator()

        entry = 100.0
        sl = 97.0  # Risk = 3.0

        # BSL at 108 (above TP2=106)
        levels = [
            LiquidityLevel(price=108.0, level_type="bsl",
                           touch_count=2, timestamps=[1000, 2000]),
        ]

        tp1, tp2, tp3 = evaluator._calculate_tp_levels(
            entry, sl, "bullish", levels,
        )

        assert tp3 == 108.0  # Uses the BSL level instead of 1:3


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

    def test_equilibrium_blocked(self):
        evaluator = SetupEvaluator()
        pd = _make_pd_zone("equilibrium")
        assert evaluator._check_pd_alignment(pd, "bullish") is False
        assert evaluator._check_pd_alignment(pd, "bearish") is False

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

class TestBlendedRR:
    """Test blended R:R calculation."""

    def test_standard_blended_rr(self):
        """Standard TP levels: blended = 0.5*1.0 + 0.3*2.0 + 0.2*3.0 = 1.7."""
        evaluator = SetupEvaluator()
        # Entry=100, SL=97, risk=3
        # TP1=103 (1:1), TP2=106 (2:1), TP3=109 (3:1)
        rr = evaluator._compute_blended_rr(100.0, 97.0, 103.0, 106.0, 109.0)
        assert rr == pytest.approx(1.7, abs=0.01)

    def test_tp3_liquidity_level_close_lowers_rr(self):
        """TP3 at a closer liquidity level lowers blended R:R."""
        evaluator = SetupEvaluator()
        # Entry=100, SL=97, risk=3
        # TP3 at 107 (2.33:1) instead of 109 (3:1)
        rr = evaluator._compute_blended_rr(100.0, 97.0, 103.0, 106.0, 107.0)
        # 0.5*1.0 + 0.3*2.0 + 0.2*2.33 = 0.5 + 0.6 + 0.467 = 1.567
        assert rr == pytest.approx(1.567, abs=0.01)

    def test_zero_risk_returns_zero(self):
        """Zero risk (entry == SL) should return 0.0."""
        evaluator = SetupEvaluator()
        rr = evaluator._compute_blended_rr(100.0, 100.0, 103.0, 106.0, 109.0)
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
        # OB_MAX_DISTANCE_PCT = 0.05 → at price 100, max distance = 5
        ob = _make_ob(entry_price=95.0)
        assert evaluator._is_ob_within_range(100.0, ob) is True

    def test_find_best_ob_selects_highest_volume(self):
        """_find_best_ob returns OB with highest volume ratio."""
        evaluator = SetupEvaluator()
        ob_low_vol = _make_ob(entry_price=99.0, volume_ratio=1.5, timestamp=9000)
        ob_high_vol = _make_ob(entry_price=98.0, volume_ratio=3.0, timestamp=8000)
        best = evaluator._find_best_ob([ob_low_vol, ob_high_vol], 100.0, "bullish")
        assert best is ob_high_vol

    def test_find_best_ob_tiebreak_by_recency(self):
        """_find_best_ob tiebreaks by most recent timestamp."""
        evaluator = SetupEvaluator()
        ob_old = _make_ob(entry_price=99.0, volume_ratio=2.0, timestamp=7000)
        ob_new = _make_ob(entry_price=98.0, volume_ratio=2.0, timestamp=9000)
        best = evaluator._find_best_ob([ob_old, ob_new], 100.0, "bullish")
        assert best is ob_new

    def test_setup_a_creates_without_proximity(self):
        """Setup A created when OB is within range but price not adjacent."""
        evaluator = SetupEvaluator()
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
            liquidations=[
                LiquidationEvent(
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
        assert setup.entry_price == 97.0

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
            liquidations=[
                LiquidationEvent(
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
            close_price=96.0, volume_ratio=2.5, had_liquidations=True,
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
            close_price=96.0, volume_ratio=2.5, had_liquidations=True,
        )

        obs = [_make_ob(direction="bullish", entry_price=101.0)]
        pd = _make_pd_zone("discount")
        candles = _make_candles_near_ob(101.0)
        snapshot = make_market_snapshot(
            cvd_15m=100.0,
            liquidations=[
                LiquidationEvent(
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
