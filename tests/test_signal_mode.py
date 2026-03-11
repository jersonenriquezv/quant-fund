"""Tests for signal mode (SIGNAL_ONLY flag)."""

import asyncio

from unittest.mock import AsyncMock

from shared.models import TradeSetup, AIDecision, RiskApproval
from shared.alert_manager import AlertManager


def _make_setup():
    return TradeSetup(
        timestamp=1000000,
        pair="ETH/USDT",
        direction="long",
        setup_type="setup_a",
        entry_price=2450.00,
        sl_price=2425.00,
        tp1_price=2475.00,
        tp2_price=2500.00,
        confluences=["sweep", "choch", "ob_15m"],
        htf_bias="bullish",
        ob_timeframe="15m",
    )


def _make_decision(approved=True, confidence=0.72):
    return AIDecision(
        confidence=confidence,
        approved=approved,
        reasoning="Funding negative, CVD bullish, context supports long",
        adjustments={},
        warnings=[],
    )


def _make_approval():
    return RiskApproval(
        approved=True,
        position_size=0.041,
        leverage=5.0,
        risk_pct=0.02,
        reason="approved",
    )


def test_notify_signal_sends_message():
    """notify_signal() formats and sends a Telegram message via alert()."""
    notifier = AsyncMock()
    notifier.send = AsyncMock(return_value=True)
    mgr = AlertManager(notifier)

    setup = _make_setup()
    decision = _make_decision()
    approval = _make_approval()

    asyncio.run(mgr.notify_signal(setup, approval, decision))

    notifier.send.assert_called_once()
    msg = notifier.send.call_args[0][0]

    # Verify key info is present
    assert "SIGNAL" in msg
    assert "ETH/USDT" in msg
    assert "LONG" in msg
    assert "2,450.00" in msg    # entry
    assert "2,425.00" in msg    # SL
    assert "2,500.00" in msg    # TP
    assert "0.041" in msg       # size
    assert "5x" in msg          # leverage
    assert "sweep" in msg       # confluence
    assert "72%" in msg         # AI confidence
    assert "R:R" in msg


def test_notify_signal_short_setup():
    """Signal works for short direction with correct arrow."""
    notifier = AsyncMock()
    notifier.send = AsyncMock(return_value=True)
    mgr = AlertManager(notifier)

    setup = TradeSetup(
        timestamp=1000000,
        pair="BTC/USDT",
        direction="short",
        setup_type="setup_d",
        entry_price=70000.00,
        sl_price=70350.00,
        tp1_price=69650.00,
        tp2_price=69300.00,
        confluences=["ob_15m", "bos"],
        htf_bias="bearish",
        ob_timeframe="15m",
    )
    decision = _make_decision(confidence=1.0)
    approval = _make_approval()

    asyncio.run(
        mgr.notify_signal(setup, approval, decision)
    )

    msg = notifier.send.call_args[0][0]
    assert "SHORT" in msg
    assert "BTC/USDT" in msg
    assert "SETUP_D" in msg


def test_notify_signal_none_decision():
    """Signal works even if decision is None (edge case)."""
    notifier = AsyncMock()
    notifier.send = AsyncMock(return_value=True)
    mgr = AlertManager(notifier)

    setup = _make_setup()
    approval = _make_approval()

    asyncio.run(
        mgr.notify_signal(setup, approval, None)
    )

    msg = notifier.send.call_args[0][0]
    assert "bypass" in msg


def test_signal_only_setting_default():
    """SIGNAL_ONLY defaults to False."""
    from config.settings import Settings
    s = Settings()
    assert s.SIGNAL_ONLY is False


def test_signal_rr_calculation():
    """R:R ratio is correctly calculated in signal message."""
    notifier = AsyncMock()
    notifier.send = AsyncMock(return_value=True)
    mgr = AlertManager(notifier)

    setup = _make_setup()  # entry=2450, sl=2425 (risk=25), tp2=2500 (reward=50) → R:R 1:2.0
    decision = _make_decision()
    approval = _make_approval()

    asyncio.run(
        mgr.notify_signal(setup, approval, decision)
    )

    msg = notifier.send.call_args[0][0]
    assert "1:2.0" in msg
