"""Tests for strategy_service.order_blocks — OB detection, volume filter, expiration."""

import pytest
from tests.conftest import make_candle
from strategy_service.order_blocks import OrderBlockDetector, OrderBlock
from strategy_service.market_structure import StructureBreak
from config.settings import settings


def _make_break(direction="bullish", candle_index=10,
                timestamp=10000) -> StructureBreak:
    """Helper to create a StructureBreak."""
    return StructureBreak(
        timestamp=timestamp,
        break_type="bos",
        direction=direction,
        break_price=110.0 if direction == "bullish" else 90.0,
        broken_level=108.0 if direction == "bullish" else 92.0,
        candle_index=candle_index,
    )


class TestOBDetection:
    """Test Order Block detection logic."""

    def test_bullish_ob_finds_last_red_candle(self):
        """Bullish break → OB is the last RED candle before the break."""
        detector = OrderBlockDetector()

        candles = []
        for i in range(15):
            if i == 8:
                # Red candle (close < open) — this should be the OB
                c = make_candle(open=102.0, high=103.0, low=99.0,
                                close=100.0, volume=20.0, timestamp=i * 1000)
            elif i == 9:
                # Green candle — skip this
                c = make_candle(open=100.0, high=105.0, low=99.5,
                                close=104.0, volume=10.0, timestamp=i * 1000)
            elif i == 10:
                # Break candle
                c = make_candle(open=104.0, high=112.0, low=103.0,
                                close=111.0, volume=10.0, timestamp=i * 1000)
            else:
                c = make_candle(open=100.0, high=101.0, low=99.0,
                                close=100.5, volume=10.0, timestamp=i * 1000)
            candles.append(c)

        brk = _make_break("bullish", candle_index=10, timestamp=10000)
        obs = detector.update(candles, [brk], "BTC/USDT", "15m", 15000)

        bullish_obs = [ob for ob in obs if ob.direction == "bullish"]
        assert len(bullish_obs) == 1
        assert bullish_obs[0].timestamp == 8000  # Index 8
        assert bullish_obs[0].body_high == 102.0  # open
        assert bullish_obs[0].body_low == 100.0   # close

    def test_bearish_ob_finds_last_green_candle(self):
        """Bearish break → OB is the last GREEN candle before the break."""
        detector = OrderBlockDetector()

        candles = []
        for i in range(15):
            if i == 8:
                # Green candle (close >= open) — this should be the OB
                c = make_candle(open=100.0, high=103.0, low=99.0,
                                close=102.0, volume=20.0, timestamp=i * 1000)
            elif i == 9:
                # Red candle — skip this
                c = make_candle(open=101.0, high=102.0, low=97.0,
                                close=98.0, volume=10.0, timestamp=i * 1000)
            elif i == 10:
                # Break candle
                c = make_candle(open=98.0, high=99.0, low=88.0,
                                close=89.0, volume=10.0, timestamp=i * 1000)
            else:
                c = make_candle(open=100.0, high=101.0, low=99.0,
                                close=100.5, volume=10.0, timestamp=i * 1000)
            candles.append(c)

        brk = _make_break("bearish", candle_index=10, timestamp=10000)
        obs = detector.update(candles, [brk], "BTC/USDT", "15m", 15000)

        bearish_obs = [ob for ob in obs if ob.direction == "bearish"]
        assert len(bearish_obs) == 1
        assert bearish_obs[0].timestamp == 8000

    def test_entry_price_is_75_pct_of_body(self):
        """OB entry should be 50% of the body (midpoint — balances fill rate vs risk)."""
        detector = OrderBlockDetector()

        candles = []
        for i in range(15):
            if i == 8:
                c = make_candle(open=104.0, high=106.0, low=98.0,
                                close=100.0, volume=20.0, timestamp=i * 1000)
            else:
                c = make_candle(volume=10.0, timestamp=i * 1000)
            candles.append(c)

        brk = _make_break("bullish", candle_index=10)
        obs = detector.update(candles, [brk], "BTC/USDT", "15m", 15000)

        assert len(obs) == 1
        # Bullish OB: body_low=100, body_high=104, range=4
        # 50% from bottom: 100 + 4*0.50 = 102
        assert obs[0].entry_price == 102.0


class TestOBVolumeFilter:
    """Test volume ratio filter."""

    def test_low_volume_ob_rejected(self):
        """OB with volume < OB_MIN_VOLUME_RATIO * avg should be rejected."""
        detector = OrderBlockDetector()

        # All candles have volume=10, OB candle also has volume=10
        # ratio = 10/10 = 1.0 < 1.5 → rejected
        candles = []
        for i in range(15):
            if i == 8:
                c = make_candle(open=104.0, high=106.0, low=98.0,
                                close=100.0, volume=10.0, timestamp=i * 1000)
            else:
                c = make_candle(volume=10.0, timestamp=i * 1000)
            candles.append(c)

        brk = _make_break("bullish", candle_index=10)
        obs = detector.update(candles, [brk], "BTC/USDT", "15m", 15000)
        assert len(obs) == 0

    def test_high_volume_ob_accepted(self):
        """OB with volume >= OB_MIN_VOLUME_RATIO * avg should be accepted."""
        detector = OrderBlockDetector()

        candles = []
        for i in range(15):
            if i == 8:
                # volume=20, avg=10, ratio=2.0 >= 1.5 ✓
                c = make_candle(open=104.0, high=106.0, low=98.0,
                                close=100.0, volume=20.0, timestamp=i * 1000)
            else:
                c = make_candle(volume=10.0, timestamp=i * 1000)
            candles.append(c)

        brk = _make_break("bullish", candle_index=10)
        obs = detector.update(candles, [brk], "BTC/USDT", "15m", 15000)
        assert len(obs) == 1
        assert obs[0].volume_ratio >= settings.OB_MIN_VOLUME_RATIO


class TestOBExpiration:
    """Test OB age expiration."""

    def test_expired_ob_pruned(self):
        """OB older than OB_MAX_AGE_HOURS should be removed."""
        detector = OrderBlockDetector()

        candles = []
        for i in range(15):
            if i == 8:
                c = make_candle(open=104.0, high=106.0, low=98.0,
                                close=100.0, volume=20.0, timestamp=i * 1000)
            else:
                c = make_candle(volume=10.0, timestamp=i * 1000)
            candles.append(c)

        brk = _make_break("bullish", candle_index=10, timestamp=10000)
        obs = detector.update(candles, [brk], "BTC/USDT", "15m", 15000)
        assert len(obs) == 1

        # Jump past expiration
        expired_time = 8000 + (settings.OB_MAX_AGE_HOURS * 3600 * 1000) + 1
        obs = detector.update(candles, [], "BTC/USDT", "15m", expired_time)
        assert len(obs) == 0


class TestOBMitigation:
    """Test OB mitigation (price closes through full zone)."""

    def test_bullish_ob_mitigated_on_close_below(self):
        """Bullish OB is mitigated when candle closes below OB low."""
        detector = OrderBlockDetector()

        candles = []
        for i in range(15):
            if i == 8:
                # OB candle: high=106, low=98
                c = make_candle(open=104.0, high=106.0, low=98.0,
                                close=100.0, volume=20.0, timestamp=i * 1000)
            else:
                c = make_candle(volume=10.0, timestamp=i * 1000)
            candles.append(c)

        brk = _make_break("bullish", candle_index=10)
        obs = detector.update(candles, [brk], "BTC/USDT", "15m", 15000)
        assert len(obs) == 1

        # Add candle that closes below OB low (98)
        mitigation_candles = candles + [
            make_candle(open=99.0, high=100.0, low=96.0, close=97.0,
                        volume=10.0, timestamp=16000),
        ]

        obs = detector.update(mitigation_candles, [], "BTC/USDT", "15m", 17000)
        assert len(obs) == 0  # Mitigated and pruned

    def test_bearish_ob_mitigated_on_close_above(self):
        """Bearish OB is mitigated when candle closes above OB high."""
        detector = OrderBlockDetector()

        candles = []
        for i in range(15):
            if i == 8:
                # Green candle for bearish OB: high=103, low=99
                c = make_candle(open=100.0, high=103.0, low=99.0,
                                close=102.0, volume=20.0, timestamp=i * 1000)
            else:
                c = make_candle(volume=10.0, timestamp=i * 1000)
            candles.append(c)

        brk = _make_break("bearish", candle_index=10)
        obs = detector.update(candles, [brk], "BTC/USDT", "15m", 15000)
        assert len(obs) == 1

        # Add candle that closes above OB high (103)
        mitigation_candles = candles + [
            make_candle(open=102.0, high=105.0, low=101.0, close=104.0,
                        volume=10.0, timestamp=16000),
        ]

        obs = detector.update(mitigation_candles, [], "BTC/USDT", "15m", 17000)
        assert len(obs) == 0


class TestOBDeduplication:
    """Test that OBs are not duplicated on repeated calls."""

    def test_same_break_does_not_create_duplicate_ob(self):
        """Calling update() with the same break twice should not duplicate."""
        detector = OrderBlockDetector()

        candles = []
        for i in range(15):
            if i == 8:
                c = make_candle(open=104.0, high=106.0, low=98.0,
                                close=100.0, volume=20.0, timestamp=i * 1000)
            else:
                c = make_candle(volume=10.0, timestamp=i * 1000)
            candles.append(c)

        brk = _make_break("bullish", candle_index=10)

        obs1 = detector.update(candles, [brk], "BTC/USDT", "15m", 15000)
        obs2 = detector.update(candles, [brk], "BTC/USDT", "15m", 16000)

        assert len(obs1) == 1
        assert len(obs2) == 1  # Still 1, not 2
