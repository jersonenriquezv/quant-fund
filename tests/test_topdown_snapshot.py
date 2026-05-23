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
    DISPLACEMENT_LOOKBACK_N, DISPLACEMENT_BASELINE_N,
    TARGET_MIN_R_MULTIPLE, ICT_KILLZONES,
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
