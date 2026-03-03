"""Tests for strategy_service.market_structure — Swing, BOS, CHoCH, Trend."""

import pytest
from tests.conftest import make_candle, make_candle_series
from strategy_service.market_structure import (
    MarketStructureAnalyzer, SwingPoint, StructureBreak, MarketStructureState,
)
from config.settings import settings


class TestSwingDetection:
    """Test swing high/low detection."""

    def test_finds_swing_high(self):
        """A candle whose high is highest in the window should be a swing high."""
        analyzer = MarketStructureAnalyzer()
        lookback = settings.SWING_LOOKBACK  # 5

        # Create candles where index 10 has the highest high
        candles = []
        for i in range(25):
            if i == 10:
                c = make_candle(high=150.0, low=99.0, close=100.0,
                                timestamp=1000 + i * 1000)
            else:
                c = make_candle(high=101.0, low=99.0, close=100.0,
                                timestamp=1000 + i * 1000)
            candles.append(c)

        state = analyzer.analyze(candles, "BTC/USDT", "15m")
        high_prices = [sh.price for sh in state.swing_highs]
        assert 150.0 in high_prices

    def test_finds_swing_low(self):
        """A candle whose low is lowest in the window should be a swing low."""
        analyzer = MarketStructureAnalyzer()

        candles = []
        for i in range(25):
            if i == 10:
                c = make_candle(high=101.0, low=50.0, close=100.0,
                                timestamp=1000 + i * 1000)
            else:
                c = make_candle(high=101.0, low=99.0, close=100.0,
                                timestamp=1000 + i * 1000)
            candles.append(c)

        state = analyzer.analyze(candles, "BTC/USDT", "15m")
        low_prices = [sl.price for sl in state.swing_lows]
        assert 50.0 in low_prices

    def test_insufficient_candles_returns_undefined(self):
        """Less than 2*SWING_LOOKBACK+1 candles → undefined trend."""
        analyzer = MarketStructureAnalyzer()
        candles = [make_candle(timestamp=1000 + i * 1000) for i in range(5)]

        state = analyzer.analyze(candles, "BTC/USDT", "15m")
        assert state.trend == "undefined"
        assert state.swing_highs == []
        assert state.swing_lows == []

    def test_no_swing_when_equal_highs(self):
        """Equal highs in the window should not produce a swing high."""
        analyzer = MarketStructureAnalyzer()

        candles = []
        for i in range(25):
            c = make_candle(high=100.0, low=99.0, close=99.5,
                            timestamp=1000 + i * 1000)
            candles.append(c)

        state = analyzer.analyze(candles, "BTC/USDT", "15m")
        assert len(state.swing_highs) == 0


class TestBOSDetection:
    """Test Break of Structure detection with 0.1% filter."""

    def _make_bos_scenario(self, direction="bullish"):
        """Create candles with a clear swing followed by a break."""
        candles = []
        ts = 1_000_000_000_000

        if direction == "bullish":
            # Flat candles, then a swing high at index 10, then break above it
            for i in range(25):
                if i == 10:
                    # Swing high
                    c = make_candle(open=100.0, high=110.0, low=99.0,
                                    close=100.0, timestamp=ts + i * 60000)
                elif i == 20:
                    # Break above swing high by > 0.1%
                    target = 110.0 * (1 + settings.BOS_CONFIRMATION_PCT + 0.001)
                    c = make_candle(open=109.0, high=target + 1, low=108.0,
                                    close=target, timestamp=ts + i * 60000)
                else:
                    c = make_candle(open=100.0, high=101.0, low=99.0,
                                    close=100.0, timestamp=ts + i * 60000)
                candles.append(c)
        else:
            # Swing low at index 10, break below it
            for i in range(25):
                if i == 10:
                    c = make_candle(open=100.0, high=101.0, low=90.0,
                                    close=100.0, timestamp=ts + i * 60000)
                elif i == 20:
                    target = 90.0 * (1 - settings.BOS_CONFIRMATION_PCT - 0.001)
                    c = make_candle(open=91.0, high=92.0, low=target - 1,
                                    close=target, timestamp=ts + i * 60000)
                else:
                    c = make_candle(open=100.0, high=101.0, low=99.0,
                                    close=100.0, timestamp=ts + i * 60000)
                candles.append(c)

        return candles

    def test_bullish_bos_detected(self):
        """Close > swing high * 1.001 should produce a bullish BOS."""
        analyzer = MarketStructureAnalyzer()
        candles = self._make_bos_scenario("bullish")
        state = analyzer.analyze(candles, "BTC/USDT", "15m")

        bullish_breaks = [
            b for b in state.structure_breaks if b.direction == "bullish"
        ]
        assert len(bullish_breaks) > 0
        assert state.trend == "bullish"

    def test_bearish_bos_detected(self):
        """Close < swing low * 0.999 should produce a bearish BOS."""
        analyzer = MarketStructureAnalyzer()
        candles = self._make_bos_scenario("bearish")
        state = analyzer.analyze(candles, "BTC/USDT", "15m")

        bearish_breaks = [
            b for b in state.structure_breaks if b.direction == "bearish"
        ]
        assert len(bearish_breaks) > 0
        assert state.trend == "bearish"

    def test_wick_only_break_rejected(self):
        """Wick exceeding swing level but close staying inside should NOT count."""
        analyzer = MarketStructureAnalyzer()
        ts = 1_000_000_000_000

        candles = []
        for i in range(25):
            if i == 10:
                # Swing high at 110
                c = make_candle(open=100.0, high=110.0, low=99.0,
                                close=100.0, timestamp=ts + i * 60000)
            elif i == 20:
                # Wick goes above 110 but close stays below
                c = make_candle(open=108.0, high=112.0, low=107.0,
                                close=109.0, timestamp=ts + i * 60000)
            else:
                c = make_candle(open=100.0, high=101.0, low=99.0,
                                close=100.0, timestamp=ts + i * 60000)
            candles.append(c)

        state = analyzer.analyze(candles, "BTC/USDT", "15m")
        # Close at 109 < 110 * 1.001 = 110.11, so no bullish break through the 110 swing
        bullish_breaks_through_110 = [
            b for b in state.structure_breaks
            if b.direction == "bullish" and b.broken_level == 110.0
        ]
        assert len(bullish_breaks_through_110) == 0

    def test_break_below_threshold_rejected(self):
        """Close exceeding swing by less than 0.1% should NOT count."""
        analyzer = MarketStructureAnalyzer()
        ts = 1_000_000_000_000

        candles = []
        for i in range(25):
            if i == 10:
                c = make_candle(open=100.0, high=110.0, low=99.0,
                                close=100.0, timestamp=ts + i * 60000)
            elif i == 20:
                # Close barely above 110 but below 0.1% threshold (110.11)
                c = make_candle(open=109.0, high=111.0, low=108.0,
                                close=110.05, timestamp=ts + i * 60000)
            else:
                c = make_candle(open=100.0, high=101.0, low=99.0,
                                close=100.0, timestamp=ts + i * 60000)
            candles.append(c)

        state = analyzer.analyze(candles, "BTC/USDT", "15m")
        bullish_breaks_through_110 = [
            b for b in state.structure_breaks
            if b.direction == "bullish" and b.broken_level == 110.0
        ]
        assert len(bullish_breaks_through_110) == 0


class TestCHoCH:
    """Test Change of Character detection."""

    def test_choch_on_trend_reversal(self):
        """A break opposite to current trend should be a CHoCH."""
        analyzer = MarketStructureAnalyzer()
        ts = 1_000_000_000_000

        # Build scenario: establish bullish trend, then bearish CHoCH
        candles = []
        for i in range(40):
            if i == 5:
                # Swing high at 110 (will be broken bullish first)
                c = make_candle(open=100.0, high=110.0, low=99.0,
                                close=100.0, timestamp=ts + i * 60000)
            elif i == 15:
                # Bullish BOS — close above 110 * 1.001
                c = make_candle(open=109.0, high=115.0, low=108.0,
                                close=111.0, timestamp=ts + i * 60000)
            elif i == 18:
                # Swing low at 95 (will be broken bearish)
                c = make_candle(open=109.0, high=110.0, low=95.0,
                                close=109.0, timestamp=ts + i * 60000)
            elif i == 30:
                # Bearish break — close below 95 * 0.999 = CHoCH
                c = make_candle(open=96.0, high=97.0, low=93.0,
                                close=94.0, timestamp=ts + i * 60000)
            else:
                c = make_candle(open=105.0, high=106.0, low=104.0,
                                close=105.0, timestamp=ts + i * 60000)
            candles.append(c)

        state = analyzer.analyze(candles, "BTC/USDT", "15m")
        choch_breaks = [
            b for b in state.structure_breaks if b.break_type == "choch"
        ]
        # Should have at least one CHoCH (bearish after bullish trend)
        assert len(choch_breaks) > 0


class TestGetState:
    """Test state caching."""

    def test_get_state_returns_cached(self):
        analyzer = MarketStructureAnalyzer()
        candles = make_candle_series(count=25)
        analyzer.analyze(candles, "BTC/USDT", "15m")

        cached = analyzer.get_state("BTC/USDT", "15m")
        assert cached is not None
        assert cached.pair == "BTC/USDT"
        assert cached.timeframe == "15m"

    def test_get_state_returns_none_for_unknown(self):
        analyzer = MarketStructureAnalyzer()
        assert analyzer.get_state("ETH/USDT", "1h") is None


class TestSingleBreakPerCandle:
    """Test that only one structure break is recorded per candle."""

    def test_large_candle_only_one_break(self):
        """A candle that breaks multiple swing levels should only produce one break."""
        analyzer = MarketStructureAnalyzer()
        lookback = settings.SWING_LOOKBACK  # 5

        # Build candles with two separate swing lows
        candles = []
        for i in range(30):
            candles.append(make_candle(
                high=105.0, low=95.0, close=100.0,
                open=100.0, timestamp=1000 + i * 1000,
            ))

        # Place two swing lows at different levels
        # Swing low at index 8 (price 92)
        candles[8] = make_candle(
            high=100.0, low=92.0, close=95.0,
            open=100.0, timestamp=1000 + 8 * 1000,
        )
        # Make surrounding candles have higher lows
        for j in range(max(0, 8 - lookback), min(len(candles), 8 + lookback + 1)):
            if j != 8:
                candles[j] = make_candle(
                    high=105.0, low=96.0, close=100.0,
                    open=100.0, timestamp=1000 + j * 1000,
                )

        # Swing low at index 18 (price 93)
        candles[18] = make_candle(
            high=100.0, low=93.0, close=95.0,
            open=100.0, timestamp=1000 + 18 * 1000,
        )
        for j in range(max(0, 18 - lookback), min(len(candles), 18 + lookback + 1)):
            if j != 18:
                candles[j] = make_candle(
                    high=105.0, low=96.0, close=100.0,
                    open=100.0, timestamp=1000 + j * 1000,
                )

        # Big bearish candle at index 25 that breaks BOTH swing lows
        candles[25] = make_candle(
            high=100.0, low=85.0, close=88.0,
            open=100.0, timestamp=1000 + 25 * 1000,
        )

        state = analyzer.analyze(candles, "BTC/USDT", "15m")

        # Count breaks at candle_index=25
        breaks_at_25 = [
            b for b in state.structure_breaks
            if b.candle_index == 25
        ]
        # Should be at most 1 break per candle
        assert len(breaks_at_25) <= 1
