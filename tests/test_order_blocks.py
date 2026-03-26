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
        original = settings.OB_MIN_VOLUME_RATIO
        settings.OB_MIN_VOLUME_RATIO = 1.5  # Explicit threshold for test

        try:
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
        finally:
            settings.OB_MIN_VOLUME_RATIO = original

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


class TestOBImpulseScore:
    """Test impulse score computation for OBs."""

    def test_strong_impulse_gets_high_score(self):
        """OB followed by large displacement + high volume candles → high impulse score."""
        detector = OrderBlockDetector()

        candles = []
        for i in range(15):
            if i == 8:
                # OB candle: red, body=2 (102→100), volume=20
                c = make_candle(open=102.0, high=103.0, low=99.0,
                                close=100.0, volume=20.0, timestamp=i * 1000)
            elif i == 9:
                # Strong impulse candle: big green, high volume
                c = make_candle(open=101.0, high=110.0, low=100.5,
                                close=109.0, volume=30.0, timestamp=i * 1000)
            elif i == 10:
                # Break candle continues impulse
                c = make_candle(open=109.0, high=115.0, low=108.0,
                                close=114.0, volume=25.0, timestamp=i * 1000)
            else:
                c = make_candle(open=100.0, high=101.0, low=99.0,
                                close=100.5, volume=10.0, timestamp=i * 1000)
            candles.append(c)

        brk = _make_break("bullish", candle_index=10, timestamp=10000)
        obs = detector.update(candles, [brk], "BTC/USDT", "15m", 15000)

        assert len(obs) == 1
        # OB body = 2, displacement to high=115 from close=100 = 15 → 15/(2*3)=2.5 capped at 1.0
        # Volume: avg impulse ~27.5 vs avg ~12.7 → ratio ~2.2 → 2.2/3=0.73
        assert obs[0].impulse_score > 0.5

    def test_weak_impulse_gets_low_score(self):
        """OB followed by small candles with normal volume → low impulse score."""
        detector = OrderBlockDetector()

        candles = []
        for i in range(15):
            if i == 8:
                # OB candle: red, body=2, volume=20 (passes vol filter)
                c = make_candle(open=102.0, high=103.0, low=99.0,
                                close=100.0, volume=20.0, timestamp=i * 1000)
            elif i in (9, 10):
                # Weak drift candles: tiny move, normal volume
                c = make_candle(open=100.5, high=101.0, low=100.0,
                                close=100.8, volume=10.0, timestamp=i * 1000)
            else:
                c = make_candle(open=100.0, high=101.0, low=99.0,
                                close=100.5, volume=10.0, timestamp=i * 1000)
            candles.append(c)

        brk = _make_break("bullish", candle_index=10, timestamp=10000)
        obs = detector.update(candles, [brk], "BTC/USDT", "15m", 15000)

        assert len(obs) == 1
        # Tiny displacement, normal volume → low score
        assert obs[0].impulse_score < 0.3


class TestOBRetestCount:
    """Test retest counting for OBs."""

    def test_retest_counted_on_wick_into_zone(self):
        """Candle wicking into OB zone without closing through → retest counted."""
        detector = OrderBlockDetector()

        # OB body_high=102 — default candles must stay above 102 to not count as retests
        candles = []
        for i in range(15):
            if i == 8:
                # OB candle: body_high=102, body_low=100, low(wick)=98
                c = make_candle(open=102.0, high=103.0, low=98.0,
                                close=100.0, volume=20.0, timestamp=i * 1000)
            elif i == 10:
                # Break candle
                c = make_candle(open=104.0, high=112.0, low=103.0,
                                close=111.0, volume=10.0, timestamp=i * 1000)
            else:
                # Default candles: low=103 stays above OB body_high=102
                c = make_candle(open=105.0, high=106.0, low=103.0,
                                close=105.5, volume=10.0, timestamp=i * 1000)
            candles.append(c)

        brk = _make_break("bullish", candle_index=10, timestamp=10000)
        obs = detector.update(candles, [brk], "BTC/USDT", "15m", 15000)
        assert len(obs) == 1

        # Add candles that wick into OB zone (low <= body_high=102)
        # but close above OB low (98) → retest, not mitigation
        retest_candles = candles + [
            make_candle(open=105.0, high=106.0, low=101.0, close=104.0,
                        volume=10.0, timestamp=16000),
            make_candle(open=104.0, high=105.0, low=100.5, close=103.0,
                        volume=10.0, timestamp=17000),
        ]

        obs = detector.update(retest_candles, [], "BTC/USDT", "15m", 18000)
        assert len(obs) == 1
        assert obs[0].retest_count == 2

    def test_no_retest_when_price_stays_above_zone(self):
        """Candles that stay above OB zone → retest_count = 0."""
        detector = OrderBlockDetector()

        candles = []
        for i in range(15):
            if i == 8:
                # OB candle: body_high=102, body_low=100
                c = make_candle(open=102.0, high=103.0, low=99.0,
                                close=100.0, volume=20.0, timestamp=i * 1000)
            elif i == 10:
                c = make_candle(open=104.0, high=112.0, low=103.0,
                                close=111.0, volume=10.0, timestamp=i * 1000)
            else:
                # Default candles: low=103 stays above OB body_high=102
                c = make_candle(open=105.0, high=106.0, low=103.0,
                                close=105.5, volume=10.0, timestamp=i * 1000)
            candles.append(c)

        brk = _make_break("bullish", candle_index=10, timestamp=10000)
        obs = detector.update(candles, [brk], "BTC/USDT", "15m", 15000)

        # Add candles well above OB zone
        no_retest_candles = candles + [
            make_candle(open=110.0, high=112.0, low=108.0, close=111.0,
                        volume=10.0, timestamp=16000),
        ]

        obs = detector.update(no_retest_candles, [], "BTC/USDT", "15m", 17000)
        assert len(obs) == 1
        assert obs[0].retest_count == 0


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
