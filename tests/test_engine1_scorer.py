"""Tests for the engine1 meta-label scorer + its main.py logging hook.

Closes the Phase 1 manual check ("ENGINE1_LIVE_SCORE appears on engine1
emissions") deterministically, without waiting for a live emission: feed a
synthetic engine1 setup through `_engine1_score_log` and assert it scores +
logs without throwing. Numeric parity vs the offline analysis is covered
separately by scripts/engine1_scorer_parity.py.

Skips gracefully if lightgbm / the frozen model are unavailable (e.g. a CI
runner without the native OpenMP lib), so this never red-bars an unrelated PR.
"""
import sys
import time

import pytest
from unittest.mock import patch

from shared.models import TradeSetup

# Skip the whole module if the model + its deps can't load in this env.
engine1_scorer = pytest.importorskip(
    "strategy_service.engines.engine1_scorer",
    reason="engine1_scorer import failed (lightgbm/libgomp missing?)",
)
try:
    engine1_scorer._load_model()
    _MODEL_OK = True
except Exception:  # noqa: BLE001 — model file or native lib unavailable
    _MODEL_OK = False

pytestmark = pytest.mark.skipif(
    not _MODEL_OK, reason="frozen engine1 model not loadable in this env"
)


def _make_engine1_setup(pair="ETH/USDT", direction="short") -> TradeSetup:
    return TradeSetup(
        timestamp=int(time.time() * 1000),
        pair=pair, direction=direction, setup_type="engine1_trend_pullback",
        entry_price=2000.0, sl_price=2020.0,
        tp1_price=1960.0, tp2_price=1920.0,
        confluences=["trend", "pullback"],
        htf_bias="bearish", ob_timeframe="1h",
    )


# A minimal feature dict — the scorer reindexes to the model's columns and lets
# LightGBM treat missing ones as NaN, so a sparse dict still scores.
_MIN_FEATURES = {
    "pair": "ETH/USDT", "direction": "short", "htf_bias": "bearish",
    "rr_ratio": 2.0, "risk_distance_pct": 1.0, "entry_distance_pct": 0.2,
    "outcome_type": "shadow_sl",  # ignored by transform (dropped), keeps parity
}


def test_score_features_returns_probability():
    score = engine1_scorer.score_features(_MIN_FEATURES)
    assert isinstance(score, float)
    assert 0.0 <= score <= 1.0


def test_passes_cutoff_uses_settings():
    from config.settings import settings
    cut = settings.ENGINE1_SCORE_CUTOFF
    assert engine1_scorer.passes_cutoff(cut) is True
    assert engine1_scorer.passes_cutoff(cut + 0.01) is True
    assert engine1_scorer.passes_cutoff(cut - 0.01) is False


def test_score_is_deterministic():
    s1 = engine1_scorer.score_features(_MIN_FEATURES)
    s2 = engine1_scorer.score_features(dict(_MIN_FEATURES))
    assert s1 == s2


def _import_main():
    """Import main.py with a no-op logger (same trick as test_main_pipeline)."""
    def _noop_logger(name=""):
        from loguru import logger
        return logger

    if "main" in sys.modules:
        del sys.modules["main"]
    with patch("shared.logger.setup_logger", side_effect=_noop_logger):
        if "shared.notifier" in sys.modules:
            del sys.modules["shared.notifier"]
        import main
    return main


def test_engine1_score_log_fires_and_logs():
    """The pipeline hook scores an engine1 setup and emits ENGINE1_LIVE_SCORE.

    Captures via a temporary loguru sink (not capsys/caplog): the bot logs
    through loguru, whose stderr sink is bound once at startup and so is not
    reliably visible to pytest's stream capture across the full suite.
    """
    from loguru import logger

    main = _import_main()
    setup = _make_engine1_setup()

    messages: list[str] = []
    sink_id = logger.add(messages.append, format="{message}", level="INFO")
    try:
        # _data_service is None at import → _emit_metric no-ops; safe to call.
        main._engine1_score_log(setup, _MIN_FEATURES)
    finally:
        logger.remove(sink_id)

    hits = [m for m in messages if "ENGINE1_LIVE_SCORE" in m]
    assert hits, f"expected an ENGINE1_LIVE_SCORE log line, got: {messages}"
    assert "score=" in hits[0] and "eligible=" in hits[0]


def test_engine1_score_log_swallows_errors(caplog):
    """A scoring failure must not propagate into the pipeline (log-only hook)."""
    main = _import_main()
    setup = _make_engine1_setup()
    with patch.object(engine1_scorer, "score_features", side_effect=RuntimeError("boom")):
        main._engine1_score_log(setup, _MIN_FEATURES)  # must not raise
