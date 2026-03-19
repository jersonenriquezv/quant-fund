"""
Entry point for the One-Man Quant Fund trading bot.

Single Python process running all 5 layers:
    Data Service → Strategy Service → AI Service → Risk Service → Execution Service

Runs in Docker via docker-compose.yml (bot + PostgreSQL + Redis).

Usage:
    python main.py
"""

import asyncio
import json
import signal
import sys
import time

from config.settings import settings, QUICK_SETUP_TYPES, AI_BYPASS_SETUP_TYPES
from shared.logger import setup_logger
from shared.models import Candle, AIDecision
from shared.ml_features import extract_setup_features, extract_risk_context
from data_service.service import DataService
from data_service.data_integrity import DataServiceState, can_trade_setup
from strategy_service import StrategyService
from ai_service import AIService
from risk_service import RiskService
from execution_service import ExecutionService
from execution_service.campaign_monitor import CampaignMonitor
from shared.notifier import TelegramNotifier
from shared.alert_manager import AlertManager
from data_service.liquidation_estimator import estimate_liquidation_levels

logger = setup_logger("main")

# Setup deduplication cache — prevents sending the same setup to Claude every 5m candle.
# Key: (pair, direction, setup_type, entry_price), Value: unix timestamp of last eval.
_setup_dedup_cache: dict[tuple, float] = {}
_SETUP_DEDUP_TTL_SECONDS = 3600  # 1 hour — prevents re-sending same setup while limit order is pending

# Module-level references set by main() so the callback can access them
_data_service: DataService | None = None
_strategy_service: StrategyService | None = None
_ai_service: AIService | None = None
_risk_service: RiskService | None = None
_execution_service: ExecutionService | None = None
_campaign_monitor: CampaignMonitor | None = None
_notifier: TelegramNotifier | None = None
_alert_manager: AlertManager | None = None

# Track last setup detection time for dry spell alerts
_last_setup_detected_time: float = 0.0
# Track ATR history per pair for volatility spike detection
_atr_history: dict[str, list[float]] = {}  # pair -> [recent ATR values]


# ================================================================
# Pipeline callback — triggered on every confirmed candle
# ================================================================

def _publish_strategy_state(pair: str) -> None:
    """Publish active OBs and HTF bias to Redis for the dashboard."""
    if _strategy_service is None or _data_service is None:
        return
    try:
        redis = _data_service.redis

        # Collect OBs for all pairs
        all_obs = []
        for p in settings.TRADING_PAIRS:
            for ob in _strategy_service.get_active_order_blocks(p):
                all_obs.append({
                    "timestamp": ob.timestamp,
                    "pair": ob.pair,
                    "timeframe": ob.timeframe,
                    "direction": ob.direction,
                    "high": ob.high,
                    "low": ob.low,
                    "body_high": ob.body_high,
                    "body_low": ob.body_low,
                    "entry_price": ob.entry_price,
                    "volume_ratio": ob.volume_ratio,
                })
        redis.set_bot_state("order_blocks", json.dumps(all_obs), ttl=600)

        # HTF bias for all pairs
        bias = {p: _strategy_service.get_htf_bias(p) for p in settings.TRADING_PAIRS}
        redis.set_bot_state("htf_bias", json.dumps(bias), ttl=600)
    except Exception as e:
        logger.error(f"Failed to publish strategy state to Redis: {e}")


async def on_candle_confirmed(candle: Candle) -> None:
    """Pipeline entry point: Data → Strategy → AI → Risk → Execution.

    Strategy Service evaluates LTF candles for SMC setups.
    AI/Risk/Execution layers are stubs — will be wired as they're built.
    """
    pipeline_start = time.monotonic()

    logger.info(
        f"Pipeline triggered: pair={candle.pair} tf={candle.timeframe} "
        f"close={candle.close} vol={candle.volume:.4f}"
    )

    if _strategy_service is None:
        return

    # ============================================================
    # HTF Campaign path — 4H candles trigger campaign evaluation
    # ============================================================
    if (settings.HTF_CAMPAIGN_ENABLED
            and candle.timeframe == settings.HTF_CAMPAIGN_SIGNAL_TF
            and _campaign_monitor is not None):

        if _campaign_monitor.has_active_campaign(candle.pair):
            # Active campaign on this pair — evaluate pyramid add
            c = _campaign_monitor.get_campaign(candle.pair)
            if c is not None and c.phase == "active":
                await _campaign_monitor.evaluate_add(c, candle)
        else:
            # No active campaign on this pair — check for new HTF setup
            await _evaluate_htf_pipeline(candle)

        _emit_metric("pipeline_latency_ms", (time.monotonic() - pipeline_start) * 1000, candle.pair)
        # Don't return — still publish strategy state below and allow
        # intraday pipeline to check blocking logic

    # Block intraday when HTF campaign active on this pair
    if (settings.HTF_CAMPAIGN_ENABLED
            and _campaign_monitor is not None
            and _campaign_monitor.has_active_campaign(candle.pair)):
        logger.debug(
            f"Intraday blocked: HTF campaign active on {candle.pair}"
        )
        _publish_strategy_state(candle.pair)
        return

    # ============================================================
    # Intraday path (unchanged)
    # ============================================================

    # Block new HTF campaign if intraday position active on same pair
    # (handled in _evaluate_htf_pipeline)

    setup = _strategy_service.evaluate(candle.pair, candle)
    _publish_strategy_state(candle.pair)

    # Position Guardian — evaluate open positions against live market conditions
    # Only run when data is clean (RUNNING state) to avoid false early closes
    if (settings.POSITION_GUARDIAN_ENABLED
            and _execution_service is not None
            and _execution_service._guardian is not None
            and _data_service is not None
            and _data_service.state == DataServiceState.RUNNING):
        try:
            recent = _data_service.get_candles(candle.pair, candle.timeframe, count=20)
            snapshot = _data_service.get_market_snapshot(candle.pair)
            cvd = snapshot.cvd if snapshot else None
            await _execution_service._guardian.evaluate(candle.pair, candle, recent, cvd)
        except Exception as e:
            logger.error(f"Position Guardian error: {e}")

    if setup is None:
        return

    global _last_setup_detected_time
    _last_setup_detected_time = time.time()

    logger.info(
        f"Trade setup detected: type={setup.setup_type} pair={setup.pair} "
        f"direction={setup.direction} entry={setup.entry_price:.2f} "
        f"sl={setup.sl_price:.2f} tp1={setup.tp1_price:.2f} "
        f"confluences={setup.confluences}"
    )

    # --- Data integrity gate ---
    if _data_service is not None and _data_service.state != DataServiceState.RUNNING:
        logger.info(
            f"Data gate: service={_data_service.state.name} | "
            f"{setup.setup_type} {setup.pair}"
        )
        _ml_log_setup(setup, candle)
        _ml_resolve_outcome(setup.setup_id, "data_blocked")
        return

    if _data_service is not None:
        snapshot = _data_service.get_market_snapshot(candle.pair)
        cvd_state = _data_service.get_cvd_state(candle.pair)
        allowed, reason = can_trade_setup(
            setup.setup_type, snapshot.health, _data_service.state, cvd_state,
        )
        if not allowed:
            logger.info(
                f"Data gate: {reason} | {setup.setup_type} {setup.pair}"
            )
            _ml_log_setup(setup, candle)
            _ml_resolve_outcome(setup.setup_id, "data_blocked")
            return

    # Dedup: block identical setups within TTL (covers ALL setup types, not just Claude)
    # NOTE: entry_price intentionally excluded — same pair/direction/type within TTL
    # is the same trade idea even if the OB recalculates to a slightly different price.
    # Including price caused duplicate orders (e.g. 73038 vs 72937 = different keys).
    dedup_key = (setup.pair, setup.direction, setup.setup_type)
    now = time.time()
    last_eval = _setup_dedup_cache.get(dedup_key)
    if last_eval and (now - last_eval) < _SETUP_DEDUP_TTL_SECONDS:
        logger.debug(
            f"Setup dedup (pipeline): {setup.pair} {setup.direction} "
            f"{setup.setup_type} entry={setup.entry_price:.2f} — "
            f"already processed {int(now - last_eval)}s ago, skipping"
        )
        return

    # --- ML: capture feature snapshot AFTER dedup (dedup adds no ML value) ---
    _ml_log_setup(setup, candle)

    # Pre-check: can this pair meet exchange minimum order size?
    min_size = settings.MIN_ORDER_SIZES.get(setup.pair, 0)
    if min_size > 0:
        capital = _risk_service._state.get_capital()
        if settings.FIXED_TRADE_MARGIN > 0:
            max_margin = settings.FIXED_TRADE_MARGIN
        else:
            max_margin = capital * settings.TRADE_CAPITAL_PCT
        max_notional = max_margin * settings.MAX_LEVERAGE
        max_position = max_notional / setup.entry_price
        if max_position < min_size:
            logger.info(
                f"Setup skipped: {setup.pair} min order {min_size} > "
                f"max position {max_position:.6f} "
                f"(need ${min_size * setup.entry_price:.0f} notional, "
                f"have ${max_notional:.0f} at {settings.MAX_LEVERAGE}x)"
            )
            _ml_resolve_outcome(setup.setup_id, "risk_rejected")
            return

    # Layer 3: AI Service — Claude filter
    decision = None
    if setup.setup_type in QUICK_SETUP_TYPES or setup.setup_type in AI_BYPASS_SETUP_TYPES:
        reason = ("data-driven quick setup" if setup.setup_type in QUICK_SETUP_TYPES
                  else "AI bypass (pending recalibration)")
        decision = AIDecision(
            confidence=1.0,
            approved=True,
            reasoning=f"{reason} ({setup.setup_type})",
            adjustments={},
            warnings=[],
        )
        logger.info(f"AI bypass: {setup.setup_type} — {reason}")
    elif _ai_service is not None and _data_service is not None:
        decision = await _evaluate_with_claude(setup, candle)
        if decision is None:
            _ml_resolve_outcome(setup.setup_id, "ai_rejected")
            return
        if not decision.approved:
            _ml_resolve_outcome(setup.setup_id, "ai_rejected")
            return

    # Layer 4: Risk Service (pass AI confidence for bet sizing)
    approval = None
    ai_conf = decision.confidence if decision else 1.0
    if _risk_service is not None:
        approval = _risk_service.check(setup, ai_confidence=ai_conf)
        if not approval.approved:
            logger.info(f"Risk rejected: {approval.reason}")
            if _alert_manager:
                await _alert_manager.notify_setup_rejected(
                    setup.pair, setup.setup_type, setup.direction,
                    "Risk", approval.reason or "unknown",
                )
            _persist_risk_event("trade_rejected", {
                "pair": setup.pair,
                "direction": setup.direction,
                "reason": approval.reason,
            })
            # ML: resolve with risk context at rejection time
            _ml_resolve_outcome(
                setup.setup_id, "risk_rejected",
                risk_context=extract_risk_context(_risk_service),
            )
            # Cache structural risk rejections (SL too close, R:R too low) so the
            # same broken setup doesn't re-trigger Claude after dedup TTL expires.
            reason_lower = (approval.reason or "").lower()
            if "sl too close" in reason_lower or "risk distance" in reason_lower:
                _setup_dedup_cache[dedup_key] = time.time()
                logger.debug(f"Dedup: cached risk-rejected setup {dedup_key}")
            return
        logger.info(
            f"Risk approved: size={approval.position_size:.6f} "
            f"leverage={approval.leverage:.2f}x risk={approval.risk_pct*100:.1f}%"
        )

    # Check if this OB already failed
    if _strategy_service is not None and _strategy_service.is_ob_failed(
        setup.pair, setup.sl_price, setup.entry_price
    ):
        logger.info(
            f"OB already failed: {setup.pair} entry={setup.entry_price:.2f} "
            f"sl={setup.sl_price:.2f} — skipping"
        )
        _ml_resolve_outcome(setup.setup_id, "risk_rejected")
        _setup_dedup_cache[dedup_key] = time.time()
        return

    # Layer 5: Execution (or Signal)
    if approval is not None and approval.approved:
        if settings.SIGNAL_ONLY:
            logger.info(f"Signal mode: sending signal for {setup.pair} {setup.direction}")
            if _alert_manager is not None:
                await _alert_manager.notify_signal(setup, approval, decision)
            _setup_dedup_cache[dedup_key] = time.time()
        elif _execution_service is not None:
            ai_confidence = decision.confidence if decision else 0.0
            placed = await _execution_service.execute(setup, approval, ai_confidence)
            # Always dedup — even on failure. NetworkError may mean the order
            # reached OKX but we didn't get the response. Without this, the
            # next candle retries and creates a duplicate live order.
            _setup_dedup_cache[dedup_key] = time.time()

    _emit_metric("pipeline_latency_ms", (time.monotonic() - pipeline_start) * 1000, candle.pair)


# ================================================================
# ML instrumentation helpers (fire-and-forget)
# ================================================================

def _ml_log_setup(setup, candle: Candle) -> None:
    """Log setup features to ml_setups table at detection time."""
    if _data_service is None or _data_service.postgres is None:
        return
    try:
        snapshot = _data_service.get_market_snapshot(candle.pair)
        current_price = candle.close
        recent_candles = _data_service.get_candles(candle.pair, candle.timeframe, count=20)
        features = extract_setup_features(setup, snapshot, current_price, recent_candles)
        # Add fields that come from setup but aren't in the feature dict yet
        features["timestamp"] = setup.timestamp
        features["tp1_price"] = setup.tp1_price
        features["tp2_price"] = setup.tp2_price

        # Risk context at detection time (before risk check)
        risk_ctx = None
        if _risk_service is not None:
            risk_ctx = extract_risk_context(_risk_service)

        ok = _data_service.postgres.insert_ml_setup(
            setup_id=setup.setup_id,
            features=features,
            risk_context=risk_ctx,
            feature_version=settings.ML_FEATURE_VERSION,
        )
        _emit_metric("ml_setup_insert_ok" if ok else "ml_setup_insert_error", 1, setup.pair)
    except Exception as e:
        logger.error(f"ML setup logging failed: {e}")
        _emit_metric("ml_setup_insert_error", 1, setup.pair)


def _ml_resolve_outcome(setup_id: str, outcome_type: str, **kwargs) -> None:
    """Resolve an ml_setup outcome (fire-and-forget)."""
    if not setup_id or _data_service is None or _data_service.postgres is None:
        return
    try:
        ok = _data_service.postgres.update_ml_setup_outcome(
            setup_id=setup_id,
            outcome_type=outcome_type,
            **kwargs,
        )
        _emit_metric("ml_outcome_update_ok" if ok else "ml_outcome_update_error", 1)
    except Exception as e:
        logger.error(f"ML outcome resolution failed: {setup_id} {e}")
        _emit_metric("ml_outcome_update_error", 1)


# ================================================================
# HTF Campaign pipeline
# ================================================================

async def _evaluate_htf_pipeline(candle: Candle) -> None:
    """Evaluate a 4H candle for HTF campaign entry.

    Full pipeline: Strategy → AI → Risk → Campaign execution.
    """
    if (_strategy_service is None or _campaign_monitor is None
            or _risk_service is None):
        return

    # Block if intraday position active on this pair
    if (_execution_service is not None and _execution_service._monitor is not None
            and candle.pair in _execution_service._monitor.positions):
        pos = _execution_service._monitor.positions[candle.pair]
        if pos.phase != "closed":
            logger.debug(f"HTF blocked: intraday position active on {candle.pair}")
            return

    # Block if max campaigns reached or already have one on this pair
    if not _campaign_monitor.can_open_new_campaign():
        return
    if _campaign_monitor.has_active_campaign(candle.pair):
        return

    # Data integrity gate for HTF pipeline
    if _data_service is not None and _data_service.state != DataServiceState.RUNNING:
        logger.debug(f"HTF blocked: data service state={_data_service.state.name}")
        return

    setup = _strategy_service.evaluate_htf(candle.pair, candle)
    if setup is None:
        return

    logger.info(
        f"HTF campaign setup found: type={setup.setup_type} pair={setup.pair} "
        f"direction={setup.direction} entry={setup.entry_price:.2f} "
        f"sl={setup.sl_price:.2f} confluences={setup.confluences}"
    )

    # AI filter
    decision = None
    if _ai_service is not None and _data_service is not None:
        decision = await _evaluate_with_claude(setup, candle)
        if decision is None:
            return
        if not decision.approved:
            return

    # Risk check — uses same guardrails (DD, cooldown, max positions)
    approval = _risk_service.check(setup)
    if not approval.approved:
        logger.info(f"HTF risk rejected: {approval.reason}")
        return

    # Execute campaign (pass approval so campaign uses risk-approved sizing)
    ai_confidence = decision.confidence if decision else 0.0
    await _campaign_monitor.execute_campaign(setup, ai_confidence, approval=approval)


# ================================================================
# Metrics helper (fire-and-forget to PostgreSQL for Grafana)
# ================================================================

def _emit_metric(name: str, value: float, pair: str | None = None, labels: dict | None = None) -> None:
    """Write an operational metric to PostgreSQL. Non-blocking, swallows errors."""
    if _data_service is None:
        return
    try:
        _data_service.postgres.insert_metric(name, value, pair=pair, labels=labels)
    except Exception:
        pass  # Fire-and-forget — never block the pipeline


# ================================================================
# Persistence helpers (called from pipeline callback)
# ================================================================

def _persist_ai_decision(trade_id, decision, setup) -> None:
    """Write AI decision to PostgreSQL (fire-and-forget)."""
    if _data_service is None:
        return
    try:
        _data_service.postgres.insert_ai_decision(
            trade_id=trade_id,
            confidence=decision.confidence,
            reasoning=decision.reasoning,
            adjustments=decision.adjustments,
            warnings=list(decision.warnings),
            pair=setup.pair,
            direction=setup.direction,
            setup_type=setup.setup_type,
            approved=decision.approved,
        )
    except Exception as e:
        logger.error(f"Failed to persist AI decision: {e}")


async def _evaluate_with_claude(setup, candle) -> "AIDecision | None":
    """Run pre-filter then Claude evaluation. Returns AIDecision or None if rejected/failed."""
    if _ai_service is None or _data_service is None:
        return None

    snapshot = _data_service.get_market_snapshot(candle.pair)

    # Pre-filter: reject obvious losers before calling Claude
    reject_reason = _pre_filter_for_claude(setup, snapshot)
    if reject_reason:
        logger.info(f"AI PRE-FILTERED: {reject_reason}")
        _persist_ai_pre_filter(setup, reject_reason)
        # Note: ai_pre_filtered notification removed — too noisy.
        return None

    # Claude evaluation
    claude_start = time.monotonic()
    decision = await _ai_service.evaluate(setup, snapshot)
    _emit_metric("claude_latency_ms", (time.monotonic() - claude_start) * 1000, setup.pair)

    # Attach snapshot health to adjustments for audit trail
    if snapshot.health is not None:
        decision.adjustments["snapshot_health"] = {
            "completeness_pct": snapshot.health.completeness_pct,
            "critical_ok": snapshot.health.critical_sources_healthy,
            "stale": list(snapshot.health.stale_sources),
            "missing": list(snapshot.health.missing_sources),
        }

    _persist_ai_decision(None, decision, setup)

    if not decision.approved:
        logger.info(
            f"AI rejected: confidence={decision.confidence:.2f} "
            f"reason={decision.reasoning}"
        )
    else:
        logger.info(f"AI approved: confidence={decision.confidence:.2f}")

    return decision


def _pre_filter_for_claude(setup, snapshot) -> str | None:
    """Deterministic pre-filter before Claude API call.

    Returns rejection reason string if setup should be rejected, None if it should
    proceed to Claude. Conservative: skips checks when data is unavailable.

    Setup C skips funding check (extreme funding IS the signal).
    """
    # HTF bias is no longer a hard gate — Claude evaluates it as context.
    # LTF structure (CHoCH/BOS) drives trade direction; strong counter-trend
    # setups with clear LTF structure can proceed to Claude for evaluation.

    threshold = settings.FUNDING_EXTREME_THRESHOLD

    # Check 1: Funding rate extreme against trade direction
    # Skip for Setup C — extreme funding IS the signal
    if setup.setup_type != "setup_c":
        if snapshot.funding is not None and snapshot.funding.rate is not None:
            rate = snapshot.funding.rate
            if setup.direction == "long" and rate > threshold:
                return f"Funding extreme against long ({rate*100:.4f}% > {threshold*100:.4f}%)"
            if setup.direction == "short" and rate < -threshold:
                return f"Funding extreme against short ({rate*100:.4f}% < -{threshold*100:.4f}%)"

    # Check 2: Fear & Greed extreme against trade direction
    if snapshot.news_sentiment is not None:
        fg = snapshot.news_sentiment.score
        if setup.direction == "long" and fg < settings.NEWS_EXTREME_FEAR_THRESHOLD:
            return f"Extreme Fear (F&G={fg}) — rejecting long"
        if setup.direction == "short" and fg > settings.NEWS_EXTREME_GREED_THRESHOLD:
            return f"Extreme Greed (F&G={fg}) — rejecting short"

    # Check 3: CVD strong divergence against trade direction
    if snapshot.cvd is not None:
        buy_vol = snapshot.cvd.buy_volume
        sell_vol = snapshot.cvd.sell_volume
        total_vol = buy_vol + sell_vol
        if total_vol > 0:
            buy_dominance = buy_vol / total_vol
            if setup.direction == "long" and buy_dominance < 0.40:
                return f"CVD divergence against long (buy dominance {buy_dominance*100:.1f}% < 40%)"
            if setup.direction == "short" and buy_dominance > 0.60:
                return f"CVD divergence against short (buy dominance {buy_dominance*100:.1f}% > 60%)"

    return None


def _persist_ai_pre_filter(setup, reason: str) -> None:
    """Write synthetic AI decision for pre-filter rejection (audit trail)."""
    if _data_service is None:
        return
    try:
        _data_service.postgres.insert_ai_decision(
            trade_id=None,
            confidence=0.0,
            reasoning=f"Pre-filter: {reason}",
            adjustments=None,
            warnings=[],
            pair=setup.pair,
            direction=setup.direction,
            setup_type=setup.setup_type,
            approved=False,
        )
    except Exception as e:
        logger.error(f"Failed to persist AI pre-filter: {e}")



def _persist_risk_event(event_type: str, details: dict) -> None:
    """Write risk event to PostgreSQL (fire-and-forget)."""
    if _data_service is None:
        return
    try:
        _data_service.postgres.insert_risk_event(event_type, details)
    except Exception as e:
        logger.error(f"Failed to persist risk event: {e}")


# ================================================================
# Daily summary loop (replaces hourly status — available on Grafana)
# ================================================================

_bot_start_time: float = 0.0


async def _daily_summary_loop() -> None:
    """Placeholder — daily summary disabled. Kept for future use."""
    # Daily summary notification removed — user wants only order/close alerts.
    # Grafana dashboards provide the same info on demand.
    while True:
        await asyncio.sleep(86400)


# ================================================================
# Trading session alerts
# ================================================================

# Sessions defined as (name, start_hour_utc, end_hour_utc, label)
TRADING_SESSIONS = [
    ("asia", 0, 9, "00:00-09:00"),
    ("europe", 7, 16, "07:00-16:00"),
    ("us", 13, 22, "13:00-22:00"),
]


async def _session_alert_loop() -> None:
    """Send Telegram alert when a major trading session opens."""
    # Track which sessions we've already alerted today
    from datetime import datetime, timezone
    alerted: dict[str, int] = {}  # session_name -> day_of_year

    while True:
        try:
            now = datetime.now(timezone.utc)
            day = now.timetuple().tm_yday

            for name, start_hour, _, hours_label in TRADING_SESSIONS:
                if now.hour == start_hour and now.minute < 5:
                    if alerted.get(name) != day and _alert_manager:
                        await _alert_manager.notify_session_open(name, hours_label)
                        alerted[name] = day
        except Exception as e:
            logger.error(f"Session alert error: {e}")

        await asyncio.sleep(60)  # Check every minute


# ================================================================
# Dry spell alert — no setups detected in X hours
# ================================================================

_DRY_SPELL_THRESHOLD_HOURS = 4  # Alert after 4 hours of no setups
_dry_spell_alerted: bool = False


async def _dry_spell_loop() -> None:
    """Alert when no setups detected for extended period."""
    global _dry_spell_alerted

    # Wait for bot to warm up
    await asyncio.sleep(300)

    while True:
        try:
            if _last_setup_detected_time > 0 and _alert_manager:
                hours_since = (time.time() - _last_setup_detected_time) / 3600
                if hours_since >= _DRY_SPELL_THRESHOLD_HOURS and not _dry_spell_alerted:
                    await _alert_manager.notify_dry_spell(
                        hours_since, settings.TRADING_PAIRS,
                    )
                    _dry_spell_alerted = True
                elif hours_since < _DRY_SPELL_THRESHOLD_HOURS:
                    _dry_spell_alerted = False  # Reset when setup found
            elif _last_setup_detected_time == 0 and _alert_manager:
                # Bot just started, no setup yet — check if it's been long enough
                hours_since = (time.time() - _bot_start_time) / 3600
                if hours_since >= _DRY_SPELL_THRESHOLD_HOURS and not _dry_spell_alerted:
                    await _alert_manager.notify_dry_spell(
                        hours_since, settings.TRADING_PAIRS,
                    )
                    _dry_spell_alerted = True
        except Exception as e:
            logger.error(f"Dry spell alert error: {e}")

        await asyncio.sleep(600)  # Check every 10 min


# ================================================================
# Volatility spike + funding extreme alerts
# ================================================================

_vol_spike_cooldown: dict[str, float] = {}  # pair -> last alert time
_funding_extreme_cooldown: dict[str, float] = {}  # pair -> last alert time
_VOL_SPIKE_RATIO = 2.0  # ATR must be 2x above rolling average
_VOL_ALERT_COOLDOWN = 3600  # 1 hour between alerts per pair
_FUNDING_ALERT_COOLDOWN = 7200  # 2 hours between alerts per pair


async def _market_monitor_loop() -> None:
    """Monitor volatility spikes and funding extremes across all pairs."""
    # Wait for data to populate
    await asyncio.sleep(120)

    while True:
        try:
            now = time.time()

            for pair in settings.TRADING_PAIRS:
                if _data_service is None or _alert_manager is None:
                    continue

                # --- Volatility spike detection ---
                candles = _data_service.get_candles(pair, "5m", 100)
                if candles and len(candles) >= 50:
                    # Simple ATR: avg(high-low) over recent vs older window
                    recent = candles[-14:]
                    older = candles[-50:-14]
                    current_atr = sum(c.high - c.low for c in recent) / len(recent)
                    avg_atr = sum(c.high - c.low for c in older) / len(older)
                    price = candles[-1].close

                    if avg_atr > 0 and price > 0:
                        current_pct = current_atr / price
                        avg_pct = avg_atr / price
                        ratio = current_pct / avg_pct

                        last_alert = _vol_spike_cooldown.get(pair, 0)
                        if ratio >= _VOL_SPIKE_RATIO and (now - last_alert) > _VOL_ALERT_COOLDOWN:
                            await _alert_manager.notify_volatility_spike(
                                pair, current_pct, avg_pct,
                            )
                            _vol_spike_cooldown[pair] = now

                # --- Funding rate extreme detection ---
                funding = _data_service.get_funding_rate(pair)
                if funding and abs(funding.rate) >= settings.FUNDING_EXTREME_THRESHOLD:
                    last_alert = _funding_extreme_cooldown.get(pair, 0)
                    if (now - last_alert) > _FUNDING_ALERT_COOLDOWN:
                        direction = "long" if funding.rate < 0 else "short"
                        await _alert_manager.notify_funding_extreme(
                            pair, funding.rate, direction,
                        )
                        _funding_extreme_cooldown[pair] = now

                # --- Drawdown warning ---
                if _risk_service is not None:
                    daily_dd = _risk_service._state.get_daily_dd_pct()
                    dd_threshold = settings.MAX_DAILY_DRAWDOWN * settings.DD_WARNING_THRESHOLD
                    if daily_dd >= dd_threshold and daily_dd < settings.MAX_DAILY_DRAWDOWN:
                        await _alert_manager.notify_dd_warning(
                            daily_dd, settings.MAX_DAILY_DRAWDOWN,
                        )

        except Exception as e:
            logger.error(f"Market monitor error: {e}")

        await asyncio.sleep(300)  # Check every 5 min


async def _liquidation_alert_loop() -> None:
    """Send top liquidation clusters near price every 4 hours via Telegram."""
    # Wait 60s for data to populate on startup
    await asyncio.sleep(60)

    while True:
        try:
            await _send_liquidation_alert()
        except Exception as e:
            logger.error(f"Liquidation alert error: {e}")

        await asyncio.sleep(4 * 3600)  # 4 hours


async def _send_liquidation_alert() -> None:
    """Compute and send top liquidation clusters for all pairs."""
    if _data_service is None or _alert_manager is None:
        return

    all_clusters: list[dict] = []

    for pair in settings.TRADING_PAIRS:
        candles = _data_service.get_candles(pair, "5m", settings.LIQ_CANDLE_COUNT)
        oi = _data_service.get_open_interest(pair)
        if not candles or oi is None or oi.oi_usd <= 0:
            continue

        current_price = candles[-1].close
        if current_price <= 0:
            continue

        bins = estimate_liquidation_levels(candles, oi.oi_usd, pair)
        if not bins:
            continue

        # Find top clusters above price (short liquidations — fuel for up moves)
        above = []
        for b in bins:
            if b.price > current_price and b.liq_short_usd > 0:
                dist_pct = (b.price - current_price) / current_price * 100
                if dist_pct <= 10:  # Within 10% above
                    above.append({
                        "price": b.price,
                        "usd": b.liq_short_usd,
                        "dist_pct": dist_pct,
                    })

        # Find top clusters below price (long liquidations — fuel for down moves)
        below = []
        for b in bins:
            if b.price < current_price and b.liq_long_usd > 0:
                dist_pct = (b.price - current_price) / current_price * 100
                if dist_pct >= -10:  # Within 10% below
                    below.append({
                        "price": b.price,
                        "usd": b.liq_long_usd,
                        "dist_pct": dist_pct,
                    })

        # Sort by USD size and take top 3 each
        above.sort(key=lambda x: x["usd"], reverse=True)
        below.sort(key=lambda x: x["usd"], reverse=True)

        all_clusters.append({
            "pair": pair,
            "price": current_price,
            "above": above[:3],
            "below": below[:3],
        })

    if all_clusters:
        await _alert_manager.notify_liquidation_clusters(all_clusters)


# ================================================================
# Config validation
# ================================================================

def validate_config() -> bool:
    """Check that minimum required config is present."""
    ok = True

    if not settings.OKX_API_KEY:
        logger.warning("OKX_API_KEY not set — trading will be disabled (market data still works)")

    if not settings.ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — AI filter disabled, trades will be auto-rejected")

    if settings.SIGNAL_ONLY:
        logger.info("SIGNAL MODE — signals only, no automatic execution")

    if settings.OKX_SANDBOX:
        logger.info("Running in DEMO mode (OKX sandbox / simulated trading)")
    else:
        logger.info("Running in LIVE mode (OKX mainnet)")

    logger.info(f"Trading pairs: {settings.TRADING_PAIRS}")
    logger.info(f"Timeframes: HTF={settings.HTF_TIMEFRAMES} LTF={settings.LTF_TIMEFRAMES}")
    logger.info(f"Risk per trade: {settings.RISK_PER_TRADE*100:.1f}%")
    logger.info(f"Max leverage: {settings.MAX_LEVERAGE}x")
    logger.info(f"Max daily DD: {settings.MAX_DAILY_DRAWDOWN*100:.1f}%")

    if settings.HTF_CAMPAIGN_ENABLED:
        logger.info(
            f"HTF campaigns ENABLED: signal={settings.HTF_CAMPAIGN_SIGNAL_TF} "
            f"bias={settings.HTF_CAMPAIGN_BIAS_TF} "
            f"initial_margin=${settings.HTF_INITIAL_MARGIN} "
            f"max_adds={settings.HTF_MAX_ADDS}"
        )

    return ok


# ================================================================
# Main
# ================================================================

async def main() -> None:
    logger.info("=" * 60)
    logger.info("ONE-MAN QUANT FUND — Starting")
    logger.info("=" * 60)

    if not validate_config():
        logger.error("Config validation failed. Exiting.")
        sys.exit(1)

    global _data_service, _strategy_service, _ai_service, _risk_service, _execution_service, _campaign_monitor, _notifier, _alert_manager

    # Create Telegram notifier + AlertManager wrapper
    _notifier = TelegramNotifier(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID)
    _alert_manager = AlertManager(_notifier)

    # Create DataService with pipeline callback + alert manager for whale alerts
    _data_service = DataService(on_candle_confirmed=on_candle_confirmed, alert_manager=_alert_manager)

    # Create StrategyService — Layer 2
    _strategy_service = StrategyService(_data_service)
    logger.info("Strategy Service initialized")

    # Create AIService — Layer 3
    _ai_service = AIService(_data_service)

    # Create RiskService — Layer 4 (capital from exchange balance or INITIAL_CAPITAL fallback)
    balance = _data_service.fetch_usdt_balance()
    if balance is not None and balance > 0:
        capital = balance
        logger.info(f"Capital from exchange: ${capital:.2f}")
    else:
        capital = settings.INITIAL_CAPITAL
        logger.warning(f"Could not fetch balance — using INITIAL_CAPITAL: ${capital:.2f}")
    _risk_service = RiskService(capital=capital, data_service=_data_service)

    # Reconcile drawdown from PostgreSQL (source of truth for realized PnL).
    # Catches cases where Redis state was lost or stale after restart.
    _risk_service._state.reconcile_drawdown_from_db(_data_service.postgres)

    # Create ExecutionService — Layer 5
    # on_sl_hit callback marks failed OBs so the same OB doesn't re-trigger
    def _on_sl_hit(pair: str, sl_price: float, entry_price: float) -> None:
        if _strategy_service is not None:
            _strategy_service.mark_ob_failed(pair, sl_price, entry_price)

    _execution_service = ExecutionService(
        _risk_service, _data_service, alert_manager=_alert_manager,
        on_sl_hit=_on_sl_hit
    )
    await _execution_service.start()

    # Create CampaignMonitor for HTF position trades (when enabled)
    if settings.HTF_CAMPAIGN_ENABLED and _execution_service._executor is not None:
        _campaign_monitor = CampaignMonitor(
            executor=_execution_service._executor,
            risk_service=_risk_service,
            strategy_service=_strategy_service,
            data_store=_data_service,
            alert_manager=_alert_manager,
        )
        _campaign_monitor.start()
        logger.info("HTF Campaign Monitor started")
    elif settings.HTF_CAMPAIGN_ENABLED:
        logger.warning("HTF campaigns enabled but execution disabled (no OKX key)")

    # Handle graceful shutdown
    shutdown_event = asyncio.Event()

    def handle_signal(sig, frame):
        sig_name = signal.Signals(sig).name
        logger.info(f"Received {sig_name} — initiating graceful shutdown")
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Start DataService in background
    data_task = asyncio.create_task(_data_service.start(), name="data_service")

    # Start daily summary loop + bot started notification
    global _bot_start_time
    _bot_start_time = time.time()
    status_task = asyncio.create_task(_daily_summary_loop(), name="daily_summary")
    liq_task = asyncio.create_task(_liquidation_alert_loop(), name="liquidation_alerts")
    session_task = asyncio.create_task(_session_alert_loop(), name="session_alerts")
    dry_spell_task = asyncio.create_task(_dry_spell_loop(), name="dry_spell_alerts")
    market_monitor_task = asyncio.create_task(_market_monitor_loop(), name="market_monitor")

    # Wait for shutdown signal
    await shutdown_event.wait()

    # Graceful shutdown
    logger.info("Shutting down...")
    if _campaign_monitor is not None:
        await _campaign_monitor.stop()
    if _execution_service is not None:
        await _execution_service.stop()
    if _ai_service is not None:
        await _ai_service.close()
    await _data_service.stop()

    # Cancel background tasks
    for task in [data_task, status_task, liq_task, session_task, dry_spell_task, market_monitor_task]:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    logger.info("=" * 60)
    logger.info("ONE-MAN QUANT FUND — Stopped")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
