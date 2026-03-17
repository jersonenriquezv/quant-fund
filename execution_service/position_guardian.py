"""
Position Guardian — active market condition monitor for open positions.

Evaluates live candles against open positions and exits early when
the trading edge disappears. Called on every confirmed candle.

Rules:
- Can ONLY tighten SL or close. NEVER loosens protection, NEVER opens positions.
- Checks run in priority order; first action wins.
- All thresholds configurable via settings.

Checks (v1):
1. Counter-structure: N consecutive candles against position direction → close
2. Momentum death: avg body shrinks below entry-time levels → tighten or close
3. Stall detection: price range collapses → close if losing
4. Adverse CVD divergence: order flow against position + losing → close
"""

from __future__ import annotations

from typing import Optional

from config.settings import settings
from shared.logger import setup_logger
from shared.models import Candle, CVDSnapshot

logger = setup_logger("position_guardian")


class PositionGuardian:
    """Actively monitors open positions against live market conditions.
    Can only tighten SL or close — never loosens protection."""

    def __init__(self, monitor) -> None:
        """Initialize with a reference to PositionMonitor.

        Args:
            monitor: PositionMonitor instance (access to managed positions + close methods).
        """
        self.monitor = monitor
        # Track last action per pair for ML logging / dedup
        self.last_actions: dict[str, dict] = {}

    async def evaluate(
        self,
        pair: str,
        candle: Candle,
        recent_candles: list[Candle],
        cvd: Optional[CVDSnapshot] = None,
    ) -> Optional[str]:
        """Called on every confirmed candle. Check all open positions for the pair.

        Returns the action taken ("close", "tighten_sl") or None.
        """
        if not settings.POSITION_GUARDIAN_ENABLED:
            return None

        # Get managed position for this pair from monitor
        pos = self.monitor.positions.get(pair)
        if pos is None or pos.phase != "active":
            return None

        # Need enough candles for analysis
        if len(recent_candles) < settings.GUARDIAN_COUNTER_CANDLES:
            return None

        # Determine profit/loss state
        current_price = candle.close
        entry_price = pos.actual_entry_price or pos.entry_price
        if entry_price <= 0:
            return None

        if pos.direction == "long":
            in_profit = current_price > entry_price
        else:
            in_profit = current_price < entry_price

        # Run checks in priority order — stop on first action
        action = await self._check_counter_structure(pos, recent_candles)
        if action:
            return action

        action = await self._check_momentum_death(pos, recent_candles, in_profit)
        if action:
            return action

        action = await self._check_stall(pos, recent_candles, in_profit, current_price)
        if action:
            return action

        if settings.GUARDIAN_CVD_ENABLED and cvd is not None:
            action = await self._check_adverse_cvd(pos, cvd, in_profit)
            if action:
                return action

        return None

    # ================================================================
    # Check 1: Counter-structure detection (highest priority)
    # ================================================================

    async def _check_counter_structure(
        self, pos, recent_candles: list[Candle]
    ) -> Optional[str]:
        """N consecutive candles closing against position direction → early close.

        For long: N red candles in a row. For short: N green candles in a row.
        """
        n = settings.GUARDIAN_COUNTER_CANDLES
        tail = recent_candles[-n:]

        if pos.direction == "long":
            counter = all(c.close < c.open for c in tail)
        else:
            counter = all(c.close > c.open for c in tail)

        if not counter:
            return None

        logger.info(
            f"Guardian: counter_structure triggered for {pos.pair} {pos.direction} "
            f"— close (reason: {n} consecutive candles against position)"
        )
        await self._close_position(pos, "counter_structure")
        return "close"

    # ================================================================
    # Check 2: Momentum death
    # ================================================================

    async def _check_momentum_death(
        self, pos, recent_candles: list[Candle], in_profit: bool
    ) -> Optional[str]:
        """Avg recent candle body shrinks below threshold of earlier candles.

        If in profit → tighten SL to breakeven.
        If in loss → early close.
        """
        n = settings.GUARDIAN_COUNTER_CANDLES
        if len(recent_candles) < n * 2:
            return None

        # Recent candle bodies
        tail = recent_candles[-n:]
        avg_body_recent = sum(abs(c.close - c.open) for c in tail) / len(tail)

        # Earlier candle bodies (reference window)
        ref = recent_candles[-(n * 2):-n]
        avg_body_ref = sum(abs(c.close - c.open) for c in ref) / len(ref)

        if avg_body_ref <= 0:
            return None

        ratio = avg_body_recent / avg_body_ref
        if ratio >= settings.GUARDIAN_MOMENTUM_DECAY_RATIO:
            return None

        if in_profit:
            # Tighten SL to breakeven
            entry = pos.actual_entry_price or pos.entry_price
            if entry <= 0:
                return None

            # Only tighten if not already at or beyond breakeven
            if pos.direction == "long" and pos.current_sl_price >= entry:
                return None
            if pos.direction == "short" and pos.current_sl_price > 0 and pos.current_sl_price <= entry:
                return None

            logger.info(
                f"Guardian: momentum_death triggered for {pos.pair} {pos.direction} "
                f"— tighten_sl to breakeven (reason: body ratio {ratio:.2f} < "
                f"{settings.GUARDIAN_MOMENTUM_DECAY_RATIO})"
            )
            await self._tighten_sl_to_breakeven(pos)
            return "tighten_sl"
        else:
            logger.info(
                f"Guardian: momentum_death triggered for {pos.pair} {pos.direction} "
                f"— close (reason: body ratio {ratio:.2f} < "
                f"{settings.GUARDIAN_MOMENTUM_DECAY_RATIO}, position in loss)"
            )
            await self._close_position(pos, "momentum_death")
            return "close"

    # ================================================================
    # Check 3: Stall detection
    # ================================================================

    async def _check_stall(
        self, pos, recent_candles: list[Candle], in_profit: bool, current_price: float
    ) -> Optional[str]:
        """Last N candles have total range < threshold — price going nowhere.

        Only closes if position is in loss.
        """
        if in_profit:
            return None

        n = settings.GUARDIAN_COUNTER_CANDLES
        tail = recent_candles[-n:]

        highest = max(c.high for c in tail)
        lowest = min(c.low for c in tail)

        if current_price <= 0:
            return None

        total_range_pct = (highest - lowest) / current_price

        if total_range_pct >= settings.GUARDIAN_STALL_RANGE_PCT:
            return None

        logger.info(
            f"Guardian: stall_detection triggered for {pos.pair} {pos.direction} "
            f"— close (reason: range {total_range_pct*100:.4f}% < "
            f"{settings.GUARDIAN_STALL_RANGE_PCT*100:.4f}%, position in loss)"
        )
        await self._close_position(pos, "stall_detection")
        return "close"

    # ================================================================
    # Check 4: Adverse CVD divergence
    # ================================================================

    async def _check_adverse_cvd(
        self, pos, cvd: CVDSnapshot, in_profit: bool
    ) -> Optional[str]:
        """CVD diverging against position direction while in loss → close.

        For long: cvd_5m negative (sellers dominating) + in loss → close.
        For short: cvd_5m positive (buyers dominating) + in loss → close.
        """
        if in_profit:
            return None

        if pos.direction == "long" and cvd.cvd_5m >= 0:
            return None
        if pos.direction == "short" and cvd.cvd_5m <= 0:
            return None

        logger.info(
            f"Guardian: adverse_cvd triggered for {pos.pair} {pos.direction} "
            f"— close (reason: CVD_5m={cvd.cvd_5m:.2f} against {pos.direction}, "
            f"position in loss)"
        )
        await self._close_position(pos, "adverse_cvd")
        return "close"

    # ================================================================
    # Action helpers
    # ================================================================

    async def _close_position(self, pos, reason: str) -> None:
        """Market close the position via the monitor's close flow."""
        try:
            await self.monitor._close_all_orders_and_market_close(pos)
            # Override the reason (monitor sets "timeout" by default)
            pos.close_reason = f"guardian_{reason}"
        except Exception as e:
            logger.error(f"Guardian close failed for {pos.pair}: {e}")

        self.last_actions[pos.pair] = {"action": "close", "reason": reason}

    async def _tighten_sl_to_breakeven(self, pos) -> None:
        """Move SL to entry price (breakeven)."""
        entry = pos.actual_entry_price or pos.entry_price
        try:
            await self.monitor._adjust_sl(pos, entry)
        except Exception as e:
            logger.error(f"Guardian SL tighten failed for {pos.pair}: {e}")

        self.last_actions[pos.pair] = {"action": "tighten_sl", "reason": "momentum_death"}
