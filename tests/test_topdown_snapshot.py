"""Unit + golden-file tests for /topdown ICT enhancements (Phase 1).

Covers:
- Bug fix: _play_idea target distance 1.5R floor (SOL incident 2026-05-22)
- _displacement_read (ICT Displacement Candle)
- _pd_array_position (ICT PD Array wrapper)
- _inducement_check (ICT IDM)
- _killzone_now (ICT Killzones exact windows)
- _render_telegram_markdown golden-file (all 6 section headers present)

No DB access — all synthetic Snapshot fixtures.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from shared.models import Candle
from strategy_service.market_structure import (
    MarketStructureState, SwingPoint, StructureBreak,
)
from strategy_service.order_blocks import OrderBlock
from strategy_service.fvg import FairValueGap
from strategy_service.liquidity import LiquidityLevel

from scripts.topdown_snapshot import (
    Snapshot, TFAnalysis,
    _displacement_read, _pd_array_position, _inducement_check,
    _killzone_now, _min_target_distance, _pick_valid_target,
    _play_idea, _render_telegram_markdown,
    _has_required_telegram_sections,
    _pd_bias_conflict, _sweep_distance_pct, _sweep_actionable,
    _trade_triplet, _bos_session_quality,
    _compute_pdh_pdl, _compute_pwh_pwl, _daily_bias_chain, _today_candle_status,
    _daily_atr, _adaptive_tp,
    _trend_duration, _ltf_flip_vs_htf, _last_candle_impulse, _wick_into_liquidity,
    DISPLACEMENT_LOOKBACK_N, DISPLACEMENT_BASELINE_N,
    TARGET_MIN_R_MULTIPLE, ICT_KILLZONES, SWEEP_MAX_ACTIONABLE_PCT,
    DAILY_DOJI_TOLERANCE, DAILY_CHAIN_N,
    ATR_PERIOD, ADAPTIVE_TP_ATR_MULTIPLE,
    LTF_FLIP_MAX_CANDLES, IMPULSE_BIG_RATIO, IMPULSE_EXTREME_RATIO,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_candle(open_, high, low, close, ts=0, vol=10.0, pair="SOL/USDT", tf="4h"):
    return Candle(
        timestamp=ts, open=open_, high=high, low=low, close=close,
        volume=vol, volume_quote=vol * close, pair=pair, timeframe=tf,
        confirmed=True,
    )


def _make_bear_displacement_candles(n_recent=3, n_baseline=30):
    """Construct candles: small baseline + strong bear recent."""
    candles = []
    # Baseline: tiny bodies (0.1% range) around 100
    for i in range(n_baseline):
        c = _make_candle(100.0, 100.1, 99.9, 100.05, ts=i * 1000)
        candles.append(c)
    # Recent: large bear bodies (3% range, close near low)
    for i in range(n_recent):
        c = _make_candle(
            100.0, 100.2, 97.0, 97.2,
            ts=(n_baseline + i) * 1000,
        )
        candles.append(c)
    return candles


def _make_state(trend="bearish", swing_highs=None, swing_lows=None,
                latest_break=None, structure_breaks=None):
    return MarketStructureState(
        pair="SOL/USDT", timeframe="4h", trend=trend,
        swing_highs=swing_highs or [],
        swing_lows=swing_lows or [],
        structure_breaks=structure_breaks or [],
        latest_break=latest_break,
    )


def _make_snapshot(tf_results=None, current_price=84.12, side="short",
                   conf="medium", invalidation=85.78, raw_candles=None):
    tf_results = tf_results or {}
    return Snapshot(
        pair="SOL/USDT",
        current_price=current_price,
        current_time_ms=int(time.time() * 1000),
        tf_results=tf_results,
        vp=None,
        reconciled_side=side,
        confidence=conf,
        invalidation_level=invalidation,
        invalidation_reason="4H close above last swing high",
        raw_candles=raw_candles or {},
    )


# ---------------------------------------------------------------------------
# Bug fix: target distance 1.5R floor
# ---------------------------------------------------------------------------

class TestPlayIdeaTargetDistance:
    """SOL incident 2026-05-22: target 84.123 vs sweep entry 84.12 = noise."""

    def test_min_target_distance_basic(self):
        # Sweep at 84.82, invalidation at 84.82 → distance 0 → floor 0
        assert _min_target_distance(84.82, 84.82) == 0
        # Sweep at 84.82, invalidation at 85.78 → distance 0.96 × 1.5 = 1.44
        result = _min_target_distance(84.82, 85.78)
        assert abs(result - 1.44) < 0.001

    def test_min_target_distance_none_inputs(self):
        assert _min_target_distance(None, 85.0) is None
        assert _min_target_distance(85.0, None) is None
        assert _min_target_distance(None, None) is None

    def test_pick_valid_target_rejects_too_close(self):
        # Sweep at 84.82, floor 1.44 from sweep — target at 84.65 is 0.17 away,
        # rejected. Target at 82.10 is 2.72 away, accepted.
        candidates = [
            type("L", (), {"price": 84.65})(),  # too close
            type("L", (), {"price": 82.10})(),  # far enough
        ]
        target = _pick_valid_target(candidates, "short", 84.82, 1.44)
        assert target.price == 82.10

    def test_pick_valid_target_returns_none_if_all_too_close(self):
        candidates = [
            type("L", (), {"price": 84.65})(),
            type("L", (), {"price": 84.50})(),
        ]
        target = _pick_valid_target(candidates, "short", 84.82, 1.44)
        assert target is None

    def test_pick_valid_target_fallback_when_no_min_distance(self):
        candidates = [type("L", (), {"price": 84.65})()]
        # min_distance None → return nearest (legacy behavior)
        target = _pick_valid_target(candidates, "short", 84.82, None)
        assert target.price == 84.65

    def test_pick_valid_target_empty_candidates(self):
        assert _pick_valid_target([], "short", 84.82, 1.44) is None

    def test_play_idea_skips_noise_target_sol_incident(self):
        """Reproduce SOL pattern: sweep above, invalidation tight, noise target below."""
        # Price 84.50, BSL above (84.82 = sweep entry, invalidation 85.78),
        # noise SSL just below (84.40 — only 0.42 from sweep), real SSL far (82.10).
        # Floor = (84.82 - 85.78) * 1.5 = 1.44 → noise rejected, real accepted.
        liq = [
            LiquidityLevel(price=84.82, level_type="bsl", touch_count=3,
                           timestamps=[1000], swept=False),
            LiquidityLevel(price=84.40, level_type="ssl", touch_count=2,
                           timestamps=[1100], swept=False),  # noise
            LiquidityLevel(price=82.10, level_type="ssl", touch_count=2,
                           timestamps=[1200], swept=False),  # real
        ]
        tfa = TFAnalysis(
            timeframe="4h", state=_make_state(trend="bearish"),
            obs=[], fvgs=[], liquidity=liq,
        )
        snap = _make_snapshot(
            tf_results={"4h": tfa},
            current_price=84.50, side="short", conf="medium",
            invalidation=85.78,
        )
        play_lines = _play_idea(snap)
        body = "\n".join(play_lines)
        # Target line must NOT be 84.40 (noise — too close to sweep)
        target_lines = [l for l in play_lines if "Target" in l]
        for tl in target_lines:
            assert "84.4" not in tl, f"Noise target leaked into Target line: {tl}"
        # Must propose 82.10 as the actual target
        assert "82.1" in body


# ---------------------------------------------------------------------------
# ICT Displacement Candle
# ---------------------------------------------------------------------------

class TestDisplacementRead:
    def test_strong_bear_displacement(self):
        candles = _make_bear_displacement_candles()
        d = _displacement_read(candles)
        assert d["strength"] == "strong"
        assert d["direction"] == "bear"
        assert d["direction_consistent"] is True
        assert d["body_ratio"] >= 2.0
        assert d["close_to_extreme_pct"] >= 0.80

    def test_unknown_when_insufficient_candles(self):
        d = _displacement_read([])
        assert d["strength"] == "unknown"
        d2 = _displacement_read([_make_candle(100, 101, 99, 100)] * 5)
        assert d2["strength"] == "unknown"

    def test_weak_when_mixed_direction(self):
        # 3 recent: bull, bear, bull → not consistent
        candles = []
        for i in range(DISPLACEMENT_BASELINE_N):
            candles.append(_make_candle(100.0, 100.1, 99.9, 100.05, ts=i))
        candles.append(_make_candle(100.0, 101.0, 99.5, 100.8, ts=100))
        candles.append(_make_candle(100.8, 101.0, 99.0, 99.5, ts=101))
        candles.append(_make_candle(99.5, 100.5, 99.0, 100.2, ts=102))
        d = _displacement_read(candles)
        assert d["strength"] == "weak"
        assert d["direction_consistent"] is False


# ---------------------------------------------------------------------------
# ICT PD Array wrapper
# ---------------------------------------------------------------------------

class TestPDArrayPosition:
    def test_premium_zone_with_explicit_swings(self):
        # Range from 80 → 90, price at 88 → 80% premium
        swing_highs = [SwingPoint(timestamp=1000, price=90.0, index=10, swing_type="high")]
        swing_lows = [SwingPoint(timestamp=900, price=80.0, index=5, swing_type="low")]
        state = _make_state(swing_highs=swing_highs, swing_lows=swing_lows)
        result = _pd_array_position(
            htf_candles=[_make_candle(85.0, 90.0, 80.0, 88.0, ts=i*1000) for i in range(20)],
            htf_state=state,
            pair="SOL/USDT", current_price=88.0,
            current_time_ms=2_000_000,
        )
        assert result is not None
        assert result["position_pct"] == pytest.approx(80.0, abs=1.0)
        assert result["zone"] == "premium"

    def test_discount_zone(self):
        swing_highs = [SwingPoint(timestamp=1000, price=90.0, index=10, swing_type="high")]
        swing_lows = [SwingPoint(timestamp=900, price=80.0, index=5, swing_type="low")]
        state = _make_state(swing_highs=swing_highs, swing_lows=swing_lows)
        result = _pd_array_position(
            htf_candles=[_make_candle(82.0, 90.0, 80.0, 81.0, ts=i*1000) for i in range(20)],
            htf_state=state,
            pair="SOL/USDT", current_price=81.0,
            current_time_ms=2_000_000,
        )
        assert result["zone"] == "discount"

    def test_none_when_range_invalid(self):
        # Empty swings + insufficient candles → fallback fails, returns None
        state = _make_state(swing_highs=[], swing_lows=[])
        result = _pd_array_position(
            htf_candles=[_make_candle(85.0, 90.0, 80.0, 88.0)],
            htf_state=state, pair="SOL/USDT",
            current_price=88.0, current_time_ms=1_000_000,
        )
        assert result is None


# ---------------------------------------------------------------------------
# ICT IDM (inducement)
# ---------------------------------------------------------------------------

class TestInducementCheck:
    def test_idm_detected_bearish_bos(self):
        # BOS bearish at ts=2000. BSL swept at ts=1500 (before BOS) = IDM.
        latest_break = StructureBreak(
            timestamp=2000, break_type="bos", direction="bearish",
            break_price=99.0, broken_level=99.5, candle_index=10,
        )
        liquidity = [
            LiquidityLevel(price=101.5, level_type="bsl", touch_count=2,
                           timestamps=[1500], swept=True),  # IDM candidate
        ]
        tfa = TFAnalysis(
            timeframe="4h",
            state=_make_state(latest_break=latest_break),
            obs=[], fvgs=[], liquidity=liquidity,
        )
        result = _inducement_check(tfa)
        assert result["has_idm"] is True
        assert result["idm_level"] == 101.5

    def test_no_idm_when_no_swept_opposite_liquidity(self):
        latest_break = StructureBreak(
            timestamp=2000, break_type="bos", direction="bearish",
            break_price=99.0, broken_level=99.5, candle_index=10,
        )
        tfa = TFAnalysis(
            timeframe="4h",
            state=_make_state(latest_break=latest_break),
            obs=[], fvgs=[], liquidity=[],
        )
        result = _inducement_check(tfa)
        assert result["has_idm"] is False
        assert result["idm_level"] is None

    def test_no_idm_when_swept_after_bos(self):
        latest_break = StructureBreak(
            timestamp=2000, break_type="bos", direction="bearish",
            break_price=99.0, broken_level=99.5, candle_index=10,
        )
        # Swept at ts=2500 (AFTER BOS) — not IDM
        liquidity = [
            LiquidityLevel(price=101.5, level_type="bsl", touch_count=2,
                           timestamps=[2500], swept=True),
        ]
        tfa = TFAnalysis(
            timeframe="4h",
            state=_make_state(latest_break=latest_break),
            obs=[], fvgs=[], liquidity=liquidity,
        )
        assert _inducement_check(tfa)["has_idm"] is False

    def test_no_idm_when_no_latest_break(self):
        tfa = TFAnalysis(
            timeframe="4h", state=_make_state(latest_break=None),
            obs=[], fvgs=[], liquidity=[],
        )
        assert _inducement_check(tfa)["has_idm"] is False


# ---------------------------------------------------------------------------
# ICT Killzones — exact windows
# ---------------------------------------------------------------------------

class TestKillzoneNow:
    def _ts_at_hour(self, hour: int, minute: int = 0) -> int:
        """Build a timestamp ms aligned to hour:minute UTC on a known day."""
        # 2026-05-23 is a Saturday — pick that day's 00:00 UTC as anchor.
        # UTC seconds for 2026-05-23 00:00 = compute via mktime equivalent:
        # 1779747200 is 2026-05-23 00:00:00 UTC (approx — verify).
        # Use direct calculation: days since epoch = (2026-1970)*365 + leaps
        # Easier: use a known epoch-aligned hour:minute on day 0
        return ((hour * 3600) + (minute * 60)) * 1000

    @pytest.mark.parametrize("hour,expected", [
        (20, "Asian"), (22, "Asian"), (23, "Asian"),
        (2, "London"), (4, "London"),
        (12, "NY AM"), (14, "NY AM"),
        (18, "NY PM"), (19, "NY PM"),
    ])
    def test_active_inside_window(self, hour, expected):
        ts = self._ts_at_hour(hour, 30)
        kz = _killzone_now(ts)
        assert kz["active"] is True
        assert kz["name"] == expected

    @pytest.mark.parametrize("hour", [0, 1, 5, 6, 7, 10, 15, 17])
    def test_inactive_between_windows(self, hour):
        ts = self._ts_at_hour(hour, 0)
        kz = _killzone_now(ts)
        assert kz["active"] is False
        assert kz["name"] is None
        assert kz["next_name"] is not None
        assert kz["minutes_to_next"] > 0

    def test_next_killzone_is_nearest_upcoming(self):
        # At hour 6:00, nearest is NY AM at 12:00 (6h away). NOT London (next day).
        ts = self._ts_at_hour(6, 0)
        kz = _killzone_now(ts)
        assert kz["next_name"] == "NY AM"
        assert kz["minutes_to_next"] == 360  # 6h


# ---------------------------------------------------------------------------
# Telegram Markdown renderer — golden-file check
# ---------------------------------------------------------------------------

class TestTelegramRenderer:
    def _build_minimal_snap(self):
        """Smallest viable snap that exercises every section."""
        latest_break = StructureBreak(
            timestamp=1_000_000, break_type="bos", direction="bearish",
            break_price=84.0, broken_level=84.5, candle_index=10,
        )
        liq_4h = [
            LiquidityLevel(price=85.50, level_type="bsl", touch_count=3,
                           timestamps=[500_000], swept=True),  # IDM swept
            LiquidityLevel(price=84.82, level_type="bsl", touch_count=3,
                           timestamps=[800_000], swept=False),
            LiquidityLevel(price=82.10, level_type="ssl", touch_count=2,
                           timestamps=[900_000], swept=False),
        ]
        ob_break = StructureBreak(
            timestamp=750_000, break_type="bos", direction="bearish",
            break_price=85.0, broken_level=85.2, candle_index=8,
        )
        ob_4h = [
            OrderBlock(
                timestamp=750_000, pair="SOL/USDT", timeframe="4h",
                direction="bearish",
                high=85.55, low=85.25,
                body_high=85.50, body_low=85.30,
                entry_price=85.40,
                volume=100.0, volume_ratio=1.5,
                mitigated=False,
                associated_break=ob_break,
                impulse_score=0.78,
            ),
        ]
        state_4h = _make_state(
            trend="bearish",
            swing_highs=[SwingPoint(timestamp=500_000, price=90.0, index=5, swing_type="high")],
            swing_lows=[SwingPoint(timestamp=400_000, price=80.0, index=3, swing_type="low")],
            latest_break=latest_break,
        )
        tfa_4h = TFAnalysis(
            timeframe="4h", state=state_4h, obs=ob_4h, fvgs=[], liquidity=liq_4h,
        )
        state_1h = _make_state(trend="bearish")
        tfa_1h = TFAnalysis(
            timeframe="1h", state=state_1h, obs=[], fvgs=[], liquidity=[],
        )

        raw = {
            "4h": _make_bear_displacement_candles(),
            "1h": _make_bear_displacement_candles(),
        }
        return _make_snapshot(
            tf_results={"4h": tfa_4h, "1h": tfa_1h},
            current_price=84.12, side="short", conf="medium",
            invalidation=85.78, raw_candles=raw,
        )

    def test_renders_all_required_sections(self):
        snap = self._build_minimal_snap()
        out = _render_telegram_markdown(snap)
        assert _has_required_telegram_sections(out), (
            f"Missing section markers. Output:\n{out}"
        )

    def test_includes_pair_and_price(self):
        snap = self._build_minimal_snap()
        out = _render_telegram_markdown(snap)
        assert "SOL/USDT" in out
        assert "84.12" in out

    def test_brief_under_max_lines(self):
        snap = self._build_minimal_snap()
        out = _render_telegram_markdown(snap)
        n_lines = len(out.split("\n"))
        assert n_lines <= 35, f"Brief too long: {n_lines} lines\n{out}"

    def test_uses_telegram_markdown_syntax(self):
        snap = self._build_minimal_snap()
        out = _render_telegram_markdown(snap)
        assert "*BIAS:*" in out  # bold markdown
        assert "`" in out  # code-tick for prices

    def test_displacement_section_present_when_data(self):
        snap = self._build_minimal_snap()
        out = _render_telegram_markdown(snap)
        assert "Displacement 4H" in out

    def test_idm_flag_appears_when_present(self):
        snap = self._build_minimal_snap()
        out = _render_telegram_markdown(snap)
        # IDM was set up with BSL swept BEFORE the BOS — should detect
        assert "IDM confirmed" in out


# ---------------------------------------------------------------------------
# PR1 v2 — quick wins
# ---------------------------------------------------------------------------


class TestPDBiasConflict:
    def test_short_in_discount_is_conflict(self):
        assert _pd_bias_conflict("short", "discount") is True

    def test_long_in_premium_is_conflict(self):
        assert _pd_bias_conflict("long", "premium") is True

    def test_short_in_premium_no_conflict(self):
        assert _pd_bias_conflict("short", "premium") is False

    def test_long_in_discount_no_conflict(self):
        assert _pd_bias_conflict("long", "discount") is False

    def test_equilibrium_never_conflict(self):
        assert _pd_bias_conflict("short", "equilibrium") is False
        assert _pd_bias_conflict("long", "equilibrium") is False

    def test_undefined_side_never_conflict(self):
        assert _pd_bias_conflict("undefined", "premium") is False
        assert _pd_bias_conflict(None, "discount") is False

    def test_none_zone_never_conflict(self):
        assert _pd_bias_conflict("short", None) is False


class TestSweepDistance:
    def test_distance_pct_basic(self):
        assert _sweep_distance_pct(100.0, 105.0) == pytest.approx(5.0)
        assert _sweep_distance_pct(100.0, 95.0) == pytest.approx(5.0)

    def test_distance_pct_none_sweep(self):
        assert _sweep_distance_pct(100.0, None) is None

    def test_distance_pct_zero_price(self):
        assert _sweep_distance_pct(0.0, 105.0) is None

    def test_actionable_within_default_5pct(self):
        assert _sweep_actionable(3.0) is True
        assert _sweep_actionable(5.0) is True
        assert _sweep_actionable(0.5) is True

    def test_not_actionable_beyond_5pct(self):
        assert _sweep_actionable(5.01) is False
        assert _sweep_actionable(15.0) is False

    def test_none_distance_not_actionable(self):
        assert _sweep_actionable(None) is False


class TestTradeTriplet:
    def _snap_with_levels(self, side="short", price=100.0, invalidation=102.0,
                          liquidity=None):
        liq = liquidity or []
        tfa = TFAnalysis(
            timeframe="4h", state=_make_state(trend="bearish"),
            obs=[], fvgs=[], liquidity=liq,
        )
        return _make_snapshot(
            tf_results={"4h": tfa}, current_price=price,
            side=side, conf="medium", invalidation=invalidation,
        )

    def test_valid_short_triplet(self):
        # BSL at 101 (sweep, 1% away), invalidation 102, SSL at 95 (target far enough)
        # Floor = (101-102)*1.5 = 1.5, target 95 is 6 away from sweep → passes
        liq = [
            LiquidityLevel(price=101.0, level_type="bsl", touch_count=2,
                           timestamps=[1000], swept=False),
            LiquidityLevel(price=95.0, level_type="ssl", touch_count=2,
                           timestamps=[1100], swept=False),
        ]
        snap = self._snap_with_levels(liquidity=liq)
        t = _trade_triplet(snap)
        assert t is not None
        assert t["valid"] is True
        assert t["entry"] == 101.0
        assert t["sl"] == 102.0
        assert t["tp"] == 95.0
        assert t["rr"] >= 5.0

    def test_sweep_too_far_returns_invalid(self):
        # BSL at 120 (20% above current 100) — not actionable
        liq = [
            LiquidityLevel(price=120.0, level_type="bsl", touch_count=2,
                           timestamps=[1000], swept=False),
        ]
        snap = self._snap_with_levels(liquidity=liq)
        t = _trade_triplet(snap)
        assert t is not None
        assert t["valid"] is False
        assert t["reason"] == "sweep_too_far"
        assert t["sweep_distance_pct"] > 5.0

    def test_no_target_returns_invalid(self):
        # Only sweep available, no SSL below
        liq = [
            LiquidityLevel(price=101.0, level_type="bsl", touch_count=2,
                           timestamps=[1000], swept=False),
        ]
        snap = self._snap_with_levels(liquidity=liq)
        t = _trade_triplet(snap)
        assert t["valid"] is False
        assert t["reason"] == "no_valid_target"

    def test_undefined_side_returns_none(self):
        snap = self._snap_with_levels(side="undefined")
        assert _trade_triplet(snap) is None

    def test_missing_invalidation_returns_none(self):
        snap = self._snap_with_levels(invalidation=None)
        assert _trade_triplet(snap) is None


class TestBosSessionQuality:
    def _ts_at_hour(self, hour: int, minute: int = 0) -> int:
        return ((hour * 3600) + (minute * 60)) * 1000

    def test_asian_session_low_quality(self):
        q = _bos_session_quality(self._ts_at_hour(21))
        assert q["session"] == "Asian"
        assert q["quality"] == "low"

    def test_london_session_high_quality(self):
        q = _bos_session_quality(self._ts_at_hour(3))
        assert q["session"] == "London"
        assert q["quality"] == "high"

    def test_ny_am_high_quality(self):
        q = _bos_session_quality(self._ts_at_hour(13))
        assert q["session"] == "NY AM"
        assert q["quality"] == "high"

    def test_dead_zone_low_quality(self):
        # Hour 6 = between London and NY AM = dead zone
        q = _bos_session_quality(self._ts_at_hour(6))
        assert q["session"] == "dead zone"
        assert q["quality"] == "low"

    def test_no_break_returns_none(self):
        assert _bos_session_quality(None) is None


class TestRenderTelegramV2Integration:
    """Verifies PR1 v2 features surface correctly in rendered brief."""

    def _build_conflict_snap(self):
        """Snap with SHORT bias but PD will be DISCOUNT — triggers conflict."""
        latest_break = StructureBreak(
            timestamp=1_000_000, break_type="bos", direction="bearish",
            break_price=84.0, broken_level=84.5, candle_index=10,
        )
        # Range 80-90, price at 81 = ~10% (discount). Bias short = conflict.
        swing_highs = [SwingPoint(timestamp=500_000, price=90.0, index=5, swing_type="high")]
        swing_lows = [SwingPoint(timestamp=400_000, price=80.0, index=3, swing_type="low")]
        liq = [
            LiquidityLevel(price=81.5, level_type="bsl", touch_count=2,
                           timestamps=[800_000], swept=False),
            LiquidityLevel(price=80.2, level_type="ssl", touch_count=2,
                           timestamps=[900_000], swept=False),
        ]
        state = _make_state(trend="bearish",
                            swing_highs=swing_highs, swing_lows=swing_lows,
                            latest_break=latest_break)
        tfa = TFAnalysis(timeframe="4h", state=state, obs=[], fvgs=[], liquidity=liq)
        return _make_snapshot(
            tf_results={"4h": tfa}, current_price=81.0, side="short",
            conf="medium", invalidation=82.0,
            raw_candles={"4h": _make_bear_displacement_candles()},
        )

    def test_pd_bias_conflict_flag_renders(self):
        snap = self._build_conflict_snap()
        out = _render_telegram_markdown(snap)
        assert "PD-BIAS CONFLICT" in out, f"Conflict flag missing:\n{out}"
        assert "PD conflict" in out  # confidence suffix

    def test_sweep_too_far_renders_spectator_warning(self):
        latest_break = StructureBreak(
            timestamp=1_000_000, break_type="bos", direction="bearish",
            break_price=84.0, broken_level=84.5, candle_index=10,
        )
        # BSL at 120 = 20% above price 100 — way too far
        liq = [
            LiquidityLevel(price=120.0, level_type="bsl", touch_count=2,
                           timestamps=[1000], swept=False),
        ]
        state = _make_state(trend="bearish", latest_break=latest_break)
        tfa = TFAnalysis(timeframe="4h", state=state, obs=[], fvgs=[], liquidity=liq)
        snap = _make_snapshot(
            tf_results={"4h": tfa}, current_price=100.0, side="short",
            conf="medium", invalidation=105.0,
            raw_candles={"4h": _make_bear_displacement_candles()},
        )
        out = _render_telegram_markdown(snap)
        assert "Sweep too far" in out
        assert "spectator" in out

    def test_explicit_triplet_renders_with_rr(self):
        latest_break = StructureBreak(
            timestamp=1_000_000, break_type="bos", direction="bearish",
            break_price=99.0, broken_level=99.5, candle_index=10,
        )
        liq = [
            LiquidityLevel(price=101.0, level_type="bsl", touch_count=2,
                           timestamps=[1000], swept=False),
            LiquidityLevel(price=95.0, level_type="ssl", touch_count=2,
                           timestamps=[1100], swept=False),
        ]
        state = _make_state(trend="bearish", latest_break=latest_break)
        tfa = TFAnalysis(timeframe="4h", state=state, obs=[], fvgs=[], liquidity=liq)
        snap = _make_snapshot(
            tf_results={"4h": tfa}, current_price=100.0, side="short",
            conf="medium", invalidation=102.0,
            raw_candles={"4h": _make_bear_displacement_candles()},
        )
        out = _render_telegram_markdown(snap)
        assert "Entry:" in out
        assert "SL:" in out
        assert "TP:" in out
        assert "R:R:" in out

    def test_bos_session_quality_renders(self):
        # Create snap with BOS at hour 3 UTC (London — high quality)
        ts_3am = (3 * 3600) * 1000
        latest_break = StructureBreak(
            timestamp=ts_3am, break_type="bos", direction="bearish",
            break_price=99.0, broken_level=99.5, candle_index=10,
        )
        state = _make_state(trend="bearish", latest_break=latest_break)
        tfa = TFAnalysis(timeframe="4h", state=state, obs=[], fvgs=[], liquidity=[])
        snap = _make_snapshot(
            tf_results={"4h": tfa}, current_price=100.0, side="short",
            conf="medium", invalidation=102.0,
            raw_candles={"4h": _make_bear_displacement_candles()},
        )
        out = _render_telegram_markdown(snap)
        assert "London session" in out
        assert "high quality" in out

    def test_brief_under_35_lines_with_all_v2_features(self):
        snap = self._build_conflict_snap()
        out = _render_telegram_markdown(snap)
        n = len(out.split("\n"))
        assert n <= 35, f"Brief too long: {n} lines\n{out}"


# ---------------------------------------------------------------------------
# PR2 v2 — Daily Context Memory (PDH/PDL/PWH/PWL + chain + today)
# ---------------------------------------------------------------------------


def _make_daily(open_, high, low, close, ts_ms, pair="SOL/USDT"):
    return Candle(
        timestamp=ts_ms, open=open_, high=high, low=low, close=close,
        volume=1000.0, volume_quote=1000.0 * close,
        pair=pair, timeframe="1d", confirmed=True,
    )


def _series_n_daily(n: int, base_ts_ms: int = 1_700_000_000_000):
    """Build N daily candles 1 day apart, varying close around 100."""
    candles = []
    for i in range(n):
        # day i: open 100+i, close 100+i+0.5, high 100+i+1, low 100+i-1
        op = 100.0 + i
        cl = op + 0.5
        candles.append(_make_daily(op, op + 1, op - 1, cl,
                                   ts_ms=base_ts_ms + i * 86_400_000))
    return candles


class TestComputePDHPDL:
    def test_basic_pdh_pdl(self):
        # Yesterday: high=105, low=95, close=100. Today: high=104, low=99, close=101
        candles = [
            _make_daily(98, 105, 95, 100, ts_ms=1_000_000_000_000),
            _make_daily(100, 104, 99, 101, ts_ms=1_000_086_400_000),
        ]
        r = _compute_pdh_pdl(candles)
        assert r is not None
        assert r["pdh"] == 105
        assert r["pdl"] == 95
        assert r["pdh_status"] == "untaken"   # today_high 104 < PDH 105
        assert r["pdl_status"] == "untaken"   # today_low 99 > PDL 95

    def test_pdh_swept_wick_only(self):
        # PDH = 105. Today_high = 106 (wick above), today_close = 103 (below)
        candles = [
            _make_daily(98, 105, 95, 100, ts_ms=1_000_000_000_000),
            _make_daily(100, 106, 99, 103, ts_ms=1_000_086_400_000),
        ]
        r = _compute_pdh_pdl(candles)
        assert r["pdh_status"] == "swept"

    def test_pdh_broken_close_above(self):
        candles = [
            _make_daily(98, 105, 95, 100, ts_ms=1_000_000_000_000),
            _make_daily(100, 108, 99, 107, ts_ms=1_000_086_400_000),
        ]
        r = _compute_pdh_pdl(candles)
        assert r["pdh_status"] == "broken"

    def test_pdl_broken_close_below(self):
        candles = [
            _make_daily(102, 105, 95, 100, ts_ms=1_000_000_000_000),
            _make_daily(100, 102, 90, 92, ts_ms=1_000_086_400_000),
        ]
        r = _compute_pdh_pdl(candles)
        assert r["pdl_status"] == "broken"

    def test_insufficient_candles_returns_none(self):
        assert _compute_pdh_pdl([]) is None
        assert _compute_pdh_pdl([_make_daily(100, 101, 99, 100, ts_ms=0)]) is None


class TestComputePWHPWL:
    def test_returns_none_when_insufficient(self):
        candles = _series_n_daily(7)
        assert _compute_pwh_pwl(candles) is None  # need ≥14

    def test_basic_prev_week_aggregation(self):
        # 20 days of candles ending on a known date
        # 2026-05-23 = Saturday (weekday=5). Build candles ending today.
        import datetime as _dt
        today = _dt.datetime(2026, 5, 23, 0, 0, 0, tzinfo=_dt.timezone.utc)
        candles = []
        for i in range(20, 0, -1):
            d = today - _dt.timedelta(days=i - 1)
            ts = int(d.timestamp() * 1000)
            candles.append(_make_daily(100, 110 + i, 90 - i, 100 + (i % 5), ts_ms=ts))
        r = _compute_pwh_pwl(candles)
        assert r is not None
        assert r["n_days"] == 7  # full prior week
        # Sanity: pwh > 100, pwl < 100
        assert r["pwh"] > 100
        assert r["pwl"] < 100

    def test_inside_flag(self):
        import datetime as _dt
        today = _dt.datetime(2026, 5, 23, 0, 0, 0, tzinfo=_dt.timezone.utc)
        candles = []
        for i in range(20, 0, -1):
            d = today - _dt.timedelta(days=i - 1)
            ts = int(d.timestamp() * 1000)
            # Prev week candles have range [99, 105]; today inside
            candles.append(_make_daily(100, 105, 99, 102, ts_ms=ts))
        r = _compute_pwh_pwl(candles)
        assert r["inside"] is True


class TestDailyBiasChain:
    def test_chain_with_clear_pattern(self):
        # 5 bear candles: open > close consistently
        candles = []
        for i in range(5):
            candles.append(_make_daily(100, 101, 95, 96, ts_ms=i * 86_400_000))
        chain = _daily_bias_chain(candles)
        assert chain["bull"] == 0
        assert chain["bear"] == 5
        assert chain["majority"] == "bear"

    def test_chain_mixed(self):
        # 3 bull, 2 bear
        candles = [
            _make_daily(100, 105, 99, 104, ts_ms=0),
            _make_daily(100, 105, 99, 104, ts_ms=1),
            _make_daily(100, 101, 95, 96, ts_ms=2),
            _make_daily(100, 105, 99, 104, ts_ms=3),
            _make_daily(100, 101, 95, 96, ts_ms=4),
        ]
        chain = _daily_bias_chain(candles)
        assert chain["bull"] == 3
        assert chain["bear"] == 2
        assert chain["majority"] == "bull"

    def test_doji_detected(self):
        # 1 doji + 4 bull
        candles = [
            _make_daily(100, 100.05, 99.95, 100.00, ts_ms=0),  # body 0% → doji
            _make_daily(100, 105, 99, 104, ts_ms=1),
            _make_daily(100, 105, 99, 104, ts_ms=2),
            _make_daily(100, 105, 99, 104, ts_ms=3),
            _make_daily(100, 105, 99, 104, ts_ms=4),
        ]
        chain = _daily_bias_chain(candles)
        assert chain["doji"] == 1
        assert chain["bull"] == 4

    def test_insufficient_returns_none(self):
        candles = [_make_daily(100, 101, 99, 100, ts_ms=0)] * 3
        assert _daily_bias_chain(candles) is None


class TestTodayCandleStatus:
    def test_bull_today(self):
        import datetime as _dt
        now_ms = int(_dt.datetime.utcnow().timestamp() * 1000)
        candles = [_make_daily(100, 105, 99, 103, ts_ms=now_ms)]
        s = _today_candle_status(candles)
        assert s["side"] == "bull"
        assert s["body_pct"] > 0
        assert s["forming"] is True

    def test_bear_today(self):
        import datetime as _dt
        now_ms = int(_dt.datetime.utcnow().timestamp() * 1000)
        candles = [_make_daily(100, 101, 95, 97, ts_ms=now_ms)]
        s = _today_candle_status(candles)
        assert s["side"] == "bear"
        assert s["body_pct"] < 0

    def test_inside_doji(self):
        import datetime as _dt
        now_ms = int(_dt.datetime.utcnow().timestamp() * 1000)
        candles = [_make_daily(100, 100.05, 99.95, 100.0, ts_ms=now_ms)]
        s = _today_candle_status(candles)
        assert s["side"] == "inside"

    def test_returns_none_when_empty(self):
        assert _today_candle_status([]) is None


class TestDailyContextRenderIntegration:
    def _snap_with_daily(self, daily_candles):
        latest_break = StructureBreak(
            timestamp=1_000_000, break_type="bos", direction="bearish",
            break_price=99.0, broken_level=99.5, candle_index=10,
        )
        liq = [
            LiquidityLevel(price=101.0, level_type="bsl", touch_count=2,
                           timestamps=[1000], swept=False),
            LiquidityLevel(price=95.0, level_type="ssl", touch_count=2,
                           timestamps=[1100], swept=False),
        ]
        state = _make_state(trend="bearish", latest_break=latest_break)
        tfa = TFAnalysis(timeframe="4h", state=state, obs=[], fvgs=[], liquidity=liq)
        return _make_snapshot(
            tf_results={"4h": tfa}, current_price=100.0, side="short",
            conf="medium", invalidation=102.0,
            raw_candles={
                "4h": _make_bear_displacement_candles(),
                "1d": daily_candles,
            },
        )

    def test_daily_context_section_renders(self):
        import datetime as _dt
        today = _dt.datetime(2026, 5, 23, 0, 0, 0, tzinfo=_dt.timezone.utc)
        candles = []
        for i in range(20, 0, -1):
            d = today - _dt.timedelta(days=i - 1)
            ts = int(d.timestamp() * 1000)
            candles.append(_make_daily(100, 102, 98, 99, ts_ms=ts))
        snap = self._snap_with_daily(candles)
        out = _render_telegram_markdown(snap)
        assert "*DAILY CONTEXT:*" in out
        assert "Today:" in out
        assert "Chain" in out
        assert "Weekly:" in out
        assert "PDH" in out
        assert "PWH" in out

    def test_daily_context_omitted_when_no_daily_data(self):
        latest_break = StructureBreak(
            timestamp=1_000_000, break_type="bos", direction="bearish",
            break_price=99.0, broken_level=99.5, candle_index=10,
        )
        state = _make_state(trend="bearish", latest_break=latest_break)
        tfa = TFAnalysis(timeframe="4h", state=state, obs=[], fvgs=[], liquidity=[])
        snap = _make_snapshot(
            tf_results={"4h": tfa}, current_price=100.0, side="short",
            conf="medium", invalidation=102.0,
            raw_candles={"4h": _make_bear_displacement_candles(), "1d": []},
        )
        out = _render_telegram_markdown(snap)
        # Section header must NOT appear when no daily candles
        assert "*DAILY CONTEXT:*" not in out

    def test_brief_under_40_lines_with_daily_context(self):
        import datetime as _dt
        today = _dt.datetime(2026, 5, 23, 0, 0, 0, tzinfo=_dt.timezone.utc)
        candles = []
        for i in range(20, 0, -1):
            d = today - _dt.timedelta(days=i - 1)
            ts = int(d.timestamp() * 1000)
            candles.append(_make_daily(100, 102, 98, 99, ts_ms=ts))
        snap = self._snap_with_daily(candles)
        out = _render_telegram_markdown(snap)
        n = len(out.split("\n"))
        assert n <= 40, f"Brief too long after DAILY CONTEXT: {n}\n{out}"


# ---------------------------------------------------------------------------
# PR3 v2 — Adaptive TP via Daily ATR
# ---------------------------------------------------------------------------


class TestDailyATR:
    def test_basic_atr_constant_range(self):
        # 20 candles, each with TR ≈ 2.0 (high-low = 2, prev_close ~= mid)
        candles = []
        base = 1_700_000_000_000
        for i in range(20):
            candles.append(_make_daily(100, 101, 99, 100,
                                       ts_ms=base + i * 86_400_000))
        atr = _daily_atr(candles, period=14)
        assert atr is not None
        # All TRs = 2.0 → ATR(14) = 2.0
        assert atr == pytest.approx(2.0, abs=0.01)

    def test_returns_none_insufficient_candles(self):
        candles = [_make_daily(100, 101, 99, 100, ts_ms=i) for i in range(10)]
        assert _daily_atr(candles, period=14) is None

    def test_atr_handles_gaps(self):
        # 15 candles, day 5 has a gap up — TR includes |high - prev_close|
        candles = []
        for i in range(15):
            if i == 5:
                # Gap up: open at 110, range 110-115, prev close was 100
                candles.append(_make_daily(110, 115, 110, 113, ts_ms=i * 86_400_000))
            else:
                candles.append(_make_daily(100, 101, 99, 100, ts_ms=i * 86_400_000))
        atr = _daily_atr(candles, period=14)
        assert atr is not None
        assert atr > 2.0  # gap pulls average up


class TestAdaptiveTP:
    def test_single_when_short_distance(self):
        # entry 100, sl 102, final 99 (1pt away), ATR 2.0, threshold = 4.0
        # 1 < 4 → single
        r = _adaptive_tp(100, 102, 99, [], daily_atr=2.0)
        assert r["mode"] == "single"
        assert r["tp1"] == 99
        assert r["tp2"] is None
        assert r["splits"] == [100]

    def test_single_when_no_atr(self):
        r = _adaptive_tp(100, 102, 90, [], daily_atr=None)
        assert r["mode"] == "single"

    def test_scaled_with_intermediate(self):
        # entry 100, sl 102, final 90 (10pt away), ATR 3.0, threshold = 6.0
        # 10 > 6 → scaled. Intermediate at 97.5 (2.5 from entry, within 1× ATR=3)
        intermediate = type("L", (), {"price": 97.5})()
        r = _adaptive_tp(100, 102, 90, [intermediate], daily_atr=3.0)
        assert r["mode"] == "scaled"
        assert r["tp1"] == 97.5
        assert r["tp2"] == 90
        assert r["splits"] == [50, 50]

    def test_scaled_fallback_to_single_no_intermediate(self):
        # Long distance but no intermediate within 1× ATR → single fallback
        r = _adaptive_tp(100, 102, 90, [], daily_atr=3.0)
        assert r["mode"] == "single"
        assert r.get("fallback_reason") == "no_intermediate_in_range"

    def test_scaled_picks_closest_to_1x_atr(self):
        # Multiple intermediates: 99 (1 away), 98 (2 away), 97 (3 away, == ATR).
        # ATR = 3, threshold for scaled = 6. Final at 92 (8 away → scaled).
        # Should pick 97 (closest to 1× ATR distance).
        intermediates = [
            type("L", (), {"price": 99})(),
            type("L", (), {"price": 98})(),
            type("L", (), {"price": 97})(),
        ]
        r = _adaptive_tp(100, 102, 92, intermediates, daily_atr=3.0)
        assert r["mode"] == "scaled"
        assert r["tp1"] == 97

    def test_intermediate_past_final_target_excluded(self):
        # Short trade: entry 100, final 86 (14 away). ATR=4 (intermediate
        # range 4 from entry, threshold 2*ATR=8). target_distance 14 > 8 → scaled.
        # Intermediate at 85 (below final 86) → excluded.
        # Intermediate at 97 (3 from entry, ≤ ATR=4, between entry+final) → valid.
        intermediates = [
            type("L", (), {"price": 85})(),  # past final 86, excluded
            type("L", (), {"price": 97})(),  # valid
        ]
        r = _adaptive_tp(100, 102, 86, intermediates, daily_atr=4.0)
        assert r["mode"] == "scaled"
        assert r["tp1"] == 97

    def test_long_trade_direction_handled(self):
        # entry 100, sl 98, final 110 (10pt above). ATR 3, threshold 6. Scaled.
        # Intermediate at 102 (2 above entry, within ATR).
        intermediates = [type("L", (), {"price": 102})()]
        r = _adaptive_tp(100, 98, 110, intermediates, daily_atr=3.0)
        assert r["mode"] == "scaled"
        assert r["tp1"] == 102


class TestAdaptiveTPRenderIntegration:
    def _snap_with_long_distance_setup(self):
        """SHORT setup with long-distance target requiring scaled TP."""
        latest_break = StructureBreak(
            timestamp=1_000_000, break_type="bos", direction="bearish",
            break_price=99.0, broken_level=99.5, candle_index=10,
        )
        # Setup so target picker (1.5R floor) lands on far SSL 92, and
        # adaptive TP picks intermediate 99.7 for TP1.
        # entry sweep BSL 101, invalidation 102 → floor = (101-102)*1.5 = 1.5
        # SSL 99.7 distance from sweep = 1.3 → BELOW floor, skipped by _pick_valid_target
        # SSL 92 distance from sweep = 9 → passes floor, becomes target
        # ATR=4 → intermediate range 4 from entry, threshold 2*ATR=8
        # target_distance entry→final = 9 > 8 → scaled
        # intermediate 99.7 is between 92 and 101, distance from entry 1.3 ≤ 4 → valid TP1
        liq = [
            LiquidityLevel(price=101.0, level_type="bsl", touch_count=2,
                           timestamps=[1000], swept=False),
            LiquidityLevel(price=99.7, level_type="ssl", touch_count=2,
                           timestamps=[1100], swept=False),
            LiquidityLevel(price=92.0, level_type="ssl", touch_count=2,
                           timestamps=[1200], swept=False),
        ]
        state = _make_state(trend="bearish", latest_break=latest_break)
        tfa = TFAnalysis(timeframe="4h", state=state, obs=[], fvgs=[], liquidity=liq)
        # Daily candles with range 4 (high 102, low 98) → ATR ≈ 4
        daily = [
            _make_daily(100, 102, 98, 100, ts_ms=i * 86_400_000)
            for i in range(20)
        ]
        return _make_snapshot(
            tf_results={"4h": tfa}, current_price=100.0, side="short",
            conf="medium", invalidation=102.0,
            raw_candles={
                "4h": _make_bear_displacement_candles(),
                "1d": daily,
            },
        )

    def test_scaled_tp_renders_when_long_distance(self):
        snap = self._snap_with_long_distance_setup()
        out = _render_telegram_markdown(snap)
        assert "TP1:" in out
        assert "TP2:" in out
        assert "scaled" in out
        assert "daily ATR" in out

    def test_single_tp_renders_for_short_distance(self):
        # Set up where target is close (single TP)
        latest_break = StructureBreak(
            timestamp=1_000_000, break_type="bos", direction="bearish",
            break_price=99.0, broken_level=99.5, candle_index=10,
        )
        liq = [
            LiquidityLevel(price=101.0, level_type="bsl", touch_count=2,
                           timestamps=[1000], swept=False),
            LiquidityLevel(price=99.0, level_type="ssl", touch_count=2,
                           timestamps=[1100], swept=False),
        ]
        state = _make_state(trend="bearish", latest_break=latest_break)
        tfa = TFAnalysis(timeframe="4h", state=state, obs=[], fvgs=[], liquidity=liq)
        # Large ATR so threshold is high relative to small target distance
        daily = [
            _make_daily(100, 105, 95, 100, ts_ms=i * 86_400_000)
            for i in range(20)
        ]
        snap = _make_snapshot(
            tf_results={"4h": tfa}, current_price=100.0, side="short",
            conf="medium", invalidation=102.0,
            raw_candles={
                "4h": _make_bear_displacement_candles(),
                "1d": daily,
            },
        )
        out = _render_telegram_markdown(snap)
        # Single mode: "TP:" present, "TP1:" NOT
        assert "TP1:" not in out
        # Allow plain "TP:" line (single mode keeps existing format)
        assert "TP:" in out


# ---------------------------------------------------------------------------
# PR4 v2 — Structure Context (HTF duration + LTF flip + impulse + wick)
# ---------------------------------------------------------------------------


class TestTrendDuration:
    def test_basic_duration(self):
        # Bear trend, BOS at ts=1000, current candle at ts=10000 (9s later)
        brk = StructureBreak(
            timestamp=1000, break_type="bos", direction="bearish",
            break_price=99.0, broken_level=99.5, candle_index=5,
        )
        state = _make_state(trend="bearish", structure_breaks=[brk],
                            latest_break=brk)
        tfa = TFAnalysis(timeframe="4h", state=state, obs=[], fvgs=[], liquidity=[])
        candles = [_make_candle(100, 101, 99, 100, ts=ts)
                   for ts in [500, 1500, 2500, 5000, 10000]]
        d = _trend_duration(tfa, candles)
        assert d is not None
        assert d["trend"] == "bearish"
        assert d["candles_back"] == 4  # candles 1500, 2500, 5000, 10000 all >= 1000

    def test_returns_none_no_breaks(self):
        state = _make_state(trend="undefined", structure_breaks=[])
        tfa = TFAnalysis(timeframe="4h", state=state, obs=[], fvgs=[], liquidity=[])
        assert _trend_duration(tfa, [_make_candle(100, 101, 99, 100, ts=0)]) is None

    def test_returns_none_no_candles(self):
        brk = StructureBreak(timestamp=0, break_type="bos", direction="bearish",
                             break_price=99.0, broken_level=99.5, candle_index=0)
        state = _make_state(trend="bearish", structure_breaks=[brk], latest_break=brk)
        tfa = TFAnalysis(timeframe="4h", state=state, obs=[], fvgs=[], liquidity=[])
        assert _trend_duration(tfa, []) is None


class TestLtfFlipVsHtf:
    def _make_tfa(self, tf, trend, breaks):
        latest = breaks[-1] if breaks else None
        state = _make_state(trend=trend, structure_breaks=breaks, latest_break=latest)
        return TFAnalysis(timeframe=tf, state=state, obs=[], fvgs=[], liquidity=[])

    def test_flip_detected_when_ltf_against_htf(self):
        # HTF bear, 15m flipped bull (most recent break is bullish, prev was bearish)
        prev_brk = StructureBreak(timestamp=1000, break_type="bos",
                                  direction="bearish", break_price=99,
                                  broken_level=99.5, candle_index=2)
        new_brk = StructureBreak(timestamp=2000, break_type="choch",
                                 direction="bullish", break_price=101,
                                 broken_level=100.5, candle_index=10)
        tf_results = {
            "4h": self._make_tfa("4h", "bearish",
                                  [StructureBreak(timestamp=500, break_type="bos",
                                                 direction="bearish", break_price=99,
                                                 broken_level=99.5, candle_index=0)]),
            "15m": self._make_tfa("15m", "bullish", [prev_brk, new_brk]),
        }
        r = _ltf_flip_vs_htf(tf_results)
        assert r is not None
        assert r["ltf_tf"] == "15m"
        assert r["ltf_trend"] == "bullish"
        assert r["htf_trend"] == "bearish"

    def test_no_flip_when_same_direction(self):
        # Both HTF and LTF bear → continuation, not flip
        brk = StructureBreak(timestamp=2000, break_type="bos", direction="bearish",
                             break_price=99, broken_level=99.5, candle_index=10)
        tf_results = {
            "4h": self._make_tfa("4h", "bearish", [brk]),
            "15m": self._make_tfa("15m", "bearish", [brk]),
        }
        assert _ltf_flip_vs_htf(tf_results) is None

    def test_returns_none_when_htf_undefined(self):
        tf_results = {
            "4h": self._make_tfa("4h", "undefined", []),
            "15m": self._make_tfa("15m", "bullish",
                                  [StructureBreak(timestamp=2000, break_type="bos",
                                                 direction="bullish", break_price=101,
                                                 broken_level=100.5, candle_index=10)]),
        }
        assert _ltf_flip_vs_htf(tf_results) is None

    def test_prefers_lowest_tf(self):
        # Both 15m AND 1h flipped against HTF — should prefer 15m
        prev_b = StructureBreak(timestamp=500, break_type="bos", direction="bearish",
                                break_price=99, broken_level=99.5, candle_index=0)
        new_b = StructureBreak(timestamp=2000, break_type="choch",
                               direction="bullish", break_price=101,
                               broken_level=100.5, candle_index=10)
        tf_results = {
            "4h": self._make_tfa("4h", "bearish", [prev_b]),
            "1h": self._make_tfa("1h", "bullish", [prev_b, new_b]),
            "15m": self._make_tfa("15m", "bullish", [prev_b, new_b]),
        }
        r = _ltf_flip_vs_htf(tf_results)
        assert r["ltf_tf"] == "15m"


class TestLastCandleImpulse:
    def test_big_impulse_flagged(self):
        # 30 baseline candles with 0.5% bodies, last candle 3% body → ratio 6
        candles = [_make_candle(100, 100.5, 99.5, 100.5, ts=i) for i in range(30)]
        candles.append(_make_candle(100, 103.5, 99.5, 103, ts=30))
        imp = _last_candle_impulse(candles)
        assert imp is not None
        assert imp["magnitude"] in ("big", "extreme")
        assert imp["direction"] == "bull"
        assert imp["ratio"] >= 3.0

    def test_normal_impulse_not_flagged(self):
        candles = [_make_candle(100, 100.5, 99.5, 100.5, ts=i) for i in range(31)]
        imp = _last_candle_impulse(candles)
        assert imp is not None
        assert imp["magnitude"] == "normal"

    def test_insufficient_candles_returns_none(self):
        assert _last_candle_impulse([_make_candle(100, 101, 99, 100, ts=0)]) is None


class TestWickIntoLiquidity:
    def test_bsl_swept_by_wick(self):
        # Last candle: high 102, close 99 → wick above level at 101.5, close below
        candles = [_make_candle(100, 102, 99, 99, ts=0)]
        liq = [
            LiquidityLevel(price=101.5, level_type="bsl", touch_count=3,
                           timestamps=[500], swept=False),
        ]
        r = _wick_into_liquidity(candles, liq)
        assert r is not None
        assert r["side"] == "bsl"
        assert r["level_price"] == 101.5

    def test_ssl_swept_by_wick(self):
        # Last candle: low 98, close 101 → wick below level at 98.5, close above
        candles = [_make_candle(100, 101, 98, 101, ts=0)]
        liq = [
            LiquidityLevel(price=98.5, level_type="ssl", touch_count=3,
                           timestamps=[500], swept=False),
        ]
        r = _wick_into_liquidity(candles, liq)
        assert r is not None
        assert r["side"] == "ssl"

    def test_no_sweep_when_close_through(self):
        # Wick AND close above BSL → broken, not swept
        candles = [_make_candle(100, 102, 99, 101.8, ts=0)]
        liq = [
            LiquidityLevel(price=101.5, level_type="bsl", touch_count=3,
                           timestamps=[500], swept=False),
        ]
        assert _wick_into_liquidity(candles, liq) is None

    def test_already_swept_excluded(self):
        candles = [_make_candle(100, 102, 99, 99, ts=0)]
        liq = [
            LiquidityLevel(price=101.5, level_type="bsl", touch_count=3,
                           timestamps=[500], swept=True),
        ]
        assert _wick_into_liquidity(candles, liq) is None

    def test_prefers_highest_touch_count(self):
        candles = [_make_candle(100, 102, 99, 99, ts=0)]
        liq = [
            LiquidityLevel(price=101.5, level_type="bsl", touch_count=2,
                           timestamps=[500], swept=False),
            LiquidityLevel(price=101.6, level_type="bsl", touch_count=5,
                           timestamps=[600], swept=False),
        ]
        r = _wick_into_liquidity(candles, liq)
        assert r["touch_count"] == 5


class TestStructureContextRenderIntegration:
    def _make_snap_with_structure(self, *, ltf_flip=True, big_impulse=True, wick_tap=True):
        # 4H bear since timestamp 1_000_000, current candle at 5_000_000
        brk_4h = StructureBreak(timestamp=1_000_000, break_type="bos",
                                direction="bearish", break_price=99.0,
                                broken_level=99.5, candle_index=10)
        state_4h = _make_state(trend="bearish", structure_breaks=[brk_4h],
                               latest_break=brk_4h)
        candles_4h = [_make_candle(100, 101, 99, 100, ts=t)
                      for t in [1_000_000, 2_000_000, 3_000_000, 4_000_000, 5_000_000]]

        # 15m: bullish with prev bearish break → fresh flip
        breaks_15m = []
        if ltf_flip:
            breaks_15m = [
                StructureBreak(timestamp=2_000_000, break_type="bos",
                              direction="bearish", break_price=99,
                              broken_level=99.5, candle_index=2),
                StructureBreak(timestamp=4_500_000, break_type="choch",
                              direction="bullish", break_price=101,
                              broken_level=100.5, candle_index=10),
            ]
        state_15m = _make_state(
            trend="bullish" if ltf_flip else "bearish",
            structure_breaks=breaks_15m,
            latest_break=breaks_15m[-1] if breaks_15m else None,
        )

        # 1h candles: last candle 3% body if big_impulse; tap BSL if wick_tap
        candles_1h = [_make_candle(100, 100.5, 99.5, 100.5, ts=i * 1000)
                      for i in range(30)]
        if big_impulse:
            if wick_tap:
                # 3% body bear close + wick to 102 (taps BSL 101.5)
                candles_1h.append(_make_candle(100, 102, 96.5, 97, ts=30_000))
            else:
                candles_1h.append(_make_candle(100, 103.5, 99.5, 103, ts=30_000))
        else:
            candles_1h.append(_make_candle(100, 100.5, 99.5, 100.5, ts=30_000))

        liq = []
        if wick_tap:
            liq.append(LiquidityLevel(price=101.5, level_type="bsl", touch_count=3,
                                      timestamps=[500], swept=False))

        tfa_4h = TFAnalysis(timeframe="4h", state=state_4h, obs=[], fvgs=[], liquidity=liq)
        tfa_15m = TFAnalysis(timeframe="15m", state=state_15m, obs=[], fvgs=[], liquidity=[])

        return _make_snapshot(
            tf_results={"4h": tfa_4h, "15m": tfa_15m},
            current_price=100.0, side="short", conf="medium",
            invalidation=102.0,
            raw_candles={
                "4h": candles_4h,
                "1h": candles_1h,
            },
        )

    def test_structure_context_renders_all_signals(self):
        snap = self._make_snap_with_structure()
        out = _render_telegram_markdown(snap)
        assert "*STRUCTURE CONTEXT:*" in out
        assert "4H bearish since" in out
        assert "flipped bullish vs 4H bearish" in out
        assert "Last 1H:" in out  # big impulse line
        assert "wick tapped" in out  # wick line

    def test_structure_context_omitted_when_no_signals(self):
        # No LTF flip, no big impulse, no wick tap
        snap = self._make_snap_with_structure(
            ltf_flip=False, big_impulse=False, wick_tap=False,
        )
        out = _render_telegram_markdown(snap)
        # Only trend-duration line should appear, others omitted.
        # Structure section header appears IF any line exists.
        assert "4H bearish since" in out
        assert "flipped" not in out
        assert "wick tapped" not in out

    def test_brief_under_45_lines_with_full_structure_context(self):
        snap = self._make_snap_with_structure()
        out = _render_telegram_markdown(snap)
        n = len(out.split("\n"))
        assert n <= 45, f"Brief too long with full structure context: {n}\n{out}"
