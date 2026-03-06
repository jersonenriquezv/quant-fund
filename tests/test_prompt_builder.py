"""Tests for ai_service.prompt_builder — system + evaluation prompt construction."""

import time
import pytest
from ai_service.prompt_builder import PromptBuilder
from shared.models import (
    TradeSetup, MarketSnapshot, FundingRate, OpenInterest,
    CVDSnapshot, LiquidationEvent, WhaleMovement,
)
from config.settings import settings


@pytest.fixture
def builder():
    return PromptBuilder()


def _make_setup(direction="long", pair="BTC/USDT", confluences=None) -> TradeSetup:
    return TradeSetup(
        timestamp=int(time.time() * 1000),
        pair=pair,
        direction=direction,
        setup_type="setup_a",
        entry_price=50000.0,
        sl_price=49000.0,
        tp1_price=51000.0,
        tp2_price=52000.0,
        tp3_price=53000.0,
        confluences=confluences if confluences is not None else ["choch", "ob", "sweep"],
        htf_bias="bullish",
        ob_timeframe="15m",
    )


def _make_snapshot(
    funding_rate=0.0001,
    oi_usd=1_000_000.0,
    cvd_15m=100.0,
    liquidations=None,
    whales=None,
) -> MarketSnapshot:
    ts = int(time.time() * 1000)
    funding = FundingRate(
        timestamp=ts, pair="BTC/USDT", rate=funding_rate,
        next_rate=funding_rate, next_funding_time=ts + 28800000,
    )
    oi = OpenInterest(
        timestamp=ts, pair="BTC/USDT",
        oi_contracts=1000, oi_base=10.0, oi_usd=oi_usd,
    )
    cvd = CVDSnapshot(
        timestamp=ts, pair="BTC/USDT",
        cvd_5m=cvd_15m / 3, cvd_15m=cvd_15m, cvd_1h=cvd_15m * 4,
        buy_volume=500.0, sell_volume=400.0,
    )
    return MarketSnapshot(
        pair="BTC/USDT", timestamp=ts,
        funding=funding, oi=oi, cvd=cvd,
        recent_liquidations=liquidations or [],
        whale_movements=whales or [],
    )


# ============================================================
# System prompt
# ============================================================

class TestSystemPrompt:

    def test_contains_json_format(self, builder):
        prompt = builder.build_system_prompt()
        assert '"confidence"' in prompt
        assert '"approved"' in prompt
        assert '"reasoning"' in prompt

    def test_contains_decision_guidelines(self, builder):
        prompt = builder.build_system_prompt()
        assert "0.6" in prompt
        assert "No quota" in prompt

    def test_contains_critical_rules(self, builder):
        prompt = builder.build_system_prompt()
        assert "CRITICAL RULES" in prompt
        assert "Capital preservation" in prompt

    def test_threshold_follows_settings(self, builder):
        """System prompt threshold must reflect current AI_MIN_CONFIDENCE."""
        original = settings.AI_MIN_CONFIDENCE
        try:
            settings.AI_MIN_CONFIDENCE = 0.50
            prompt = builder.build_system_prompt()
            assert "0.5" in prompt
            assert "0.6" not in prompt
        finally:
            settings.AI_MIN_CONFIDENCE = original


# ============================================================
# Evaluation prompt — setup data
# ============================================================

class TestEvaluationPrompt:

    def test_includes_setup_data(self, builder):
        setup = _make_setup()
        snapshot = _make_snapshot()
        prompt = builder.build_evaluation_prompt(setup, snapshot, {})

        assert "BTC/USDT" in prompt
        assert "long" in prompt
        assert "50000" in prompt
        assert "49000" in prompt
        assert "choch" in prompt

    def test_includes_funding(self, builder):
        setup = _make_setup()
        snapshot = _make_snapshot(funding_rate=0.0001)
        prompt = builder.build_evaluation_prompt(setup, snapshot, {})

        assert "Funding Rate" in prompt
        assert "0.0001" in prompt

    def test_handles_none_funding(self, builder):
        setup = _make_setup()
        snapshot = MarketSnapshot(
            pair="BTC/USDT", timestamp=int(time.time() * 1000),
            funding=None,
        )
        prompt = builder.build_evaluation_prompt(setup, snapshot, {})
        assert "Not available" in prompt

    def test_handles_none_oi(self, builder):
        setup = _make_setup()
        snapshot = MarketSnapshot(
            pair="BTC/USDT", timestamp=int(time.time() * 1000),
            oi=None,
        )
        prompt = builder.build_evaluation_prompt(setup, snapshot, {})
        assert "Not available" in prompt

    def test_handles_none_cvd(self, builder):
        setup = _make_setup()
        snapshot = MarketSnapshot(
            pair="BTC/USDT", timestamp=int(time.time() * 1000),
            cvd=None,
        )
        prompt = builder.build_evaluation_prompt(setup, snapshot, {})
        assert "Not available" in prompt

    def test_includes_liquidations(self, builder):
        ts = int(time.time() * 1000)
        liqs = [
            LiquidationEvent(
                timestamp=ts, pair="BTC/USDT", side="long",
                size_usd=50000, price=49500, source="oi_proxy",
            ),
        ]
        setup = _make_setup()
        snapshot = _make_snapshot(liquidations=liqs)
        prompt = builder.build_evaluation_prompt(setup, snapshot, {})

        assert "Liquidations" in prompt
        assert "50,000" in prompt

    def test_no_liquidations(self, builder):
        setup = _make_setup()
        snapshot = _make_snapshot(liquidations=[])
        prompt = builder.build_evaluation_prompt(setup, snapshot, {})
        assert "No liquidation cascades detected" in prompt

    def test_includes_whale_movements(self, builder):
        ts = int(time.time() * 1000)
        whales = [
            WhaleMovement(
                timestamp=ts, wallet="0xabc", action="exchange_deposit",
                amount=150.0, exchange="Binance", significance="high", chain="ETH",
            ),
        ]
        setup = _make_setup()
        snapshot = _make_snapshot(whales=whales)
        prompt = builder.build_evaluation_prompt(setup, snapshot, {})

        assert "150.0 ETH" in prompt
        assert "Binance" in prompt

    def test_extreme_funding_flagged(self, builder):
        setup = _make_setup()
        snapshot = _make_snapshot(funding_rate=0.0005)
        prompt = builder.build_evaluation_prompt(setup, snapshot, {})
        assert "EXTREME" in prompt

    def test_price_context_included(self, builder):
        setup = _make_setup()
        snapshot = _make_snapshot()
        candles_ctx = {
            "1h": {"latest_close": 50100, "prev_close": 50000, "pct_change": 0.2},
        }
        prompt = builder.build_evaluation_prompt(setup, snapshot, candles_ctx)
        assert "Price Context" in prompt
        assert "50100" in prompt

    def test_non_exchange_whale_labels(self, builder):
        ts = int(time.time() * 1000)
        whales = [
            WhaleMovement(
                timestamp=ts, wallet="0xabc", action="transfer_out",
                amount=500.0, exchange="0xde12...ab34", significance="medium", chain="ETH",
            ),
            WhaleMovement(
                timestamp=ts, wallet="0xabc", action="transfer_in",
                amount=300.0, exchange="0xff00...1234", significance="medium", chain="BTC",
            ),
        ]
        setup = _make_setup()
        snapshot = _make_snapshot(whales=whales)
        prompt = builder.build_evaluation_prompt(setup, snapshot, {})

        assert "transferred out to" in prompt
        assert "received from" in prompt
        assert "0xde12...ab34" in prompt

    def test_ends_with_json_instruction(self, builder):
        setup = _make_setup()
        snapshot = _make_snapshot()
        prompt = builder.build_evaluation_prompt(setup, snapshot, {})
        assert prompt.strip().endswith("Evaluate this setup and respond with JSON only.")


# ============================================================
# Profile-aware prompts
# ============================================================

class TestProfileAwarePrompts:

    def test_no_profile_section_in_default(self, builder):
        """Default profile does NOT include any profile-specific section."""
        original = settings.STRATEGY_PROFILE
        try:
            settings.STRATEGY_PROFILE = "default"
            setup = _make_setup()
            snapshot = _make_snapshot()
            prompt = builder.build_evaluation_prompt(setup, snapshot, {})

            assert "Active Profile" not in prompt
        finally:
            settings.STRATEGY_PROFILE = original

    def test_htf_bias_not_annotated_as_informational(self, builder):
        """HTF Bias line should NOT have informational annotation."""
        setup = _make_setup(direction="short")
        snapshot = _make_snapshot()
        prompt = builder.build_evaluation_prompt(setup, snapshot, {})

        assert "HTF Bias: bullish" in prompt
        assert "informational" not in prompt


# ============================================================
# Dynamic confluence formatting
# ============================================================

class TestConfluenceFormatting:

    def test_ob_volume_supporting(self, builder):
        setup = _make_setup(confluences=["ob_volume_2.1x"])
        snapshot = _make_snapshot()
        prompt = builder.build_evaluation_prompt(setup, snapshot, {})
        assert "[SUPPORTING]" in prompt
        assert "OB volume 2.1x" in prompt

    def test_ob_volume_context(self, builder):
        setup = _make_setup(confluences=["ob_volume_1.2x"])
        snapshot = _make_snapshot()
        prompt = builder.build_evaluation_prompt(setup, snapshot, {})
        assert "[CONTEXT]" in prompt
        assert "< 1.5x moderate" in prompt

    def test_sweep_volume_supporting(self, builder):
        setup = _make_setup(confluences=["sweep_volume_2.5x"])
        snapshot = _make_snapshot()
        prompt = builder.build_evaluation_prompt(setup, snapshot, {})
        assert "[SUPPORTING]" in prompt
        assert ">= 2x institutional" in prompt

    def test_liquidations_usd(self, builder):
        setup = _make_setup(confluences=["liquidations_usd_50000"])
        snapshot = _make_snapshot()
        prompt = builder.build_evaluation_prompt(setup, snapshot, {})
        assert "$50,000" in prompt

    def test_malformed_ob_volume_no_crash(self, builder):
        setup = _make_setup(confluences=["ob_volume_", "ob_volume_abc"])
        snapshot = _make_snapshot()
        prompt = builder.build_evaluation_prompt(setup, snapshot, {})
        assert "[CONTEXT]" in prompt

    def test_malformed_sweep_volume_no_crash(self, builder):
        setup = _make_setup(confluences=["sweep_volume_bad"])
        snapshot = _make_snapshot()
        prompt = builder.build_evaluation_prompt(setup, snapshot, {})
        assert "[CONTEXT]" in prompt

    def test_malformed_liquidations_no_crash(self, builder):
        setup = _make_setup(confluences=["liquidations_usd_"])
        snapshot = _make_snapshot()
        prompt = builder.build_evaluation_prompt(setup, snapshot, {})
        assert "[CONTEXT]" in prompt

    def test_order_block_and_fvg(self, builder):
        setup = _make_setup(confluences=["order_block_15m", "fvg_5m"])
        snapshot = _make_snapshot()
        prompt = builder.build_evaluation_prompt(setup, snapshot, {})
        assert "Fresh order block on 15m" in prompt
        assert "Fair value gap on 5m" in prompt

    def test_labeled_confluences(self, builder):
        setup = _make_setup(confluences=["choch_bullish", "pd_zone_discount"])
        snapshot = _make_snapshot()
        prompt = builder.build_evaluation_prompt(setup, snapshot, {})
        assert "[SUPPORTING]" in prompt
        assert "CHoCH" in prompt

    def test_unknown_confluence_falls_to_context(self, builder):
        setup = _make_setup(confluences=["some_unknown_thing"])
        snapshot = _make_snapshot()
        prompt = builder.build_evaluation_prompt(setup, snapshot, {})
        assert "[CONTEXT] some_unknown_thing" in prompt
