"""Tests for data_service.oi_liquidation_proxy — OI-based liquidation detection."""

import time
import pytest
from unittest.mock import patch

from data_service.oi_liquidation_proxy import OILiquidationProxy
from shared.models import OpenInterest


def _make_oi(pair: str, oi_usd: float, ts: int) -> OpenInterest:
    return OpenInterest(
        timestamp=ts,
        pair=pair,
        oi_contracts=1000,
        oi_base=10.0,
        oi_usd=oi_usd,
    )


# ============================================================
# Basic detection
# ============================================================

class TestDetection:

    def test_no_event_on_first_snapshot(self):
        proxy = OILiquidationProxy()
        proxy.update(_make_oi("BTC/USDT", 1_000_000, 1000))
        assert proxy.get_recent_liquidations("BTC/USDT") == []

    def test_no_event_on_small_drop(self):
        """1% drop should NOT trigger (threshold is 2%)."""
        proxy = OILiquidationProxy()
        ts = int(time.time() * 1000)
        proxy.update(_make_oi("BTC/USDT", 1_000_000, ts))
        proxy.update(_make_oi("BTC/USDT", 990_000, ts + 300_000))
        assert proxy.get_recent_liquidations("BTC/USDT") == []

    def test_event_on_threshold_drop(self):
        """Exactly 2% drop should trigger."""
        proxy = OILiquidationProxy()
        ts = int(time.time() * 1000)
        proxy.update(_make_oi("BTC/USDT", 1_000_000, ts))
        proxy.update(_make_oi("BTC/USDT", 980_000, ts + 300_000))
        events = proxy.get_recent_liquidations("BTC/USDT")
        assert len(events) == 1
        assert events[0].source == "oi_proxy"
        assert events[0].size_usd == 20_000

    def test_event_on_large_drop(self):
        """5% drop should trigger."""
        proxy = OILiquidationProxy()
        ts = int(time.time() * 1000)
        proxy.update(_make_oi("BTC/USDT", 1_000_000, ts))
        proxy.update(_make_oi("BTC/USDT", 950_000, ts + 300_000))
        events = proxy.get_recent_liquidations("BTC/USDT")
        assert len(events) == 1
        assert events[0].size_usd == 50_000

    def test_no_event_on_oi_increase(self):
        """OI going UP should never trigger."""
        proxy = OILiquidationProxy()
        ts = int(time.time() * 1000)
        proxy.update(_make_oi("BTC/USDT", 1_000_000, ts))
        proxy.update(_make_oi("BTC/USDT", 1_100_000, ts + 300_000))
        assert proxy.get_recent_liquidations("BTC/USDT") == []


# ============================================================
# Pair isolation
# ============================================================

class TestPairIsolation:

    def test_pairs_tracked_independently(self):
        proxy = OILiquidationProxy()
        ts = int(time.time() * 1000)

        proxy.update(_make_oi("BTC/USDT", 1_000_000, ts))
        proxy.update(_make_oi("ETH/USDT", 500_000, ts))

        # Drop BTC OI by 3%, ETH unchanged
        proxy.update(_make_oi("BTC/USDT", 970_000, ts + 300_000))
        proxy.update(_make_oi("ETH/USDT", 500_000, ts + 300_000))

        assert len(proxy.get_recent_liquidations("BTC/USDT")) == 1
        assert len(proxy.get_recent_liquidations("ETH/USDT")) == 0

    def test_filter_by_pair(self):
        proxy = OILiquidationProxy()
        ts = int(time.time() * 1000)

        proxy.update(_make_oi("BTC/USDT", 1_000_000, ts))
        proxy.update(_make_oi("ETH/USDT", 500_000, ts))
        proxy.update(_make_oi("BTC/USDT", 970_000, ts + 300_000))
        proxy.update(_make_oi("ETH/USDT", 480_000, ts + 300_000))

        # Both pairs dropped >2%
        all_events = proxy.get_recent_liquidations(None)
        assert len(all_events) == 2

        btc_events = proxy.get_recent_liquidations("BTC/USDT")
        assert len(btc_events) == 1
        assert btc_events[0].pair == "BTC/USDT"


# ============================================================
# Aggregated stats
# ============================================================

class TestAggregatedStats:

    def test_stats_empty(self):
        proxy = OILiquidationProxy()
        stats = proxy.get_aggregated_stats("BTC/USDT", minutes=5)
        assert stats["total_usd"] == 0
        assert stats["count"] == 0

    def test_stats_with_events(self):
        proxy = OILiquidationProxy()
        ts = int(time.time() * 1000)
        proxy.update(_make_oi("BTC/USDT", 1_000_000, ts))
        proxy.update(_make_oi("BTC/USDT", 950_000, ts + 300_000))

        stats = proxy.get_aggregated_stats("BTC/USDT", minutes=60)
        assert stats["total_usd"] == 50_000
        assert stats["count"] == 1


# ============================================================
# Edge cases
# ============================================================

class TestEdgeCases:

    def test_zero_oi_ignored(self):
        proxy = OILiquidationProxy()
        ts = int(time.time() * 1000)
        proxy.update(_make_oi("BTC/USDT", 0, ts))
        proxy.update(_make_oi("BTC/USDT", 0, ts + 300_000))
        assert proxy.get_recent_liquidations("BTC/USDT") == []

    def test_is_connected_always_true(self):
        proxy = OILiquidationProxy()
        assert proxy.is_connected is True

    def test_custom_threshold(self):
        """Verify settings.OI_DROP_THRESHOLD_PCT is respected."""
        proxy = OILiquidationProxy()
        ts = int(time.time() * 1000)
        proxy.update(_make_oi("BTC/USDT", 1_000_000, ts))

        # 1.5% drop — below default 2% threshold
        with patch("data_service.oi_liquidation_proxy.settings") as mock_s:
            mock_s.OI_DROP_THRESHOLD_PCT = 0.01  # Lower to 1%
            mock_s.OI_DROP_WINDOW_SECONDS = 300
            proxy.update(_make_oi("BTC/USDT", 985_000, ts + 300_000))

        events = proxy.get_recent_liquidations("BTC/USDT")
        assert len(events) == 1

    def test_minutes_filter(self):
        """Events outside the time window should be excluded."""
        proxy = OILiquidationProxy()
        # Create event with old timestamp (2 hours ago)
        old_ts = int((time.time() - 7200) * 1000)
        proxy.update(_make_oi("BTC/USDT", 1_000_000, old_ts))
        proxy.update(_make_oi("BTC/USDT", 950_000, old_ts + 300_000))

        # Should not appear in last 60 minutes
        assert len(proxy.get_recent_liquidations("BTC/USDT", minutes=60)) == 0
