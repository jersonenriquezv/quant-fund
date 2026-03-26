"""
Shadow Mode Monitor — theoretical outcome tracking without live execution.

Tracks detected setups that are NOT sent to execution. Instead, monitors price
action to determine whether the theoretical entry, TP, and SL would have been
hit. Records outcome in ml_setups for future feature importance analysis.

López de Prado forward-testing approach: evaluate strategy signals under real
market conditions without risking capital on unvalidated setups.

Caveats logged per setup:
- Orderbook spread + depth at detection (fill quality estimate)
- Time to theoretical fill (entry touched)
- Fill candle volume ratio (low volume = unreliable fill)
- Slippage estimate (worst price in fill candle vs theoretical entry)
"""

import asyncio
import time
from dataclasses import dataclass, field

from config.settings import settings
from shared.logger import setup_logger
from shared.models import TradeSetup
from shared.notifier import TelegramNotifier

logger = setup_logger("shadow_monitor")


@dataclass
class ShadowPosition:
    """Tracked shadow position awaiting theoretical outcome."""
    setup_id: str
    pair: str
    direction: str
    setup_type: str
    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: float
    detection_time: float         # time.time() when detected
    filled: bool = False          # True once price touched entry
    fill_time: float = 0.0        # time.time() when theoretically filled
    position_size: float = 0.0    # Theoretical position size (base currency)
    leverage: float = 0.0
    margin: float = 0.0           # USDT margin used
    # Orderbook data at detection
    spread_at_detection: float = 0.0
    depth_at_entry: float = 0.0   # USD depth near entry price
    # Fill quality
    fill_candle_volume_ratio: float = 0.0  # fill candle vol / avg vol
    slippage_estimate_pct: float = 0.0     # worst price in fill candle vs entry


class ShadowMonitor:
    """Monitors shadow-mode setups and resolves theoretical outcomes.

    Lifecycle per setup:
    1. add_shadow() — called from pipeline when setup is in SHADOW_MODE_SETUPS
    2. Every candle tick, check_candle() evaluates:
       a. If not filled: did price touch entry? → mark filled
       b. If filled: did price hit TP2 or SL? → resolve outcome
       c. Entry timeout (SHADOW_ENTRY_TIMEOUT_HOURS) → resolve as "shadow_no_fill"
       d. Trade timeout (SHADOW_TRADE_TIMEOUT_HOURS) → resolve as "shadow_timeout"
    3. Outcome written to ml_setups via data_store.update_ml_setup_outcome()
    """

    def __init__(self, data_service, notifier: TelegramNotifier | None = None):
        self._data_service = data_service
        self._notifier = notifier
        self._positions: dict[str, ShadowPosition] = {}  # setup_id -> ShadowPosition

    @property
    def active_count(self) -> int:
        return len(self._positions)

    def add_shadow(
        self, setup: TradeSetup,
        orderbook: dict | None = None,
        risk_approval=None,
    ) -> None:
        """Register a shadow setup for theoretical tracking.

        Args:
            risk_approval: RiskApproval from risk_service.check(dry_run=True).
                Must be provided with valid position_size. If risk service is
                unavailable, shadow tracking is skipped (bad data > no data).
        """
        if setup.setup_id in self._positions:
            return

        # Require risk_approval — standalone sizing removed to prevent
        # data quality issues (wrong capital, missing guardrails)
        if risk_approval is None or risk_approval.position_size <= 0:
            logger.warning(
                f"Shadow: no valid risk_approval for {setup.setup_id}, "
                f"skipping (risk service unavailable or rejected)"
            )
            return

        risk = abs(setup.entry_price - setup.sl_price)
        if risk <= 0 or setup.entry_price <= 0:
            logger.warning(f"Shadow: invalid risk for {setup.setup_id}, skipping")
            return

        # Sanity: verify TP/SL direction matches trade direction
        if setup.direction == "long":
            if setup.tp2_price <= setup.entry_price or setup.sl_price >= setup.entry_price:
                logger.warning(
                    f"Shadow: invalid prices for long {setup.setup_id} "
                    f"entry={setup.entry_price} tp2={setup.tp2_price} sl={setup.sl_price}"
                )
                return
        else:
            if setup.tp2_price >= setup.entry_price or setup.sl_price <= setup.entry_price:
                logger.warning(
                    f"Shadow: invalid prices for short {setup.setup_id} "
                    f"entry={setup.entry_price} tp2={setup.tp2_price} sl={setup.sl_price}"
                )
                return

        position_size = risk_approval.position_size
        leverage = risk_approval.leverage
        notional = position_size * setup.entry_price
        margin = notional / leverage if leverage > 1.0 else notional

        pos = ShadowPosition(
            setup_id=setup.setup_id,
            pair=setup.pair,
            direction=setup.direction,
            setup_type=setup.setup_type,
            entry_price=setup.entry_price,
            sl_price=setup.sl_price,
            tp1_price=setup.tp1_price,
            tp2_price=setup.tp2_price,
            detection_time=time.time(),
            position_size=position_size,
            leverage=leverage,
            margin=margin,
        )

        # Orderbook quality data
        if orderbook:
            pos.spread_at_detection = orderbook.get("spread", 0.0)
            # Depth on the side we need for fill
            if setup.direction == "long":
                pos.depth_at_entry = orderbook.get("depth_ask_usd", 0.0)
            else:
                pos.depth_at_entry = orderbook.get("depth_bid_usd", 0.0)

        self._positions[setup.setup_id] = pos

        # Persist shadow metadata to ml_setups
        if self._data_service and self._data_service.postgres:
            self._data_service.postgres.update_ml_shadow_tracking(
                setup.setup_id,
                {
                    "shadow_mode": True,
                    "shadow_position_size": pos.position_size,
                    "shadow_leverage": pos.leverage,
                    "shadow_margin": pos.margin,
                    "shadow_spread_at_detection": pos.spread_at_detection,
                    "shadow_depth_at_entry": pos.depth_at_entry,
                },
            )

        logger.info(
            f"Shadow: tracking {setup.setup_type} {setup.pair} {setup.direction} "
            f"entry={setup.entry_price:.2f} sl={setup.sl_price:.2f} "
            f"tp2={setup.tp2_price:.2f} size={position_size:.6f} "
            f"margin=${margin:.2f} spread={pos.spread_at_detection*100:.4f}%"
        )

        self._notify_detection(pos)

    def check_candle(self, pair: str, candle) -> None:
        """Evaluate all shadow positions for this pair against a new candle.

        Called from the pipeline on every confirmed candle.
        """
        now = time.time()
        resolved = []

        for setup_id, pos in self._positions.items():
            if pos.pair != pair:
                continue

            if not pos.filled:
                # Phase 1: Waiting for theoretical fill
                entry_timeout_s = settings.SHADOW_ENTRY_TIMEOUT_HOURS * 3600
                if (now - pos.detection_time) > entry_timeout_s:
                    self._resolve(pos, "shadow_no_fill")
                    resolved.append(setup_id)
                    continue

                # Check if candle touched entry price
                if self._candle_touched_price(candle, pos.entry_price):
                    pos.filled = True
                    pos.fill_time = now
                    # Candle-resolution approximation — real limit order fill
                    # would be sub-second for liquid pairs. This measures detection
                    # to candle close that touched entry.
                    fill_duration_ms = int((now - pos.detection_time) * 1000)

                    # Fill quality metrics
                    avg_volume = self._get_avg_volume(pair, candle.timeframe)
                    if avg_volume > 0:
                        pos.fill_candle_volume_ratio = candle.volume / avg_volume

                    # Slippage estimate: worst price within the fill candle
                    pos.slippage_estimate_pct = self._estimate_slippage(
                        candle, pos.entry_price, pos.direction
                    )

                    # Update fill tracking in DB
                    if self._data_service and self._data_service.postgres:
                        self._data_service.postgres.update_ml_shadow_tracking(
                            setup_id,
                            {
                                "shadow_fill_time_ms": fill_duration_ms,
                                "shadow_fill_candle_volume_ratio": pos.fill_candle_volume_ratio,
                                "shadow_slippage_estimate_pct": pos.slippage_estimate_pct,
                            },
                        )

                    logger.info(
                        f"Shadow fill: {pos.setup_type} {pos.pair} {pos.direction} "
                        f"entry={pos.entry_price:.2f} fill_time={fill_duration_ms/1000:.0f}s "
                        f"vol_ratio={pos.fill_candle_volume_ratio:.2f} "
                        f"slippage={pos.slippage_estimate_pct*100:.3f}%"
                    )

                    self._notify_fill(pos)

                    # Check if this same candle also hit TP or SL
                    outcome = self._check_tp_sl(candle, pos)
                    if outcome:
                        self._resolve(pos, outcome)
                        resolved.append(setup_id)

            else:
                # Phase 2: Filled — waiting for TP or SL
                trade_timeout_s = settings.SHADOW_TRADE_TIMEOUT_HOURS * 3600
                if (now - pos.fill_time) > trade_timeout_s:
                    # Timeout — compute PnL at current price
                    self._resolve(pos, "shadow_timeout", exit_price=candle.close)
                    resolved.append(setup_id)
                    continue

                outcome = self._check_tp_sl(candle, pos)
                if outcome:
                    self._resolve(pos, outcome)
                    resolved.append(setup_id)

        for sid in resolved:
            del self._positions[sid]

    def _check_tp_sl(self, candle, pos: ShadowPosition) -> str | None:
        """Check if candle hit TP2 or SL. Returns outcome string or None.

        Uses TP2 (not TP1) because shadow mode tracks full theoretical outcome.
        In live trading, TP1 partial close + TP2 remainder is the norm,
        but for ML labeling we want the terminal outcome.
        """
        hit_tp = self._candle_touched_price(candle, pos.tp2_price)
        hit_sl = self._candle_touched_price(candle, pos.sl_price)

        if hit_tp and hit_sl:
            # Both hit in same candle — conservative: assume SL hit first
            # (adverse selection bias correction)
            return "shadow_sl"
        if hit_tp:
            return "shadow_tp"
        if hit_sl:
            return "shadow_sl"
        return None

    def _candle_touched_price(self, candle, price: float) -> bool:
        """Check if candle's high-low range reached the target price."""
        return candle.low <= price <= candle.high

    def _estimate_slippage(self, candle, entry_price: float, direction: str) -> float:
        """Estimate slippage as fraction of entry price.

        For longs, worst fill = candle high (if entry is at low of candle, you might
        have gotten filled at a worse price). Approximation: use the candle range
        to estimate max adverse movement from entry.
        """
        if entry_price <= 0:
            return 0.0
        if direction == "long":
            # Worst case: filled at the high of the fill candle
            worst = candle.high
            return max(0, (worst - entry_price) / entry_price)
        else:
            # Worst case: filled at the low of the fill candle
            worst = candle.low
            return max(0, (entry_price - worst) / entry_price)

    def _resolve(
        self, pos: ShadowPosition, outcome: str, exit_price: float | None = None
    ) -> None:
        """Write theoretical outcome to ml_setups."""
        # Determine exit price and PnL
        if exit_price is None:
            if outcome == "shadow_tp":
                exit_price = pos.tp2_price
            elif outcome == "shadow_sl":
                exit_price = pos.sl_price
            else:
                exit_price = pos.entry_price  # no fill or unknown

        if pos.filled and pos.entry_price > 0:
            if pos.direction == "long":
                pnl_pct = (exit_price - pos.entry_price) / pos.entry_price
            else:
                pnl_pct = (pos.entry_price - exit_price) / pos.entry_price

            pnl_usd = pnl_pct * pos.position_size * pos.entry_price
            # Deduct fees (both sides)
            fee = (pos.position_size * pos.entry_price + pos.position_size * exit_price) * settings.TRADING_FEE_RATE
            pnl_usd -= fee
        else:
            pnl_pct = 0.0
            pnl_usd = 0.0

        fill_duration_ms = None
        trade_duration_ms = None
        if pos.filled:
            fill_duration_ms = int((pos.fill_time - pos.detection_time) * 1000)
            trade_duration_ms = int((time.time() - pos.fill_time) * 1000)

        exit_reason_map = {
            "shadow_tp": "tp",
            "shadow_sl": "sl",
            "shadow_timeout": "timeout",
            "shadow_no_fill": "no_fill",
        }

        if self._data_service and self._data_service.postgres:
            self._data_service.postgres.update_ml_setup_outcome(
                setup_id=pos.setup_id,
                outcome_type=outcome,
                pnl_pct=pnl_pct,
                pnl_usd=pnl_usd,
                actual_entry=pos.entry_price if pos.filled else None,
                actual_exit=exit_price if pos.filled else None,
                exit_reason=exit_reason_map.get(outcome, outcome),
                fill_duration_ms=fill_duration_ms,
                trade_duration_ms=trade_duration_ms,
            )

        status = "WIN" if pnl_usd > 0 else "LOSS" if pnl_usd < 0 else "FLAT"
        logger.info(
            f"Shadow resolved: {pos.setup_type} {pos.pair} {pos.direction} "
            f"outcome={outcome} pnl=${pnl_usd:.2f} ({pnl_pct*100:.2f}%) "
            f"[{status}] fill_vol_ratio={pos.fill_candle_volume_ratio:.2f} "
            f"slippage={pos.slippage_estimate_pct*100:.3f}%"
        )

        self._notify_resolve(pos, outcome, pnl_usd, pnl_pct, status)

    def _notify_detection(self, pos: ShadowPosition) -> None:
        """Send Telegram alert when a shadow setup starts tracking."""
        if self._notifier is None:
            return
        short_pair = pos.pair.replace("/USDT", "")
        rr = abs(pos.tp2_price - pos.entry_price) / abs(pos.entry_price - pos.sl_price) if abs(pos.entry_price - pos.sl_price) > 0 else 0
        msg = (
            f"\U0001f47b <b>SHADOW TRACKING</b>\n"
            f"{short_pair} {pos.direction.upper()} ({pos.setup_type})\n"
            f"Entry: ${pos.entry_price:,.2f} | SL: ${pos.sl_price:,.2f} | TP: ${pos.tp2_price:,.2f}\n"
            f"R:R {rr:.1f} | Margin: ${pos.margin:.0f}"
        )
        asyncio.ensure_future(self._notifier.send(msg))

    def _notify_fill(self, pos: ShadowPosition) -> None:
        """Send Telegram alert when a shadow position is theoretically filled."""
        if self._notifier is None:
            return
        short_pair = pos.pair.replace("/USDT", "")
        msg = (
            f"\U0001f47b <b>SHADOW FILL</b>\n"
            f"{short_pair} {pos.direction.upper()} ({pos.setup_type})\n"
            f"Entry: ${pos.entry_price:,.2f} | SL: ${pos.sl_price:,.2f}\n"
            f"TP: ${pos.tp2_price:,.2f} | Size: ${pos.margin:.0f}"
        )
        asyncio.ensure_future(self._notifier.send(msg))

    def _notify_resolve(
        self, pos: ShadowPosition, outcome: str,
        pnl_usd: float, pnl_pct: float, status: str,
    ) -> None:
        """Send Telegram alert when a shadow position resolves."""
        if self._notifier is None:
            return
        short_pair = pos.pair.replace("/USDT", "")
        outcome_label = outcome.replace("shadow_", "").upper()
        emoji = "\U0001f4b0" if status == "WIN" else "\U0001f534" if status == "LOSS" else "\u26aa"
        msg = (
            f"{emoji} <b>SHADOW {outcome_label}</b>\n"
            f"{short_pair} {pos.direction.upper()} ({pos.setup_type})\n"
            f"P&amp;L: ${pnl_usd:+.2f} ({pnl_pct*100:+.2f}%)"
        )
        asyncio.ensure_future(self._notifier.send(msg))

    def _get_avg_volume(self, pair: str, timeframe: str) -> float:
        """Get average volume for volume ratio calculation."""
        if self._data_service is None:
            return 0.0
        candles = self._data_service.get_candles(pair, timeframe, count=20)
        if not candles or len(candles) < 5:
            return 0.0
        return sum(c.volume for c in candles) / len(candles)

    def get_status(self) -> dict:
        """Return summary for dashboard/logging."""
        waiting_fill = sum(1 for p in self._positions.values() if not p.filled)
        tracking = sum(1 for p in self._positions.values() if p.filled)
        return {
            "total": len(self._positions),
            "waiting_fill": waiting_fill,
            "tracking_outcome": tracking,
            "pairs": list(set(p.pair for p in self._positions.values())),
        }
