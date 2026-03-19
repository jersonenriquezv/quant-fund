"""
Position Guardian — shadow-mode market condition monitor for open positions.

SHADOW MODE (2026-03-19): All checks evaluate but ONLY LOG and record to
ml_setups — no closes, no SL tightening. Data collection for future ML
feature importance analysis.

Per AFML (López de Prado, Ch. 3/8): guardian features (counter-structure,
momentum decay, stall, CVD divergence) should be measured for predictive
power via purged k-fold CV before being used as exit rules. Until then,
triple barrier (SL/TP/timeout) provides clean labels for training.

Shadow triggers are persisted as boolean columns in ml_setups:
  guardian_shadow_counter, guardian_shadow_momentum,
  guardian_shadow_stall, guardian_shadow_cvd
These become features for the quality model: "did this condition appear
during the trade's lifetime?" Cross-reference with outcome_type to measure
predictive power.
"""

from __future__ import annotations

from typing import Optional

from config.settings import settings
from shared.logger import setup_logger
from shared.models import Candle, CVDSnapshot

logger = setup_logger("position_guardian")


class PositionGuardian:
    """Shadow-mode guardian: evaluates market conditions, logs and persists
    to ml_setups. No closes, no SL changes. Pure data collection."""

    def __init__(self, monitor) -> None:
        self.monitor = monitor
        # Track which checks already fired per setup_id (write once per trade)
        self._fired: dict[str, set[str]] = {}

    async def evaluate(
        self,
        pair: str,
        candle: Candle,
        recent_candles: list[Candle],
        cvd: Optional[CVDSnapshot] = None,
    ) -> Optional[str]:
        """Called on every confirmed candle. Evaluate all checks in shadow mode.

        Returns None always (shadow mode never acts).
        """
        if not settings.POSITION_GUARDIAN_ENABLED:
            return None

        pos = self.monitor.positions.get(pair)
        if pos is None or pos.phase != "active":
            return None

        if len(recent_candles) < settings.GUARDIAN_COUNTER_CANDLES:
            return None

        current_price = candle.close
        entry_price = pos.actual_entry_price or pos.entry_price
        if entry_price <= 0:
            return None

        if pos.direction == "long":
            in_profit = current_price > entry_price
        else:
            in_profit = current_price < entry_price

        setup_id = pos.setup_id

        # Evaluate ALL checks (don't short-circuit — collect all shadow data)
        self._shadow_counter_structure(pos, recent_candles, in_profit, setup_id)
        self._shadow_momentum_death(pos, recent_candles, in_profit, setup_id)
        self._shadow_stall(pos, recent_candles, in_profit, current_price, setup_id)
        if cvd is not None:
            self._shadow_adverse_cvd(pos, cvd, in_profit, setup_id)

        return None  # Shadow mode — never acts

    def cleanup(self, setup_id: str) -> None:
        """Remove tracking for a closed trade."""
        self._fired.pop(setup_id, None)

    # ================================================================
    # Persistence helper
    # ================================================================

    def _record_shadow(self, setup_id: str, check_name: str) -> None:
        """Persist shadow trigger to ml_setups (once per trade per check)."""
        if not setup_id:
            return

        # Dedup: only write once per (setup_id, check_name)
        fired_set = self._fired.setdefault(setup_id, set())
        if check_name in fired_set:
            return
        fired_set.add(check_name)

        # Write to DB via monitor's data_store
        try:
            ds = self.monitor._data_store
            if ds is not None and ds.postgres is not None:
                ds.postgres.update_ml_guardian_shadow(setup_id, check_name)
        except Exception as e:
            logger.error(f"Guardian shadow persist failed: {setup_id} {check_name} {e}")

    # ================================================================
    # Shadow check 1: Counter-structure
    # ================================================================

    def _shadow_counter_structure(
        self, pos, recent_candles: list[Candle], in_profit: bool,
        setup_id: str
    ) -> None:
        n = settings.GUARDIAN_COUNTER_CANDLES
        tail = recent_candles[-n:]

        if pos.direction == "long":
            counter = all(c.close < c.open for c in tail)
        else:
            counter = all(c.close > c.open for c in tail)

        if not counter:
            return

        logger.debug(
            f"Guardian shadow: counter_structure WOULD trigger for {pos.pair} "
            f"{pos.direction} ({n} candles against, in_profit={in_profit})"
        )
        self._record_shadow(setup_id, "counter")

    # ================================================================
    # Shadow check 2: Momentum death
    # ================================================================

    def _shadow_momentum_death(
        self, pos, recent_candles: list[Candle], in_profit: bool,
        setup_id: str
    ) -> None:
        n = settings.GUARDIAN_COUNTER_CANDLES
        if len(recent_candles) < n * 2:
            return

        tail = recent_candles[-n:]
        avg_body_recent = sum(abs(c.close - c.open) for c in tail) / len(tail)

        ref = recent_candles[-(n * 2):-n]
        avg_body_ref = sum(abs(c.close - c.open) for c in ref) / len(ref)

        if avg_body_ref <= 0:
            return

        ratio = avg_body_recent / avg_body_ref
        if ratio >= settings.GUARDIAN_MOMENTUM_DECAY_RATIO:
            return

        action = "tighten_sl" if in_profit else "close"
        logger.debug(
            f"Guardian shadow: momentum_death WOULD trigger for {pos.pair} "
            f"{pos.direction} — {action} (body_ratio={ratio:.2f} < "
            f"{settings.GUARDIAN_MOMENTUM_DECAY_RATIO}, in_profit={in_profit})"
        )
        self._record_shadow(setup_id, "momentum")

    # ================================================================
    # Shadow check 3: Stall detection
    # ================================================================

    def _shadow_stall(
        self, pos, recent_candles: list[Candle], in_profit: bool,
        current_price: float, setup_id: str
    ) -> None:
        if in_profit:
            return

        n = settings.GUARDIAN_COUNTER_CANDLES
        tail = recent_candles[-n:]

        highest = max(c.high for c in tail)
        lowest = min(c.low for c in tail)

        if current_price <= 0:
            return

        total_range_pct = (highest - lowest) / current_price

        if total_range_pct >= settings.GUARDIAN_STALL_RANGE_PCT:
            return

        logger.debug(
            f"Guardian shadow: stall_detection WOULD trigger for {pos.pair} "
            f"{pos.direction} (range={total_range_pct*100:.4f}% < "
            f"{settings.GUARDIAN_STALL_RANGE_PCT*100:.4f}%, in_profit={in_profit})"
        )
        self._record_shadow(setup_id, "stall")

    # ================================================================
    # Shadow check 4: Adverse CVD divergence
    # ================================================================

    def _shadow_adverse_cvd(
        self, pos, cvd: CVDSnapshot, in_profit: bool, setup_id: str
    ) -> None:
        if in_profit:
            return

        would_trigger = False
        if pos.direction == "long" and cvd.cvd_5m < 0:
            would_trigger = True
        elif pos.direction == "short" and cvd.cvd_5m > 0:
            would_trigger = True

        if would_trigger:
            logger.debug(
                f"Guardian shadow: adverse_cvd WOULD trigger for {pos.pair} "
                f"{pos.direction} (CVD_5m={cvd.cvd_5m:.2f}, in_profit={in_profit})"
            )
            self._record_shadow(setup_id, "cvd")
