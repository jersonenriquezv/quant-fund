"""Tests for strategy_service.scalp_setups — Signal 1: liquidation reclaim.

Plan: docs/plans/scalp_shadow_v1.md.
"""

import time
from unittest.mock import patch

import pytest

from shared.models import (
    Candle, CVDSnapshot, FundingRate, MarketSnapshot, OIFlushEvent,
)
from strategy_service.scalp_setups import (
    ScalpSetupEvaluator,
    _FUNDING_FLAT_LOOKBACK_BARS,
    _FUNDING_FLAT_RANGE_THRESHOLD,
    _FUNDING_RATE_THRESHOLD,
    _LIQ_RECLAIM_FLUSH_MAX_AGE_MS,
    _LIQ_RECLAIM_LOOKBACK_BARS,
    _LIQ_RECLAIM_WICK_THRESHOLD,
    _SWEEP_CHOCH_LOOKBACK_BARS,
    _SWEEP_CHOCH_MIN_BODY_RATIO,
    _VOL_CVD_LOOKBACK_BARS,
    _VOL_CVD_MAX_SPREAD,
    _VOL_CVD_MIN_IMBALANCE,
    _VOL_CVD_Z_THRESHOLD,
)


# ============================================================
# Helpers
# ============================================================

def _make_candle(
    *,
    ts_ms: int,
    o: float, h: float, l: float, c: float,
    pair: str = "BTC/USDT",
    timeframe: str = "5m",
    confirmed: bool = True,
    volume: float = 100.0,
) -> Candle:
    return Candle(
        timestamp=ts_ms,
        open=o,
        high=h,
        low=l,
        close=c,
        volume=volume,
        volume_quote=volume * c,
        pair=pair,
        timeframe=timeframe,
        confirmed=confirmed,
    )


def _flat_history(*, base_price: float, count: int, start_ts_ms: int,
                  step_ms: int = 5 * 60 * 1000) -> list[Candle]:
    """Build `count` confirmed candles inside a tight range around base_price."""
    out: list[Candle] = []
    for i in range(count):
        # Tiny variation so high/low aren't identical.
        o = base_price - 1
        c = base_price + 1
        h = base_price + 2
        l = base_price - 2
        out.append(_make_candle(
            ts_ms=start_ts_ms + i * step_ms,
            o=o, h=h, l=l, c=c,
        ))
    return out


def _snapshot_with_flush(
    *,
    pair: str,
    flush_ts_ms: int,
    side: str = "long",
    size_usd: float = 1_000_000.0,
    price: float = 50_000.0,
) -> MarketSnapshot:
    return MarketSnapshot(
        pair=pair,
        timestamp=flush_ts_ms,
        recent_oi_flushes=[
            OIFlushEvent(
                timestamp=flush_ts_ms,
                pair=pair,
                side=side,
                size_usd=size_usd,
                price=price,
                source="oi_proxy",
            ),
        ],
    )


def _enable_scalp_shadow():
    """Patch SCALP_SHADOW_ENABLED on the settings instance for one test."""
    return patch("strategy_service.scalp_setups.settings.SCALP_SHADOW_ENABLED", True)


# ============================================================
# Gate behavior
# ============================================================

class TestScalpEnabledGate:

    def test_returns_none_when_disabled(self):
        evaluator = ScalpSetupEvaluator()
        # Disabled by default — even with valid inputs, no setup.
        candles = _flat_history(base_price=100.0, count=21, start_ts_ms=0)
        snap = _snapshot_with_flush(pair="BTC/USDT", flush_ts_ms=0)
        result = evaluator.evaluate_liq_reclaim(
            "BTC/USDT", candles, snap, now_ms=1_000,
        )
        assert result is None


# ============================================================
# Trigger requirements
# ============================================================

class TestTriggerRequirements:

    def test_returns_none_without_oi_flush(self):
        evaluator = ScalpSetupEvaluator()
        candles = _flat_history(base_price=100.0, count=21, start_ts_ms=0)
        # Replace last candle with a long lower wick reclaim shape.
        candles[-1] = _make_candle(
            ts_ms=21 * 60_000,
            o=100.0, h=100.5, l=98.0, c=100.2,
        )
        snap = MarketSnapshot(pair="BTC/USDT", timestamp=0, recent_oi_flushes=[])
        with _enable_scalp_shadow():
            result = evaluator.evaluate_liq_reclaim(
                "BTC/USDT", candles, snap, now_ms=21 * 60_000,
            )
        assert result is None

    def test_returns_none_when_flush_too_old(self):
        evaluator = ScalpSetupEvaluator()
        candles = _flat_history(base_price=100.0, count=21, start_ts_ms=0)
        candles[-1] = _make_candle(
            ts_ms=21 * 60_000,
            o=100.0, h=100.5, l=98.0, c=100.2,
        )
        # Flush is older than the max-age window.
        too_old_ms = 21 * 60_000 - _LIQ_RECLAIM_FLUSH_MAX_AGE_MS - 1
        snap = _snapshot_with_flush(pair="BTC/USDT", flush_ts_ms=too_old_ms)
        with _enable_scalp_shadow():
            result = evaluator.evaluate_liq_reclaim(
                "BTC/USDT", candles, snap, now_ms=21 * 60_000,
            )
        assert result is None

    def test_returns_none_for_unconfirmed_candle(self):
        evaluator = ScalpSetupEvaluator()
        candles = _flat_history(base_price=100.0, count=21, start_ts_ms=0)
        candles[-1] = _make_candle(
            ts_ms=21 * 60_000,
            o=100.0, h=100.5, l=98.0, c=100.2,
            confirmed=False,
        )
        snap = _snapshot_with_flush(pair="BTC/USDT", flush_ts_ms=21 * 60_000)
        with _enable_scalp_shadow():
            result = evaluator.evaluate_liq_reclaim(
                "BTC/USDT", candles, snap, now_ms=21 * 60_000,
            )
        assert result is None

    def test_returns_none_with_too_few_candles(self):
        evaluator = ScalpSetupEvaluator()
        candles = _flat_history(
            base_price=100.0,
            count=_LIQ_RECLAIM_LOOKBACK_BARS,  # one too few
            start_ts_ms=0,
        )
        snap = _snapshot_with_flush(pair="BTC/USDT", flush_ts_ms=0)
        with _enable_scalp_shadow():
            result = evaluator.evaluate_liq_reclaim(
                "BTC/USDT", candles, snap, now_ms=0,
            )
        assert result is None


# ============================================================
# Wick + reclaim direction logic
# ============================================================

class TestWickReclaim:

    def test_long_on_lower_wick_reclaim(self):
        evaluator = ScalpSetupEvaluator()
        candles = _flat_history(base_price=100.0, count=21, start_ts_ms=0)
        # Lower wick of ~2% (98.0 to body bottom 100.0). Upper wick tiny.
        candles[-1] = _make_candle(
            ts_ms=21 * 60_000,
            o=100.0, h=100.3, l=98.0, c=100.2,
        )
        snap = _snapshot_with_flush(pair="BTC/USDT", flush_ts_ms=21 * 60_000)
        with _enable_scalp_shadow():
            setup = evaluator.evaluate_liq_reclaim(
                "BTC/USDT", candles, snap, now_ms=21 * 60_000,
            )
        assert setup is not None
        assert setup.setup_type == "scalp_liq_reclaim_v1"
        assert setup.direction == "long"
        assert setup.entry_price == pytest.approx(100.2)
        # SL 0.20% below entry, TP2 0.40% above (per SCALP_SIGNAL_PARAMS default).
        assert setup.sl_price == pytest.approx(100.2 * (1 - 0.002))
        assert setup.tp2_price == pytest.approx(100.2 * (1 + 0.004))
        # tp1 sits at the midpoint of entry-tp2.
        assert setup.tp1_price == pytest.approx(100.2 + (setup.tp2_price - 100.2) * 0.5)
        assert setup.htf_bias == "scalp"

    def test_short_on_upper_wick_reclaim(self):
        evaluator = ScalpSetupEvaluator()
        candles = _flat_history(base_price=100.0, count=21, start_ts_ms=0)
        # Upper wick ~2%. Lower wick tiny.
        candles[-1] = _make_candle(
            ts_ms=21 * 60_000,
            o=100.0, h=102.0, l=99.7, c=99.8,
        )
        snap = _snapshot_with_flush(pair="BTC/USDT", flush_ts_ms=21 * 60_000)
        with _enable_scalp_shadow():
            setup = evaluator.evaluate_liq_reclaim(
                "BTC/USDT", candles, snap, now_ms=21 * 60_000,
            )
        assert setup is not None
        assert setup.direction == "short"
        assert setup.entry_price == pytest.approx(99.8)
        assert setup.sl_price == pytest.approx(99.8 * (1 + 0.002))
        assert setup.tp2_price == pytest.approx(99.8 * (1 - 0.004))

    def test_no_signal_when_wick_below_threshold(self):
        evaluator = ScalpSetupEvaluator()
        candles = _flat_history(base_price=100.0, count=21, start_ts_ms=0)
        # Lower wick of only ~0.3% — below the 0.5% threshold.
        candles[-1] = _make_candle(
            ts_ms=21 * 60_000,
            o=100.0, h=100.1, l=99.7, c=100.05,
        )
        snap = _snapshot_with_flush(pair="BTC/USDT", flush_ts_ms=21 * 60_000)
        with _enable_scalp_shadow():
            setup = evaluator.evaluate_liq_reclaim(
                "BTC/USDT", candles, snap, now_ms=21 * 60_000,
            )
        assert setup is None
        # Sanity check on threshold constant — keeps the test honest if it's tuned.
        assert _LIQ_RECLAIM_WICK_THRESHOLD == 0.005

    def test_no_signal_when_close_breaks_above_range(self):
        evaluator = ScalpSetupEvaluator()
        candles = _flat_history(base_price=100.0, count=21, start_ts_ms=0)
        # Close above prior 20-bar high — momentum breakout, not a reclaim.
        candles[-1] = _make_candle(
            ts_ms=21 * 60_000,
            o=101.0, h=103.0, l=99.5, c=102.5,
        )
        snap = _snapshot_with_flush(pair="BTC/USDT", flush_ts_ms=21 * 60_000)
        with _enable_scalp_shadow():
            setup = evaluator.evaluate_liq_reclaim(
                "BTC/USDT", candles, snap, now_ms=21 * 60_000,
            )
        assert setup is None


# ============================================================
# Look-ahead protection
# ============================================================

class TestNoLookahead:

    def test_appending_future_candles_does_not_change_result(self):
        """Detector must depend only on candles up to and including index -1.

        If we feed it the trigger window plus extra "future" candles tacked on
        after the trigger, it should still produce the same (or no) signal as
        when called with the exact window — proving no peek-ahead.
        """
        evaluator = ScalpSetupEvaluator()
        base_window = _flat_history(base_price=100.0, count=21, start_ts_ms=0)
        base_window[-1] = _make_candle(
            ts_ms=21 * 60_000,
            o=100.0, h=100.3, l=98.0, c=100.2,
        )
        snap = _snapshot_with_flush(pair="BTC/USDT", flush_ts_ms=21 * 60_000)

        with _enable_scalp_shadow():
            baseline = evaluator.evaluate_liq_reclaim(
                "BTC/USDT", base_window, snap, now_ms=21 * 60_000,
            )

        # Baseline should be a real signal — sanity.
        assert baseline is not None

        # Now construct a longer history where the same trigger candle is no
        # longer the last bar (a future candle is appended). The detector
        # ALWAYS reads candles[-1] — so the new trigger is the appended bar,
        # not the one we built. That bar is an unrelated flat candle and must
        # not produce a signal. If it produced one, that would mean the
        # detector was reaching back past index -1 to inspect the older wick.
        future = _flat_history(base_price=100.0, count=22, start_ts_ms=0)
        future[20] = _make_candle(
            ts_ms=20 * 60_000,
            o=100.0, h=100.3, l=98.0, c=100.2,
        )
        future_last = _make_candle(
            ts_ms=21 * 60_000,
            o=100.0, h=100.4, l=99.6, c=100.1,  # tiny wicks — should not fire
        )
        future[-1] = future_last
        with _enable_scalp_shadow():
            shifted = evaluator.evaluate_liq_reclaim(
                "BTC/USDT", future, snap, now_ms=21 * 60_000,
            )
        assert shifted is None

    def test_uses_only_prior_lookback_for_range(self):
        """The inside-range envelope must be built from prior bars only.

        Stuff a high spike into the trigger candle itself — if the detector
        included it in prior_high it would always include the trigger close
        trivially. The trigger candle's own high should be excluded.
        """
        evaluator = ScalpSetupEvaluator()
        candles = _flat_history(base_price=100.0, count=21, start_ts_ms=0)
        # Push prior 20 bars to a tight band around 100. Then trigger has
        # close above prior high but high spike inside trigger only.
        for i in range(20):
            candles[i] = _make_candle(
                ts_ms=i * 60_000,
                o=99.5, h=100.5, l=99.4, c=100.0,
            )
        candles[-1] = _make_candle(
            ts_ms=21 * 60_000,
            o=101.0, h=200.0, l=100.0, c=101.5,  # close above prior high
        )
        snap = _snapshot_with_flush(pair="BTC/USDT", flush_ts_ms=21 * 60_000)
        with _enable_scalp_shadow():
            setup = evaluator.evaluate_liq_reclaim(
                "BTC/USDT", candles, snap, now_ms=21 * 60_000,
            )
        # Close (101.5) is above prior_high (100.5) so reclaim fails — no setup.
        # If detector had reached into trigger.high (200) for prior_high it
        # would have returned a setup.
        assert setup is None


# ============================================================
# Signal 2 — sweep + CHoCH (close-back-inside)
# ============================================================

def _sweep_choch_history(
    *,
    base_price: float = 100.0,
    prior_high: float = 100.5,
    prior_low: float = 99.5,
) -> list[Candle]:
    """Return 22 confirmed 5m candles: 20 prior + 1 sweep + 1 confirm slot.

    The slots at indices [-2] and [-1] are placeholders that callers replace
    with the specific sweep/confirm shape they want to test.
    """
    candles: list[Candle] = []
    for i in range(_SWEEP_CHOCH_LOOKBACK_BARS):
        candles.append(_make_candle(
            ts_ms=i * 60_000,
            o=base_price - 0.1,
            h=prior_high,
            l=prior_low,
            c=base_price,
        ))
    # Sweep + confirm slots — populated per test.
    candles.append(_make_candle(
        ts_ms=_SWEEP_CHOCH_LOOKBACK_BARS * 60_000,
        o=base_price, h=base_price + 0.1, l=base_price - 0.1, c=base_price,
    ))
    candles.append(_make_candle(
        ts_ms=(_SWEEP_CHOCH_LOOKBACK_BARS + 1) * 60_000,
        o=base_price, h=base_price + 0.1, l=base_price - 0.1, c=base_price,
    ))
    return candles


class TestSweepChochGate:

    def test_returns_none_when_disabled(self):
        evaluator = ScalpSetupEvaluator()
        candles = _sweep_choch_history()
        result = evaluator.evaluate_sweep_choch("BTC/USDT", candles, None)
        assert result is None

    def test_returns_none_with_too_few_candles(self):
        evaluator = ScalpSetupEvaluator()
        candles = _sweep_choch_history()[:-1]  # 21 bars (need 22)
        with _enable_scalp_shadow():
            result = evaluator.evaluate_sweep_choch("BTC/USDT", candles, None)
        assert result is None


class TestSweepChochSignals:

    def test_short_on_high_sweep_reclaim(self):
        evaluator = ScalpSetupEvaluator()
        candles = _sweep_choch_history()
        # Sweep takes high (100.5), confirm closes back inside with bearish body.
        candles[-2] = _make_candle(
            ts_ms=20 * 60_000,
            o=100.4, h=101.0, l=100.0, c=100.6,
        )
        candles[-1] = _make_candle(
            ts_ms=21 * 60_000,
            o=100.4, h=100.5, l=99.7, c=99.8,  # body 0.6 / range 0.8 = 0.75
        )
        with _enable_scalp_shadow():
            setup = evaluator.evaluate_sweep_choch("BTC/USDT", candles, None)
        assert setup is not None
        assert setup.setup_type == "scalp_sweep_choch_v1"
        assert setup.direction == "short"
        assert setup.entry_price == pytest.approx(99.8)
        # SL 0.15% above, TP2 0.30% below.
        assert setup.sl_price == pytest.approx(99.8 * (1 + 0.0015))
        assert setup.tp2_price == pytest.approx(99.8 * (1 - 0.003))

    def test_long_on_low_sweep_reclaim(self):
        evaluator = ScalpSetupEvaluator()
        candles = _sweep_choch_history()
        candles[-2] = _make_candle(
            ts_ms=20 * 60_000,
            o=99.6, h=100.0, l=99.0, c=99.4,
        )
        candles[-1] = _make_candle(
            ts_ms=21 * 60_000,
            o=99.6, h=100.3, l=99.5, c=100.2,  # body 0.6 / range 0.8 = 0.75
        )
        with _enable_scalp_shadow():
            setup = evaluator.evaluate_sweep_choch("BTC/USDT", candles, None)
        assert setup is not None
        assert setup.direction == "long"
        assert setup.entry_price == pytest.approx(100.2)

    def test_no_signal_when_sweep_did_not_take_extreme(self):
        evaluator = ScalpSetupEvaluator()
        candles = _sweep_choch_history()
        # Sweep candle stays inside the prior range.
        candles[-2] = _make_candle(
            ts_ms=20 * 60_000,
            o=100.2, h=100.4, l=100.0, c=100.1,
        )
        candles[-1] = _make_candle(
            ts_ms=21 * 60_000,
            o=100.4, h=100.5, l=99.7, c=99.8,
        )
        with _enable_scalp_shadow():
            setup = evaluator.evaluate_sweep_choch("BTC/USDT", candles, None)
        assert setup is None

    def test_no_signal_when_confirm_closes_outside(self):
        evaluator = ScalpSetupEvaluator()
        candles = _sweep_choch_history()
        candles[-2] = _make_candle(
            ts_ms=20 * 60_000,
            o=100.4, h=101.0, l=100.0, c=100.8,
        )
        # Close above prior_high (100.5) — momentum continuation, not reclaim.
        candles[-1] = _make_candle(
            ts_ms=21 * 60_000,
            o=100.6, h=101.2, l=100.55, c=101.1,
        )
        with _enable_scalp_shadow():
            setup = evaluator.evaluate_sweep_choch("BTC/USDT", candles, None)
        assert setup is None

    def test_no_signal_when_body_ratio_below_threshold(self):
        evaluator = ScalpSetupEvaluator()
        candles = _sweep_choch_history()
        candles[-2] = _make_candle(
            ts_ms=20 * 60_000,
            o=100.4, h=101.0, l=100.0, c=100.6,
        )
        # Body 0.1 / range 1.0 = 0.10 — well below 0.60 threshold.
        candles[-1] = _make_candle(
            ts_ms=21 * 60_000,
            o=100.3, h=100.5, l=99.5, c=100.2,
        )
        with _enable_scalp_shadow():
            setup = evaluator.evaluate_sweep_choch("BTC/USDT", candles, None)
        assert setup is None
        assert _SWEEP_CHOCH_MIN_BODY_RATIO == 0.60

    def test_no_signal_when_confirm_body_direction_is_wrong(self):
        """High sweep with bullish-bodied confirm must not fire (no real rejection)."""
        evaluator = ScalpSetupEvaluator()
        candles = _sweep_choch_history()
        candles[-2] = _make_candle(
            ts_ms=20 * 60_000,
            o=100.4, h=101.0, l=100.0, c=100.6,
        )
        # Closes back inside but with a bullish body — not a rejection.
        candles[-1] = _make_candle(
            ts_ms=21 * 60_000,
            o=99.8, h=100.5, l=99.7, c=100.4,
        )
        with _enable_scalp_shadow():
            setup = evaluator.evaluate_sweep_choch("BTC/USDT", candles, None)
        assert setup is None

    def test_no_signal_for_unconfirmed_candle(self):
        evaluator = ScalpSetupEvaluator()
        candles = _sweep_choch_history()
        candles[-2] = _make_candle(
            ts_ms=20 * 60_000,
            o=100.4, h=101.0, l=100.0, c=100.6,
        )
        candles[-1] = _make_candle(
            ts_ms=21 * 60_000,
            o=100.4, h=100.5, l=99.7, c=99.8, confirmed=False,
        )
        with _enable_scalp_shadow():
            setup = evaluator.evaluate_sweep_choch("BTC/USDT", candles, None)
        assert setup is None


class TestSweepChochNoLookahead:

    def test_appending_future_candle_breaks_pattern(self):
        """If we append an unrelated bar after the confirm, the pattern is no
        longer at indices [-2]/[-1]. Detector must not reach further back to
        rediscover the original pattern.
        """
        evaluator = ScalpSetupEvaluator()
        candles = _sweep_choch_history()
        candles[-2] = _make_candle(
            ts_ms=20 * 60_000,
            o=100.4, h=101.0, l=100.0, c=100.6,
        )
        candles[-1] = _make_candle(
            ts_ms=21 * 60_000,
            o=100.4, h=100.5, l=99.7, c=99.8,
        )
        with _enable_scalp_shadow():
            baseline = evaluator.evaluate_sweep_choch("BTC/USDT", candles, None)
        assert baseline is not None  # sanity

        # Append an unrelated flat candle.
        candles_plus = candles + [_make_candle(
            ts_ms=22 * 60_000,
            o=99.8, h=99.9, l=99.7, c=99.85,
        )]
        with _enable_scalp_shadow():
            shifted = evaluator.evaluate_sweep_choch("BTC/USDT", candles_plus, None)
        assert shifted is None


# ============================================================
# Signal 3 — vol Z-score + CVD divergence
# ============================================================

def _vol_cvd_history(
    *,
    base_volume: float = 100.0,
    base_price: float = 100.0,
) -> list[Candle]:
    """Return 21 confirmed bars: 20 prior + 1 trigger slot.

    Prior bars hold a flat low-variance volume baseline; tests overwrite the
    trigger candle (`[-1]`) to set its volume + price direction.
    """
    candles: list[Candle] = []
    for i in range(_VOL_CVD_LOOKBACK_BARS):
        # Tiny per-bar volume jitter so std > 0.
        v = base_volume + (0.5 if i % 2 == 0 else -0.5)
        candles.append(_make_candle(
            ts_ms=i * 60_000,
            o=base_price - 0.05, h=base_price + 0.05,
            l=base_price - 0.05, c=base_price,
            volume=v,
        ))
    # Trigger placeholder.
    candles.append(_make_candle(
        ts_ms=_VOL_CVD_LOOKBACK_BARS * 60_000,
        o=base_price, h=base_price, l=base_price, c=base_price,
        volume=base_volume,
    ))
    return candles


def _cvd_snapshot(
    *,
    pair: str = "BTC/USDT",
    cvd_5m: float,
    buy_volume: float = 1000.0,
    sell_volume: float = 1000.0,
) -> MarketSnapshot:
    cvd = CVDSnapshot(
        timestamp=0,
        pair=pair,
        cvd_5m=cvd_5m,
        cvd_15m=cvd_5m,
        cvd_1h=cvd_5m,
        buy_volume=buy_volume,
        sell_volume=sell_volume,
    )
    return MarketSnapshot(pair=pair, timestamp=0, cvd=cvd)


def _ob(*, spread: float = 0.0001) -> dict:
    """Synthetic orderbook snapshot — only `spread` is consumed by Signal 3."""
    return {"spread": spread, "depth_bid_usd": 0.0, "depth_ask_usd": 0.0}


class TestVolCvdGate:

    def test_returns_none_when_disabled(self):
        evaluator = ScalpSetupEvaluator()
        result = evaluator.evaluate_vol_cvd_divergence(
            "BTC/USDT", _vol_cvd_history(), _cvd_snapshot(cvd_5m=500),
            orderbook=_ob(),
        )
        assert result is None

    def test_returns_none_with_too_few_candles(self):
        evaluator = ScalpSetupEvaluator()
        short_history = _vol_cvd_history()[:_VOL_CVD_LOOKBACK_BARS]  # 20, need 21
        with _enable_scalp_shadow():
            result = evaluator.evaluate_vol_cvd_divergence(
                "BTC/USDT", short_history, _cvd_snapshot(cvd_5m=500),
                orderbook=_ob(),
            )
        assert result is None

    def test_returns_none_without_snapshot_or_cvd(self):
        evaluator = ScalpSetupEvaluator()
        with _enable_scalp_shadow():
            assert evaluator.evaluate_vol_cvd_divergence(
                "BTC/USDT", _vol_cvd_history(), None, orderbook=_ob(),
            ) is None
            empty_snap = MarketSnapshot(pair="BTC/USDT", timestamp=0)
            assert evaluator.evaluate_vol_cvd_divergence(
                "BTC/USDT", _vol_cvd_history(), empty_snap, orderbook=_ob(),
            ) is None

    def test_returns_none_without_orderbook(self):
        evaluator = ScalpSetupEvaluator()
        with _enable_scalp_shadow():
            result = evaluator.evaluate_vol_cvd_divergence(
                "BTC/USDT", _vol_cvd_history(), _cvd_snapshot(cvd_5m=500),
                orderbook=None,
            )
        assert result is None


class TestVolCvdFilters:

    def _trigger_history(
        self, *, trigger_open: float, trigger_close: float, trigger_volume: float,
    ) -> list[Candle]:
        candles = _vol_cvd_history()
        candles[-1] = _make_candle(
            ts_ms=_VOL_CVD_LOOKBACK_BARS * 60_000,
            o=trigger_open,
            h=max(trigger_open, trigger_close) + 0.05,
            l=min(trigger_open, trigger_close) - 0.05,
            c=trigger_close,
            volume=trigger_volume,
        )
        return candles

    def test_no_signal_when_spread_too_wide(self):
        evaluator = ScalpSetupEvaluator()
        candles = self._trigger_history(
            trigger_open=100.0, trigger_close=100.5, trigger_volume=500.0,
        )
        # 5 bps > 2 bps cap.
        wide = _ob(spread=0.0005)
        with _enable_scalp_shadow():
            result = evaluator.evaluate_vol_cvd_divergence(
                "BTC/USDT", candles, _cvd_snapshot(cvd_5m=-500),
                orderbook=wide,
            )
        assert result is None
        assert _VOL_CVD_MAX_SPREAD == 0.0002

    def test_no_signal_when_z_below_threshold(self):
        evaluator = ScalpSetupEvaluator()
        # Trigger volume only marginally above baseline.
        candles = self._trigger_history(
            trigger_open=100.0, trigger_close=100.5, trigger_volume=101.0,
        )
        with _enable_scalp_shadow():
            result = evaluator.evaluate_vol_cvd_divergence(
                "BTC/USDT", candles, _cvd_snapshot(cvd_5m=-500),
                orderbook=_ob(),
            )
        assert result is None
        assert _VOL_CVD_Z_THRESHOLD == 3.0

    def test_no_signal_when_cvd_imbalance_too_small(self):
        evaluator = ScalpSetupEvaluator()
        candles = self._trigger_history(
            trigger_open=100.0, trigger_close=100.5, trigger_volume=500.0,
        )
        # |cvd_5m| / total_flow = 100 / 2000 = 0.05 < 0.20.
        snap = _cvd_snapshot(cvd_5m=-100, buy_volume=1000, sell_volume=1000)
        with _enable_scalp_shadow():
            result = evaluator.evaluate_vol_cvd_divergence(
                "BTC/USDT", candles, snap, orderbook=_ob(),
            )
        assert result is None
        assert _VOL_CVD_MIN_IMBALANCE == 0.20

    def test_no_signal_when_cvd_aligned_with_candle(self):
        evaluator = ScalpSetupEvaluator()
        # Bullish candle + bullish CVD = aligned, no divergence.
        candles = self._trigger_history(
            trigger_open=100.0, trigger_close=100.5, trigger_volume=500.0,
        )
        with _enable_scalp_shadow():
            result = evaluator.evaluate_vol_cvd_divergence(
                "BTC/USDT", candles, _cvd_snapshot(cvd_5m=500),
                orderbook=_ob(),
            )
        assert result is None

    def test_no_signal_on_doji_trigger(self):
        evaluator = ScalpSetupEvaluator()
        # close == open — no clear price direction.
        candles = self._trigger_history(
            trigger_open=100.0, trigger_close=100.0, trigger_volume=500.0,
        )
        with _enable_scalp_shadow():
            result = evaluator.evaluate_vol_cvd_divergence(
                "BTC/USDT", candles, _cvd_snapshot(cvd_5m=500),
                orderbook=_ob(),
            )
        assert result is None


class TestVolCvdSignals:

    def test_long_on_bearish_candle_with_bullish_cvd(self):
        evaluator = ScalpSetupEvaluator()
        candles = _vol_cvd_history()
        # Bearish trigger candle (close < open), big volume.
        candles[-1] = _make_candle(
            ts_ms=_VOL_CVD_LOOKBACK_BARS * 60_000,
            o=100.5, h=100.6, l=99.7, c=99.8, volume=500.0,
        )
        snap = _cvd_snapshot(cvd_5m=600, buy_volume=1300, sell_volume=700)
        with _enable_scalp_shadow():
            setup = evaluator.evaluate_vol_cvd_divergence(
                "BTC/USDT", candles, snap, orderbook=_ob(),
            )
        assert setup is not None
        assert setup.setup_type == "scalp_vol_cvd_div_v1"
        assert setup.direction == "long"
        assert setup.entry_price == pytest.approx(99.8)
        # TP 0.50% / SL 0.20% per defaults.
        assert setup.sl_price == pytest.approx(99.8 * (1 - 0.002))
        assert setup.tp2_price == pytest.approx(99.8 * (1 + 0.005))

    def test_short_on_bullish_candle_with_bearish_cvd(self):
        evaluator = ScalpSetupEvaluator()
        candles = _vol_cvd_history()
        candles[-1] = _make_candle(
            ts_ms=_VOL_CVD_LOOKBACK_BARS * 60_000,
            o=99.5, h=100.3, l=99.4, c=100.2, volume=500.0,
        )
        snap = _cvd_snapshot(cvd_5m=-600, buy_volume=700, sell_volume=1300)
        with _enable_scalp_shadow():
            setup = evaluator.evaluate_vol_cvd_divergence(
                "BTC/USDT", candles, snap, orderbook=_ob(),
            )
        assert setup is not None
        assert setup.direction == "short"
        assert setup.entry_price == pytest.approx(100.2)


class TestVolCvdNoLookahead:

    def test_appending_future_candle_changes_trigger(self):
        evaluator = ScalpSetupEvaluator()
        candles = _vol_cvd_history()
        candles[-1] = _make_candle(
            ts_ms=_VOL_CVD_LOOKBACK_BARS * 60_000,
            o=100.5, h=100.6, l=99.7, c=99.8, volume=500.0,
        )
        snap = _cvd_snapshot(cvd_5m=600, buy_volume=1300, sell_volume=700)
        with _enable_scalp_shadow():
            baseline = evaluator.evaluate_vol_cvd_divergence(
                "BTC/USDT", candles, snap, orderbook=_ob(),
            )
        assert baseline is not None  # sanity

        # Append a flat low-volume bar — the new trigger has no spike, so the
        # detector must produce no signal even though the spike still sits in
        # the prior window.
        candles_plus = candles + [_make_candle(
            ts_ms=(_VOL_CVD_LOOKBACK_BARS + 1) * 60_000,
            o=99.8, h=99.85, l=99.75, c=99.82, volume=100.0,
        )]
        with _enable_scalp_shadow():
            shifted = evaluator.evaluate_vol_cvd_divergence(
                "BTC/USDT", candles_plus, snap, orderbook=_ob(),
            )
        assert shifted is None


# ============================================================
# Signal 4 — funding extreme + flat price
# ============================================================

def _flat_window(
    *, base_price: float, count: int, range_pct: float,
) -> list[Candle]:
    """Return `count` confirmed candles whose combined high-low spans
    approximately `range_pct` of base_price.
    """
    half = base_price * range_pct / 2
    candles: list[Candle] = []
    for i in range(count):
        candles.append(_make_candle(
            ts_ms=i * 60_000,
            o=base_price - 0.05,
            h=base_price + half,
            l=base_price - half,
            c=base_price,
        ))
    return candles


def _funding_snapshot(*, pair: str = "BTC/USDT", rate: float) -> MarketSnapshot:
    funding = FundingRate(
        timestamp=0,
        pair=pair,
        rate=rate,
        next_rate=rate,
        next_funding_time=0,
    )
    return MarketSnapshot(pair=pair, timestamp=0, funding=funding)


class TestFundingExtremeGate:

    def test_returns_none_when_disabled(self):
        evaluator = ScalpSetupEvaluator()
        candles = _flat_window(
            base_price=100.0, count=_FUNDING_FLAT_LOOKBACK_BARS, range_pct=0.001,
        )
        snap = _funding_snapshot(rate=0.001)
        result = evaluator.evaluate_funding_extreme("BTC/USDT", candles, snap)
        assert result is None

    def test_returns_none_with_too_few_candles(self):
        evaluator = ScalpSetupEvaluator()
        candles = _flat_window(
            base_price=100.0, count=_FUNDING_FLAT_LOOKBACK_BARS - 1, range_pct=0.001,
        )
        snap = _funding_snapshot(rate=0.001)
        with _enable_scalp_shadow():
            result = evaluator.evaluate_funding_extreme("BTC/USDT", candles, snap)
        assert result is None

    def test_returns_none_without_funding(self):
        evaluator = ScalpSetupEvaluator()
        candles = _flat_window(
            base_price=100.0, count=_FUNDING_FLAT_LOOKBACK_BARS, range_pct=0.001,
        )
        with _enable_scalp_shadow():
            assert evaluator.evaluate_funding_extreme(
                "BTC/USDT", candles, None,
            ) is None
            empty_snap = MarketSnapshot(pair="BTC/USDT", timestamp=0)
            assert evaluator.evaluate_funding_extreme(
                "BTC/USDT", candles, empty_snap,
            ) is None


class TestFundingExtremeFilters:

    def test_no_signal_when_funding_below_threshold(self):
        evaluator = ScalpSetupEvaluator()
        candles = _flat_window(
            base_price=100.0, count=_FUNDING_FLAT_LOOKBACK_BARS, range_pct=0.001,
        )
        # 0.0003 < 0.0005 threshold.
        snap = _funding_snapshot(rate=0.0003)
        with _enable_scalp_shadow():
            result = evaluator.evaluate_funding_extreme("BTC/USDT", candles, snap)
        assert result is None
        assert _FUNDING_RATE_THRESHOLD == 0.0005

    def test_no_signal_when_range_too_wide(self):
        evaluator = ScalpSetupEvaluator()
        # 0.5% range > 0.3% threshold.
        candles = _flat_window(
            base_price=100.0, count=_FUNDING_FLAT_LOOKBACK_BARS, range_pct=0.005,
        )
        snap = _funding_snapshot(rate=0.001)
        with _enable_scalp_shadow():
            result = evaluator.evaluate_funding_extreme("BTC/USDT", candles, snap)
        assert result is None
        assert _FUNDING_FLAT_RANGE_THRESHOLD == 0.003


class TestFundingExtremeSignals:

    def test_short_on_positive_funding_extreme(self):
        evaluator = ScalpSetupEvaluator()
        candles = _flat_window(
            base_price=100.0, count=_FUNDING_FLAT_LOOKBACK_BARS, range_pct=0.001,
        )
        # +0.10% funding — longs paying = crowded long => short.
        snap = _funding_snapshot(rate=0.001)
        with _enable_scalp_shadow():
            setup = evaluator.evaluate_funding_extreme("BTC/USDT", candles, snap)
        assert setup is not None
        assert setup.setup_type == "scalp_funding_extreme_v1"
        assert setup.direction == "short"
        assert setup.entry_price == pytest.approx(100.0)
        # TP 0.80% / SL 0.30% per defaults.
        assert setup.sl_price == pytest.approx(100.0 * (1 + 0.003))
        assert setup.tp2_price == pytest.approx(100.0 * (1 - 0.008))

    def test_long_on_negative_funding_extreme(self):
        evaluator = ScalpSetupEvaluator()
        candles = _flat_window(
            base_price=100.0, count=_FUNDING_FLAT_LOOKBACK_BARS, range_pct=0.001,
        )
        snap = _funding_snapshot(rate=-0.001)
        with _enable_scalp_shadow():
            setup = evaluator.evaluate_funding_extreme("BTC/USDT", candles, snap)
        assert setup is not None
        assert setup.direction == "long"
        assert setup.entry_price == pytest.approx(100.0)
        assert setup.sl_price == pytest.approx(100.0 * (1 - 0.003))
        assert setup.tp2_price == pytest.approx(100.0 * (1 + 0.008))


class TestFundingExtremeNoLookahead:

    def test_appending_future_candle_changes_window(self):
        evaluator = ScalpSetupEvaluator()
        candles = _flat_window(
            base_price=100.0, count=_FUNDING_FLAT_LOOKBACK_BARS, range_pct=0.001,
        )
        snap = _funding_snapshot(rate=0.001)
        with _enable_scalp_shadow():
            baseline = evaluator.evaluate_funding_extreme("BTC/USDT", candles, snap)
        assert baseline is not None  # sanity

        # Append a wide-range bar — the rolling window is now (-N..-1) which
        # includes the new bar. Range should bust the threshold and the
        # signal must vanish even though the prior tight bars remain in the
        # full candles list.
        wide = _make_candle(
            ts_ms=_FUNDING_FLAT_LOOKBACK_BARS * 60_000,
            o=100.0, h=102.0, l=98.0, c=100.0,
        )
        with _enable_scalp_shadow():
            shifted = evaluator.evaluate_funding_extreme(
                "BTC/USDT", candles + [wide], snap,
            )
        assert shifted is None
