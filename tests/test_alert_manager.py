"""
Tests for AlertManager — routing, silencing, rate limiting, batching, escalation.
"""

import asyncio
import time

import pytest

from shared.alert_manager import AlertManager, AlertPriority, _UNSILENCEABLE


# ================================================================
# Mock TelegramNotifier
# ================================================================

class MockNotifier:
    """Fake TelegramNotifier that records sent messages."""

    def __init__(self, fail_until: int = 0):
        self.messages: list[str] = []
        self._call_count = 0
        self._fail_until = fail_until

    async def send(self, message: str) -> bool:
        self._call_count += 1
        if self._call_count <= self._fail_until:
            return False
        self.messages.append(message)
        return True

    async def notify_ob_summary(self, *args, **kwargs) -> None:
        self.messages.append("ob_summary")

    async def notify_hourly_status(self, **kwargs) -> None:
        self.messages.append("hourly_status")

    _WHALE_ACTION_MAP = {
        "exchange_deposit": ("deposited", "Bearish"),
        "exchange_withdrawal": ("withdrew", "Bullish"),
    }

    @staticmethod
    def _format_usd(value: float) -> str:
        if value >= 1_000_000:
            return f"${value / 1_000_000:.1f}M"
        return f"${value / 1_000:.0f}K"


def _run(coro):
    """Run async coroutine in a new event loop."""
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.fixture
def notifier():
    return MockNotifier()


@pytest.fixture
def mgr(notifier):
    return AlertManager(notifier)


# ================================================================
# Basic routing
# ================================================================

class TestBasicRouting:
    def test_info_sends_message(self, mgr, notifier):
        result = _run(mgr.alert(AlertPriority.INFO, "ob_summary", "test msg"))
        assert result is True
        assert notifier.messages == ["test msg"]

    def test_warning_sends_message(self, mgr, notifier):
        result = _run(mgr.alert(AlertPriority.WARNING, "ai_decision", "warn"))
        assert result is True
        assert notifier.messages == ["warn"]

    def test_critical_sends_message(self, mgr, notifier):
        result = _run(mgr.alert(AlertPriority.CRITICAL, "trade_lifecycle", "trade"))
        assert result is True
        assert notifier.messages == ["trade"]

    def test_critical_retries_once_on_failure(self):
        """CRITICAL gets 1 retry on first send failure."""
        notifier = MockNotifier(fail_until=1)
        mgr = AlertManager(notifier)
        result = _run(mgr.alert(AlertPriority.CRITICAL, "trade_lifecycle", "retry_msg"))
        assert result is True
        assert notifier.messages == ["retry_msg"]


# ================================================================
# Silencing
# ================================================================

class TestSilencing:
    def test_manual_silence_blocks_alerts(self, mgr, notifier):
        mgr.silence("ai_decision", 60)
        result = _run(mgr.alert(AlertPriority.WARNING, "ai_decision", "blocked"))
        assert result is False
        assert notifier.messages == []

    def test_silence_expires(self, mgr, notifier):
        mgr._silenced["ai_decision"] = time.time() - 1  # Already expired
        result = _run(mgr.alert(AlertPriority.WARNING, "ai_decision", "allowed"))
        assert result is True
        assert notifier.messages == ["allowed"]

    def test_emergency_ignores_silence(self, mgr, notifier):
        mgr._silenced["emergency"] = time.time() + 9999
        result = _run(mgr.alert(AlertPriority.EMERGENCY, "emergency", "critical"))
        assert result is True
        assert notifier.messages == ["critical"]

    def test_trade_lifecycle_cannot_be_silenced(self, mgr):
        result = mgr.silence("trade_lifecycle", 9999)
        assert result is False

    def test_emergency_cannot_be_silenced(self, mgr):
        result = mgr.silence("emergency", 9999)
        assert result is False

    def test_unsilenceable_categories(self):
        assert "emergency" in _UNSILENCEABLE
        assert "trade_lifecycle" in _UNSILENCEABLE

    def test_unsilence(self, mgr, notifier):
        mgr.silence("ai_decision", 9999)
        mgr.unsilence("ai_decision")
        result = _run(mgr.alert(AlertPriority.WARNING, "ai_decision", "back"))
        assert result is True

    def test_auto_silence_triggers(self, mgr, notifier):
        """3 alerts in 5 min auto-silences the category."""
        for i in range(3):
            _run(mgr.alert(AlertPriority.WARNING, "ws_reconnect", f"msg{i}"))

        # 4th should be silenced
        result = _run(mgr.alert(AlertPriority.WARNING, "ws_reconnect", "blocked"))
        assert result is False
        assert mgr.suppressed_count >= 1


# ================================================================
# Rate limiting
# ================================================================

class TestRateLimiting:
    def test_info_rate_limit(self, mgr, notifier):
        """INFO: max 10 per hour."""
        for i in range(10):
            # Use different categories to avoid auto-silence
            result = _run(mgr.alert(AlertPriority.INFO, f"cat_{i}", f"msg{i}"))
            assert result is True

        # 11th should be rate limited
        result = _run(mgr.alert(AlertPriority.INFO, "cat_extra", "overflow"))
        assert result is False

    def test_warning_rate_limit(self, mgr, notifier):
        """WARNING: max 5 per 15 min."""
        for i in range(5):
            result = _run(mgr.alert(AlertPriority.WARNING, f"cat_{i}", f"msg{i}"))
            assert result is True

        result = _run(mgr.alert(AlertPriority.WARNING, "cat_extra", "overflow"))
        assert result is False

    def test_emergency_no_rate_limit(self, mgr, notifier):
        """EMERGENCY has no rate limit."""
        for i in range(25):
            result = _run(mgr.alert(AlertPriority.EMERGENCY, "emergency", f"e{i}"))
            assert result is True


# ================================================================
# Whale batching
# ================================================================

class TestWhaleBatching:
    def test_whale_immediate_bypasses_batch(self, mgr, notifier):
        result = _run(mgr.send_whale_immediate("urgent whale"))
        assert result is True
        assert notifier.messages == ["urgent whale"]

    def test_whale_alert_returns_true(self, mgr, notifier):
        """Whale alert returns True (buffered for batch)."""
        result = _run(mgr.alert(AlertPriority.INFO, "whale_movement", "whale1"))
        assert result is True


# ================================================================
# EMERGENCY escalation
# ================================================================

class TestEmergencyEscalation:
    def test_emergency_succeeds_first_try(self):
        notifier = MockNotifier()
        mgr = AlertManager(notifier)
        result = _run(mgr.alert(AlertPriority.EMERGENCY, "emergency", "urgent"))
        assert result is True
        assert notifier.messages == ["urgent"]

    def test_emergency_retries_on_failure(self):
        """EMERGENCY retries with backoff if send fails."""
        notifier = MockNotifier(fail_until=2)  # Fail first 2, succeed 3rd
        mgr = AlertManager(notifier)

        async def run():
            # Monkey-patch sleep to speed up
            original_sleep = asyncio.sleep
            asyncio.sleep = lambda _: original_sleep(0)
            try:
                return await mgr.alert(AlertPriority.EMERGENCY, "emergency", "retry_test")
            finally:
                asyncio.sleep = original_sleep

        result = _run(run())
        assert result is True
        assert "retry_test" in notifier.messages

    def test_emergency_fails_after_all_retries(self):
        """EMERGENCY returns False after exhausting retries."""
        notifier = MockNotifier(fail_until=999)
        mgr = AlertManager(notifier)

        async def run():
            original_sleep = asyncio.sleep
            asyncio.sleep = lambda _: original_sleep(0)
            try:
                return await mgr.alert(AlertPriority.EMERGENCY, "emergency", "lost")
            finally:
                asyncio.sleep = original_sleep

        result = _run(run())
        assert result is False
        assert notifier.messages == []


# ================================================================
# Convenience methods
# ================================================================

class TestConvenienceMethods:
    def test_notify_trade_opened(self, mgr, notifier):
        class FakePos:
            pair = "BTC/USDT"
            direction = "long"
            actual_entry_price = 50000.0
            entry_price = 50000.0
            filled_size = 0.1
            leverage = 5
            sl_price = 49000.0
            tp2_price = 52000.0

        _run(mgr.notify_trade_opened(FakePos()))
        assert len(notifier.messages) == 1
        assert "TRADE OPENED" in notifier.messages[0]

    def test_notify_trade_closed(self, mgr, notifier):
        class FakePos:
            pair = "ETH/USDT"
            direction = "short"
            close_reason = "tp"
            pnl_pct = 0.02

        _run(mgr.notify_trade_closed(FakePos()))
        assert len(notifier.messages) == 1
        assert "TRADE CLOSED" in notifier.messages[0]

    def test_notify_emergency(self, mgr, notifier):
        class FakePos:
            pair = "BTC/USDT"
            direction = "long"

        _run(mgr.notify_emergency(FakePos(), "SL failed"))
        assert len(notifier.messages) == 1
        assert "EMERGENCY" in notifier.messages[0]

    def test_notify_health_down(self, mgr, notifier):
        _run(mgr.notify_health_down(["redis", "okx_ws"]))
        assert len(notifier.messages) == 1
        assert "DOWN" in notifier.messages[0]
        assert "redis" in notifier.messages[0]

    def test_notify_health_recovered(self, mgr, notifier):
        _run(mgr.notify_health_recovered(["redis"]))
        assert len(notifier.messages) == 1
        assert "RECOVERED" in notifier.messages[0]


# ================================================================
# Stats
# ================================================================

class TestStats:
    def test_suppressed_count_tracks(self, mgr, notifier):
        mgr.silence("test_cat", 60)
        _run(mgr.alert(AlertPriority.INFO, "test_cat", "blocked1"))
        _run(mgr.alert(AlertPriority.INFO, "test_cat", "blocked2"))
        assert mgr.suppressed_count == 2
