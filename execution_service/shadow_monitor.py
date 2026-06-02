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
import json
import time
from dataclasses import asdict, dataclass, field

from config.settings import settings
from shared.logger import setup_logger
from shared.models import RiskApproval, TradeSetup
from shared.notifier import TelegramNotifier
from shared.pnl_engine import (
    CandleSlice,
    Outcome,
    Position,
    compute_pnl,
    step as pnl_step,
)

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
    initial_sl_price: float = 0.0  # Original structural SL before any BE move
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
    # TP1 tracking — simulates live breakeven SL move
    tp1_touched: bool = False              # True once price touched TP1 (1:1 R:R)
    # Per-signal time stop (seconds since fill). 0 = use SHADOW_TRADE_TIMEOUT_HOURS.
    # Set by add_shadow when setup_type appears in settings.SCALP_SIGNAL_PARAMS.
    time_stop_seconds: int = 0

    @property
    def notional_usd(self) -> float:
        return self.position_size * self.entry_price

    @property
    def target_risk_usd(self) -> float:
        return settings.effective_shadow_capital * settings.RISK_PER_TRADE

    @property
    def initial_risk_usd(self) -> float:
        sl = self.initial_sl_price or self.sl_price
        return self.position_size * abs(self.entry_price - sl)

    @property
    def initial_rr(self) -> float:
        risk = abs(self.entry_price - (self.initial_sl_price or self.sl_price))
        if risk <= 0:
            return 0.0
        return abs(self.tp2_price - self.entry_price) / risk


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

    # Max shadow lifetime: 24h entry + 12h trade + buffer
    _REDIS_TTL = 172800  # 48h

    def __init__(self, data_service, notifier: TelegramNotifier | None = None):
        self._data_service = data_service
        self._notifier = notifier
        self._positions: dict[str, ShadowPosition] = {}  # setup_id -> ShadowPosition
        self._last_orphan_cleanup = 0.0
        # Batching flag: inner helpers (_check_tp_sl) set this on TP1
        # transitions so check_candle can persist once at end of tick.
        self._dirty_from_inner_checks: bool = False
        # Restore is DEFERRED, not run here. ShadowMonitor is constructed in
        # main.py BEFORE DataService.start() connects Redis, so calling
        # _load_from_redis() in __init__ always saw redis=None and silently
        # skipped — every restart lost all in-flight shadows, which then aged
        # out as `shadow_orphaned` (the orphan-leak root cause, confirmed via
        # the "Redis unavailable" restore breadcrumb on 2026-06-02). Restore
        # runs lazily on the first check_candle tick, by which point candles
        # are flowing => DataService + Redis are up.
        self._restored: bool = False

    def _ensure_restored(self) -> None:
        """Run the one-time Redis restore + orphan sweep, once Redis is up.

        Called from check_candle (candles only flow after DataService start),
        so Redis/Postgres connections are guaranteed ready — unlike __init__.
        """
        if self._restored:
            return
        self._restored = True
        self._load_from_redis()
        self._cleanup_orphaned_db_rows()

    @property
    def active_count(self) -> int:
        return len(self._positions)

    def _emit_metric(
        self, name: str, value: float = 1.0,
        pair: str | None = None, labels: dict | None = None,
    ) -> None:
        """Operational metric (fire-and-forget)."""
        if self._data_service is None or self._data_service.postgres is None:
            return
        try:
            self._data_service.postgres.insert_metric(
                name, value, pair=pair, labels=labels,
            )
        except Exception:
            pass  # Never block the shadow loop

    def add_shadow(
        self, setup: TradeSetup,
        orderbook: dict | None = None,
        risk_approval=None,
    ) -> bool:
        """Register a shadow setup for theoretical tracking.

        Args:
            risk_approval: RiskApproval from risk_service.check(dry_run=True).
                Must be provided with valid position_size. If risk service is
                unavailable, shadow tracking is skipped (bad data > no data).

        Returns:
            True if the setup was accepted for tracking, False if skipped.
        """
        if setup.setup_id in self._positions:
            return False

        # Dedup: only block if we already have a RECENT UNFILLED shadow for the
        # same pair/direction/setup_type with a similar entry price (<1% diff).
        # Once filled (tracking outcome), allow new shadows — they represent
        # a new trade idea at a different price level.
        # Staleness: unfilled shadows older than 4h don't block — OB/FVG has
        # likely shifted, new detection at similar price is a fresh data point.
        now = time.time()
        _DEDUP_STALENESS_SECONDS = 4 * 3600  # 4 hours
        for pos in self._positions.values():
            if (pos.pair == setup.pair
                    and pos.direction == setup.direction
                    and pos.setup_type == setup.setup_type
                    and not pos.filled):
                age_s = now - pos.detection_time
                if age_s > _DEDUP_STALENESS_SECONDS:
                    continue  # Stale unfilled — don't block
                price_diff = abs(pos.entry_price - setup.entry_price) / pos.entry_price
                if price_diff < 0.01:
                    logger.debug(
                        f"Shadow dedup: {setup.setup_type} {setup.pair} "
                        f"{setup.direction} entry={setup.entry_price:.2f} — "
                        f"already tracking unfilled {pos.setup_id} at {pos.entry_price:.2f}"
                    )
                    return False

        # Risk-based fallback when risk_service rejects (commonly via
        # MIN_RISK_DISTANCE_PCT for tight-SL strategies like scalp). Shadow
        # still tracks for data collection, but sizing must mirror
        # risk_service.PositionSizer so theoretical PnL reflects what a real
        # RISK_PER_TRADE-per-trade position would yield. Live execution never
        # reaches this path: risk_service rejection sets position_size=0,
        # which is filtered upstream in main._process_pipeline_setup before
        # execute() runs.
        if risk_approval is None or risk_approval.position_size <= 0:
            distance = abs(setup.entry_price - setup.sl_price)
            if distance <= 0 or setup.entry_price <= 0:
                logger.warning(f"Shadow: cannot size {setup.setup_id}, skipping")
                return False

            shadow_capital = settings.effective_shadow_capital
            risk_amount = shadow_capital * settings.RISK_PER_TRADE
            fallback_size = risk_amount / distance
            fallback_notional = fallback_size * setup.entry_price
            fallback_leverage = fallback_notional / shadow_capital

            # Cap at MAX_LEVERAGE — mirrors PositionSizer.calculate (risk_service/position_sizer.py).
            # When SL distance is very tight, implied leverage exceeds the cap;
            # we recompute size from the capped notional. Because the cap
            # SHRINKS the position, the realized SL loss falls BELOW
            # risk_amount in this edge case (same trade-off as the live sizer:
            # leverage discipline beats hitting the exact risk target).
            if fallback_leverage > settings.MAX_LEVERAGE:
                fallback_leverage = float(settings.MAX_LEVERAGE)
                fallback_notional = shadow_capital * fallback_leverage
                fallback_size = fallback_notional / setup.entry_price

            if fallback_size <= 0:
                logger.warning(f"Shadow: cannot size {setup.setup_id}, skipping")
                return False

            risk_approval = RiskApproval(
                approved=False,
                position_size=fallback_size,
                leverage=fallback_leverage,
                risk_pct=settings.RISK_PER_TRADE,
                reason="fallback_sizing_risk_based",
            )

        risk = abs(setup.entry_price - setup.sl_price)
        if risk <= 0 or setup.entry_price <= 0:
            logger.warning(f"Shadow: invalid risk for {setup.setup_id}, skipping")
            return False

        # Sanity: verify TP/SL direction matches trade direction
        if setup.direction == "long":
            if setup.tp2_price <= setup.entry_price or setup.sl_price >= setup.entry_price:
                logger.warning(
                    f"Shadow: invalid prices for long {setup.setup_id} "
                    f"entry={setup.entry_price} tp2={setup.tp2_price} sl={setup.sl_price}"
                )
                return False
        else:
            if setup.tp2_price >= setup.entry_price or setup.sl_price <= setup.entry_price:
                logger.warning(
                    f"Shadow: invalid prices for short {setup.setup_id} "
                    f"entry={setup.entry_price} tp2={setup.tp2_price} sl={setup.sl_price}"
                )
                return False

        position_size = risk_approval.position_size
        leverage = risk_approval.leverage
        notional = position_size * setup.entry_price
        margin = notional / leverage if leverage > 1.0 else notional

        # Per-signal time stop override (scalp shadow signals).
        scalp_params = settings.SCALP_SIGNAL_PARAMS.get(setup.setup_type)
        time_stop_seconds = int(scalp_params["time_stop_seconds"]) if scalp_params else 0

        pos = ShadowPosition(
            setup_id=setup.setup_id,
            pair=setup.pair,
            direction=setup.direction,
            setup_type=setup.setup_type,
            entry_price=setup.entry_price,
            sl_price=setup.sl_price,
            tp1_price=setup.tp1_price,
            tp2_price=setup.tp2_price,
            initial_sl_price=setup.sl_price,
            detection_time=time.time(),
            position_size=position_size,
            leverage=leverage,
            margin=margin,
            time_stop_seconds=time_stop_seconds,
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
        self._save_to_redis()

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
            f"notional=${pos.notional_usd:.2f} margin=${margin:.2f} "
            f"risk=${pos.initial_risk_usd:.2f}/${pos.target_risk_usd:.2f} "
            f"spread={pos.spread_at_detection*100:.4f}%"
        )

        self._notify_detection(pos)
        return True

    def check_candle(self, pair: str, candle) -> None:
        """Evaluate all shadow positions for this pair against a new candle.

        Called from the pipeline on every confirmed candle.
        Redis persistence is batched: state changes set `dirty`, and a
        single _save_to_redis fires at the end of the tick instead of
        one per event. Avoids up to N (positions × transitions) Redis
        writes per candle when many shadows transition simultaneously.
        """
        # One-time deferred restore (Redis is connected by the time candles flow).
        self._ensure_restored()

        now = time.time()

        # Periodic orphan cleanup — every 6 hours
        if (now - self._last_orphan_cleanup) > 21600:
            self._cleanup_orphaned_db_rows()

        resolved = []
        dirty = False

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

                    # Update fill tracking in DB (includes fill-candle trace
                    # for deterministic replay — see migration 17).
                    if self._data_service and self._data_service.postgres:
                        self._data_service.postgres.update_ml_shadow_tracking(
                            setup_id,
                            {
                                "shadow_fill_time_ms": fill_duration_ms,
                                "shadow_fill_candle_volume_ratio": pos.fill_candle_volume_ratio,
                                "shadow_slippage_estimate_pct": pos.slippage_estimate_pct,
                                "shadow_fill_candle_ts": int(getattr(candle, "timestamp", 0)),
                                "shadow_fill_candle_tf": getattr(candle, "timeframe", None),
                            },
                        )

                    logger.info(
                        f"Shadow fill: {pos.setup_type} {pos.pair} {pos.direction} "
                        f"entry={pos.entry_price:.2f} fill_time={fill_duration_ms/1000:.0f}s "
                        f"vol_ratio={pos.fill_candle_volume_ratio:.2f} "
                        f"slippage={pos.slippage_estimate_pct*100:.3f}%"
                    )

                    self._notify_fill(pos)
                    dirty = True  # fill transition — persist at end of tick

                    # Check if this same candle also hit TP or SL
                    outcome = self._check_tp_sl(candle, pos)
                    if outcome:
                        self._resolve(pos, outcome, resolve_candle=candle)
                        resolved.append(setup_id)

            else:
                # Phase 2: Filled — waiting for TP, SL, or time stop.
                # Per-signal time_stop_seconds takes precedence; otherwise fall
                # back to the global SHADOW_TRADE_TIMEOUT_HOURS.
                if pos.time_stop_seconds > 0:
                    trade_timeout_s = pos.time_stop_seconds
                    timeout_outcome = "shadow_time_stop"
                else:
                    trade_timeout_s = settings.SHADOW_TRADE_TIMEOUT_HOURS * 3600
                    timeout_outcome = "shadow_timeout"
                if (now - pos.fill_time) > trade_timeout_s:
                    # Timeout — compute PnL at current price
                    self._resolve(pos, timeout_outcome, exit_price=candle.close, resolve_candle=candle)
                    resolved.append(setup_id)
                    continue

                outcome = self._check_tp_sl(candle, pos)
                if outcome:
                    self._resolve(pos, outcome, resolve_candle=candle)
                    resolved.append(setup_id)

        for sid in resolved:
            del self._positions[sid]
        if resolved or dirty or self._dirty_from_inner_checks:
            self._save_to_redis()
            self._dirty_from_inner_checks = False

    def _check_tp_sl(self, candle, pos: ShadowPosition) -> str | None:
        """Delegate to shared pnl_engine. See `shared/pnl_engine.py`.

        be_confirm_closes=0 preserves legacy behavior (any TP1 touch arms BE).
        Batch 1 will bump this to 1 after backtest validation.
        """
        engine_pos = Position(
            direction=pos.direction,
            entry_price=pos.entry_price,
            sl_price=pos.sl_price,
            tp1_price=pos.tp1_price,
            tp2_price=pos.tp2_price,
            position_size=pos.position_size,
            filled=True,
            tp1_touched=pos.tp1_touched,
            be_confirm_closes=settings.BE_CONFIRM_CLOSES,
        )
        slice_ = CandleSlice(high=candle.high, low=candle.low, close=candle.close)
        outcome = pnl_step(engine_pos, slice_)

        # Sync state back to ShadowPosition
        if engine_pos.tp1_touched and not pos.tp1_touched:
            pos.tp1_touched = True
            pos.sl_price = engine_pos.sl_price
            # Flag the outer check_candle loop to persist at tick end
            # instead of issuing a Redis write per inner TP1 transition.
            self._dirty_from_inner_checks = True
            logger.info(
                f"Shadow TP1 touched: {pos.setup_type} {pos.pair} {pos.direction} "
                f"— SL moved to breakeven (entry={pos.entry_price:.2f})"
            )

        if outcome == Outcome.PENDING:
            return None
        return f"shadow_{outcome.value}"

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
        self, pos: ShadowPosition, outcome: str,
        exit_price: float | None = None,
        resolve_candle=None,
    ) -> None:
        """Write theoretical outcome to ml_setups.

        resolve_candle: the specific candle that triggered resolution. When
        provided, its OHLC + timestamp + timeframe are persisted for
        deterministic replay (migration 17).
        """
        # Determine exit price and PnL
        if exit_price is None:
            if outcome == "shadow_tp":
                exit_price = pos.tp2_price
            elif outcome == "shadow_sl":
                exit_price = pos.sl_price
            elif outcome == "shadow_breakeven":
                exit_price = pos.entry_price
            else:
                exit_price = pos.entry_price  # no fill or unknown

        if pos.filled and pos.entry_price > 0:
            pnl = compute_pnl(
                entry=pos.entry_price, exit_price=exit_price,
                size=pos.position_size, direction=pos.direction,
                fee_rate=settings.TRADING_FEE_RATE,
            )
            pnl_usd = pnl.net_usd
            pnl_pct = pnl.pct
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
            "shadow_breakeven": "breakeven",
            "shadow_timeout": "timeout",
            "shadow_time_stop": "time_stop",
            "shadow_no_fill": "no_fill",
        }

        if self._data_service and self._data_service.postgres:
            trace = {}
            if resolve_candle is not None:
                trace = {
                    "resolve_candle_ts": int(getattr(resolve_candle, "timestamp", 0)) or None,
                    "resolve_candle_tf": getattr(resolve_candle, "timeframe", None),
                    "resolve_candle_high": float(resolve_candle.high),
                    "resolve_candle_low": float(resolve_candle.low),
                    "resolve_candle_close": float(resolve_candle.close),
                }
            try:
                ok = self._data_service.postgres.update_ml_setup_outcome(
                    setup_id=pos.setup_id,
                    outcome_type=outcome,
                    pnl_pct=pnl_pct,
                    pnl_usd=pnl_usd,
                    actual_entry=pos.entry_price if pos.filled else None,
                    actual_exit=exit_price if pos.filled else None,
                    exit_reason=exit_reason_map.get(outcome, outcome),
                    fill_duration_ms=fill_duration_ms,
                    trade_duration_ms=trade_duration_ms,
                    **trace,
                )
                self._emit_metric(
                    "shadow_outcome_resolved_ok" if ok else "shadow_outcome_resolved_error",
                    1, pair=pos.pair, labels={"outcome": outcome},
                )
            except Exception as e:
                logger.error(f"Shadow outcome write failed: {pos.setup_id} {e}")
                self._emit_metric(
                    "shadow_outcome_resolved_error", 1,
                    pair=pos.pair, labels={"outcome": outcome},
                )

        status = "WIN" if pnl_usd > 0 else "LOSS" if pnl_usd < 0 else "FLAT"
        logger.info(
            f"Shadow resolved: {pos.setup_type} {pos.pair} {pos.direction} "
            f"outcome={outcome} pnl=${pnl_usd:.2f} ({pnl_pct*100:.2f}%) "
            f"[{status}] fill_vol_ratio={pos.fill_candle_volume_ratio:.2f} "
            f"slippage={pos.slippage_estimate_pct*100:.3f}%"
        )

        self._notify_resolve(pos, outcome, pnl_usd, pnl_pct, status)

    def _cleanup_orphaned_db_rows(self) -> None:
        """Resolve DB rows stuck with NULL outcome — lost on restart."""
        self._last_orphan_cleanup = time.time()
        max_age = settings.SHADOW_ENTRY_TIMEOUT_HOURS + settings.SHADOW_TRADE_TIMEOUT_HOURS
        if self._data_service and self._data_service.postgres:
            count = self._data_service.postgres.resolve_orphaned_shadow_setups(
                max_age_hours=max_age,
            )
            if count:
                logger.info(f"Cleaned up {count} orphaned shadow DB rows on startup")

    # --- Redis persistence ---

    def _save_to_redis(self) -> None:
        """Persist active shadow positions to Redis (fire-and-forget)."""
        redis = self._get_redis()
        if redis is None:
            return
        try:
            data = {sid: asdict(pos) for sid, pos in self._positions.items()}
            redis.set_bot_state(
                "shadow_positions", json.dumps(data), ttl=self._REDIS_TTL,
            )
        except Exception as e:
            logger.warning(f"Failed to save shadow positions to Redis: {e}")
            self._emit_metric("shadow_redis_save_error", 1)

    def _load_from_redis(self) -> None:
        """Restore active shadow positions from Redis on startup.

        Per-record isolation: a single bad record (e.g. schema drift between
        the snapshot and the current ShadowPosition fields) must NOT abort the
        whole restore. That was the orphan-leak root cause — one unparseable
        field dropped EVERY in-flight position, which then aged out as
        `shadow_orphaned`. Each record is parsed in its own try/except;
        failures are logged + counted, the rest still restore.

        Instrumentation (snapshot count + restored/expired/failed + metrics)
        lets the next restart confirm which loss mechanism actually fires.
        """
        redis = self._get_redis()
        if redis is None:
            # Logged (not silent) so every restart leaves a restore breadcrumb —
            # an empty/absent snapshot must be distinguishable from "ran fine".
            logger.info("Shadow restore from Redis: skipped — Redis unavailable")
            return
        try:
            raw = redis.get_bot_state("shadow_positions")
        except Exception as e:
            logger.warning(f"Failed to read shadow positions from Redis: {e}")
            self._emit_metric("shadow_redis_load_error", 1)
            return
        if not raw:
            logger.info("Shadow restore from Redis: empty snapshot — 0 positions to restore")
            return
        try:
            data = json.loads(raw)
        except Exception as e:
            logger.warning(f"Failed to parse shadow positions snapshot: {e}")
            self._emit_metric("shadow_redis_load_error", 1)
            return

        now = time.time()
        max_age = (
            settings.SHADOW_ENTRY_TIMEOUT_HOURS
            + settings.SHADOW_TRADE_TIMEOUT_HOURS
        ) * 3600
        raw_count = len(data)
        restored = skipped_expired = failed = 0
        for sid, fields in data.items():
            try:
                pos = ShadowPosition(**fields)
            except Exception as e:
                failed += 1
                logger.error(
                    f"Shadow restore: dropping unparseable record {sid}: {e}"
                )
                self._emit_metric(
                    "shadow_redis_load_dropped", 1, labels={"reason": "parse_error"},
                )
                continue
            # Skip positions that have expired since the last save.
            if (now - pos.detection_time) > max_age:
                skipped_expired += 1
                self._emit_metric(
                    "shadow_redis_load_dropped", 1, labels={"reason": "expired"},
                )
                continue
            self._positions[sid] = pos
            restored += 1

        if restored:
            self._emit_metric("shadow_redis_restored", restored)
        logger.info(
            f"Shadow restore from Redis: {raw_count} in snapshot -> "
            f"{restored} restored, {skipped_expired} expired-skipped, "
            f"{failed} parse-failed"
        )

    def _get_redis(self):
        """Get Redis store, or None if unavailable."""
        if (self._data_service and self._data_service.redis
                and self._data_service.redis._client):
            return self._data_service.redis
        return None

    def _notify_detection(self, pos: ShadowPosition) -> None:
        """Send Telegram alert when a shadow setup starts tracking."""
        if self._notifier is None:
            return
        # Benchmarks: only resolution alerts ship to Telegram (TRACKING/FILL
        # silenced) — they co-emit with every Engine 1 detection and would
        # triple the alert volume without adding lifecycle signal.
        if pos.setup_type.startswith("bench_engine1_"):
            return
        short_pair = pos.pair.replace("/USDT", "")
        msg = (
            f"\U0001f47b <b>SHADOW TRACKING</b>\n"
            f"{short_pair} {pos.direction.upper()} ({pos.setup_type})\n"
            f"Entry: ${pos.entry_price:,.2f} | SL: ${pos.sl_price:,.2f} | TP: ${pos.tp2_price:,.2f}\n"
            f"R:R {pos.initial_rr:.1f} | Risk: ${pos.initial_risk_usd:.2f}/${pos.target_risk_usd:.2f}\n"
            f"Margin: ${pos.margin:.0f} | Notional: ${pos.notional_usd:.0f} | Lev: {pos.leverage:.1f}x"
        )
        asyncio.ensure_future(self._notifier.send(msg))

    def _notify_fill(self, pos: ShadowPosition) -> None:
        """Send Telegram alert when a shadow position is theoretically filled."""
        if self._notifier is None:
            return
        if pos.setup_type.startswith("bench_engine1_"):
            return
        short_pair = pos.pair.replace("/USDT", "")
        msg = (
            f"\U0001f47b <b>SHADOW FILL</b>\n"
            f"{short_pair} {pos.direction.upper()} ({pos.setup_type})\n"
            f"Entry: ${pos.entry_price:,.2f} | SL: ${pos.sl_price:,.2f}\n"
            f"TP: ${pos.tp2_price:,.2f} | Risk: ${pos.initial_risk_usd:.2f}/${pos.target_risk_usd:.2f}\n"
            f"Margin: ${pos.margin:.0f} | Notional: ${pos.notional_usd:.0f} | Lev: {pos.leverage:.1f}x"
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
        r_multiple = pnl_usd / pos.initial_risk_usd if pos.initial_risk_usd > 0 else 0.0
        msg = (
            f"{emoji} <b>SHADOW {outcome_label}</b>\n"
            f"{short_pair} {pos.direction.upper()} ({pos.setup_type})\n"
            f"P&amp;L: ${pnl_usd:+.2f} ({pnl_pct*100:+.2f}%) | R: {r_multiple:+.2f}\n"
            f"Risk: ${pos.initial_risk_usd:.2f}/${pos.target_risk_usd:.2f}"
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
