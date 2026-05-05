"""
Risk-based fallback sizing in ShadowMonitor.add_shadow().

Validates the fix for the fixed-notional fallback hack introduced in commit
7bd8827. Shadow setups whose risk_service check fails (typically by
MIN_RISK_DISTANCE_PCT) must still be tracked, but with sizing that mirrors
risk_service.PositionSizer so theoretical PnL reflects the real
RISK_PER_TRADE-per-trade risk model.

Live execution never reaches this path: risk_service rejection sets
position_size=0, which is filtered upstream in main._process_pipeline_setup
before execute() runs. These tests guard the shadow data-quality contract
only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from config.settings import settings
from execution_service.shadow_monitor import ShadowMonitor
from shared.models import RiskApproval, TradeSetup


# ================================================================
# Fakes (parallel to test_shadow_infra.py — kept self-contained
# so this module can run in isolation)
# ================================================================


class FakeRedis:
    def __init__(self):
        self._store: dict[str, tuple[str, float, int]] = {}
        self._client = object()

    def set_bot_state(self, key: str, value: str, ttl: int = 86400) -> None:
        self._store[key] = (value, time.time(), ttl)

    def get_bot_state(self, key: str):
        entry = self._store.get(key)
        if entry is None:
            return None
        value, _, _ = entry
        return value


class FakePostgres:
    def __init__(self):
        self.ml_rows: dict[str, dict] = {}

    def update_ml_shadow_tracking(self, setup_id: str, fields: dict) -> None:
        self.ml_rows.setdefault(setup_id, {}).update(fields)

    def update_ml_setup_outcome(self, setup_id: str, **kwargs) -> None:
        self.ml_rows.setdefault(setup_id, {}).update(kwargs)

    def resolve_orphaned_shadow_setups(self, max_age_hours: float = 36.0) -> int:
        return 0


@dataclass
class FakeDataService:
    redis: FakeRedis
    postgres: FakePostgres


def _make_monitor() -> ShadowMonitor:
    return ShadowMonitor(
        data_service=FakeDataService(redis=FakeRedis(), postgres=FakePostgres()),
        notifier=None,
    )


def _mk_setup(
    *,
    setup_id: str = "fb_test",
    entry: float = 1000.0,
    sl_pct: float = 0.005,
    direction: str = "long",
    setup_type: str = "scalp_sweep_choch_v1",
) -> TradeSetup:
    """Build a TradeSetup with arbitrary SL distance for sizing tests."""
    if direction == "long":
        sl = entry * (1.0 - sl_pct)
        tp2 = entry * (1.0 + 2 * sl_pct)
        tp1 = entry + (tp2 - entry) * 0.5
    else:
        sl = entry * (1.0 + sl_pct)
        tp2 = entry * (1.0 - 2 * sl_pct)
        tp1 = entry - (entry - tp2) * 0.5
    return TradeSetup(
        timestamp=int(time.time() * 1000),
        pair="BTC/USDT",
        direction=direction,
        setup_type=setup_type,
        entry_price=entry,
        sl_price=sl,
        tp1_price=tp1,
        tp2_price=tp2,
        confluences=["test"],
        htf_bias="bullish" if direction == "long" else "bearish",
        ob_timeframe="15m",
        setup_id=setup_id,
    )


# ================================================================
# Tests — risk-based fallback contract
# ================================================================


class TestFallbackRiskBased:
    """The fallback path must size by RISK_PER_TRADE × SHADOW_CAPITAL / SL distance,
    matching PositionSizer.calculate.
    """

    def test_fallback_when_approval_is_none(self):
        """No risk_approval → risk-based fallback fires."""
        mon = _make_monitor()
        setup = _mk_setup(entry=1000.0, sl_pct=0.005)
        accepted = mon.add_shadow(setup, orderbook=None, risk_approval=None)
        assert accepted is True

        pos = mon._positions[setup.setup_id]
        risk_amount = settings.SHADOW_CAPITAL * settings.RISK_PER_TRADE
        expected_size = risk_amount / abs(setup.entry_price - setup.sl_price)
        assert pos.position_size == pytest.approx(expected_size, rel=1e-9)

    def test_fallback_when_position_size_zero(self):
        """RiskApproval with position_size <= 0 (rejected) → risk-based fallback."""
        mon = _make_monitor()
        setup = _mk_setup(setup_id="fb_zero", entry=1000.0, sl_pct=0.003)
        rejected = RiskApproval(
            approved=False, position_size=0.0, leverage=0.0,
            risk_pct=0.0, reason="MIN_RISK_DISTANCE_PCT failed",
        )
        accepted = mon.add_shadow(setup, orderbook=None, risk_approval=rejected)
        assert accepted is True

        pos = mon._positions[setup.setup_id]
        risk_amount = settings.SHADOW_CAPITAL * settings.RISK_PER_TRADE
        expected_size = risk_amount / abs(setup.entry_price - setup.sl_price)
        assert pos.position_size == pytest.approx(expected_size, rel=1e-9)

    def test_sl_loss_equals_risk_amount_at_normal_distance(self):
        """SL hit → loss ≈ RISK_PER_TRADE × SHADOW_CAPITAL when leverage cap
        does not bind. Uses gross PnL (size × distance); fees are layered on
        top by compute_pnl in production but the position-sizing contract is
        per-distance.
        """
        mon = _make_monitor()
        setup = _mk_setup(entry=1000.0, sl_pct=0.005)  # 0.5% SL
        mon.add_shadow(setup, orderbook=None, risk_approval=None)
        pos = mon._positions[setup.setup_id]

        gross_sl_loss = pos.position_size * abs(setup.entry_price - setup.sl_price)
        risk_amount = settings.SHADOW_CAPITAL * settings.RISK_PER_TRADE
        assert gross_sl_loss == pytest.approx(risk_amount, rel=1e-9)

    def test_tp_gross_matches_rr_ratio(self):
        """TP gross / SL gross must equal the structural RR set by the setup
        (sizing does not distort RR).
        """
        mon = _make_monitor()
        setup = _mk_setup(entry=1000.0, sl_pct=0.005)  # tp2 at +1.0% → RR 2:1
        mon.add_shadow(setup, orderbook=None, risk_approval=None)
        pos = mon._positions[setup.setup_id]

        gross_sl = pos.position_size * abs(setup.entry_price - setup.sl_price)
        gross_tp = pos.position_size * abs(setup.tp2_price - setup.entry_price)
        structural_rr = abs(setup.tp2_price - setup.entry_price) / abs(
            setup.entry_price - setup.sl_price
        )
        assert gross_tp / gross_sl == pytest.approx(structural_rr, rel=1e-9)


class TestFallbackLeverageCap:
    """Tight SL distances would imply leverage > MAX_LEVERAGE. The fallback
    must cap notional like PositionSizer.calculate does.
    """

    def test_leverage_capped_when_distance_tight(self):
        """SL distance forces leverage above MAX_LEVERAGE → cap kicks in."""
        # Pick sl_pct so risk-implied leverage clearly exceeds MAX_LEVERAGE.
        # leverage_uncapped = (risk_amount / distance × entry) / SHADOW_CAPITAL
        #                   = (RISK_PER_TRADE / sl_pct)
        # → for any sl_pct < RISK_PER_TRADE / MAX_LEVERAGE, cap binds.
        sl_pct = (settings.RISK_PER_TRADE / settings.MAX_LEVERAGE) / 2
        mon = _make_monitor()
        setup = _mk_setup(setup_id="fb_cap", entry=1000.0, sl_pct=sl_pct)
        mon.add_shadow(setup, orderbook=None, risk_approval=None)

        pos = mon._positions[setup.setup_id]
        assert pos.leverage == pytest.approx(float(settings.MAX_LEVERAGE), rel=1e-9)

        notional = pos.position_size * setup.entry_price
        expected_notional = settings.SHADOW_CAPITAL * settings.MAX_LEVERAGE
        assert notional == pytest.approx(expected_notional, rel=1e-9)

    def test_capped_position_underrealizes_risk(self):
        """When cap binds, realized SL loss is BELOW risk_amount (cap shrinks
        position). This documents the safety direction: capped sizing never
        risks more than the target, only less.
        """
        sl_pct = (settings.RISK_PER_TRADE / settings.MAX_LEVERAGE) / 2  # cap binds
        mon = _make_monitor()
        setup = _mk_setup(setup_id="fb_under", entry=1000.0, sl_pct=sl_pct)
        mon.add_shadow(setup, orderbook=None, risk_approval=None)
        pos = mon._positions[setup.setup_id]

        gross_sl_loss = pos.position_size * abs(setup.entry_price - setup.sl_price)
        risk_amount = settings.SHADOW_CAPITAL * settings.RISK_PER_TRADE
        assert gross_sl_loss < risk_amount, (
            "leverage cap must shrink loss below target risk, not exceed it"
        )

    def test_leverage_uncapped_at_normal_distance(self):
        """When SL distance is wide enough, computed leverage stays under cap
        and is NOT clamped.
        """
        # 1% SL with 1% risk per trade → leverage = 1.0x (well under cap).
        mon = _make_monitor()
        setup = _mk_setup(setup_id="fb_normal", entry=1000.0, sl_pct=0.01)
        mon.add_shadow(setup, orderbook=None, risk_approval=None)

        pos = mon._positions[setup.setup_id]
        assert pos.leverage < settings.MAX_LEVERAGE
        assert pos.leverage == pytest.approx(1.0, rel=1e-9)


class TestFallbackEdgeCases:
    """Defensive checks — fallback must refuse to track invalid setups."""

    def test_skip_when_distance_zero(self):
        """entry == sl → distance 0, cannot risk-size, skip."""
        mon = _make_monitor()
        # _mk_setup builds sl from sl_pct; sl_pct=0 yields sl == entry,
        # but downstream long/short price ordering check would also reject.
        # Use a manual TradeSetup to force entry == sl precisely.
        setup = TradeSetup(
            timestamp=int(time.time() * 1000),
            pair="BTC/USDT",
            direction="long",
            setup_type="scalp_sweep_choch_v1",
            entry_price=1000.0,
            sl_price=1000.0,
            tp1_price=1005.0,
            tp2_price=1010.0,
            confluences=["test"],
            htf_bias="bullish",
            ob_timeframe="15m",
            setup_id="fb_dist0",
        )
        accepted = mon.add_shadow(setup, orderbook=None, risk_approval=None)
        assert accepted is False
        assert "fb_dist0" not in mon._positions

    def test_skip_when_entry_price_zero(self):
        """entry_price <= 0 → skip."""
        mon = _make_monitor()
        setup = TradeSetup(
            timestamp=int(time.time() * 1000),
            pair="BTC/USDT",
            direction="long",
            setup_type="scalp_sweep_choch_v1",
            entry_price=0.0,
            sl_price=0.0,
            tp1_price=0.0,
            tp2_price=0.0,
            confluences=["test"],
            htf_bias="bullish",
            ob_timeframe="15m",
            setup_id="fb_entry0",
        )
        accepted = mon.add_shadow(setup, orderbook=None, risk_approval=None)
        assert accepted is False

    def test_approval_with_valid_size_bypasses_fallback(self):
        """When risk_approval has a real position_size, fallback must NOT
        overwrite it — live-shaped sizing flows through unchanged.
        """
        mon = _make_monitor()
        setup = _mk_setup(setup_id="fb_passthrough", entry=1000.0, sl_pct=0.005)
        approved = RiskApproval(
            approved=True, position_size=0.0123, leverage=3.0,
            risk_pct=0.01, reason="ok",
        )
        accepted = mon.add_shadow(setup, orderbook=None, risk_approval=approved)
        assert accepted is True

        pos = mon._positions[setup.setup_id]
        assert pos.position_size == pytest.approx(0.0123, rel=1e-9)
        assert pos.leverage == pytest.approx(3.0, rel=1e-9)


class TestFallbackParityWithPositionSizer:
    """Direct parity with risk_service.PositionSizer.calculate — both code paths
    must produce the same (size, leverage) tuple for the same inputs.
    """

    @pytest.mark.parametrize(
        "entry,sl_pct,direction",
        [
            (1000.0, 0.005, "long"),
            (1000.0, 0.0015, "long"),   # scalp tight
            (50000.0, 0.004, "short"),
            (1.5, 0.003, "long"),       # low-priced asset
        ],
    )
    def test_parity_with_position_sizer(self, entry, sl_pct, direction):
        from risk_service.position_sizer import PositionSizer

        mon = _make_monitor()
        setup = _mk_setup(
            setup_id=f"parity_{entry}_{sl_pct}_{direction}",
            entry=entry, sl_pct=sl_pct, direction=direction,
        )
        mon.add_shadow(setup, orderbook=None, risk_approval=None)
        pos = mon._positions[setup.setup_id]

        sizer = PositionSizer()
        expected_size, expected_lev = sizer.calculate(
            entry=setup.entry_price,
            sl=setup.sl_price,
            capital=settings.SHADOW_CAPITAL,
            risk_pct=settings.RISK_PER_TRADE,
        )
        assert pos.position_size == pytest.approx(expected_size, rel=1e-9)
        assert pos.leverage == pytest.approx(expected_lev, rel=1e-9)
