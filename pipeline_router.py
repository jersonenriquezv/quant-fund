"""Pipeline core — the confirmed-candle callback and its routing logic.

Extracted from main.py (Refactor Phase 6, docs/plans/main-py-split-phase6.md).
This is the heart of the bot: `on_candle_confirmed` is wired into the
DataService as the per-candle entry point, fanning out to shadow tracking,
the Dual Thrust hook, the HTF-campaign path, and the SMC intraday pipeline
(`_process_pipeline_setup`). The dormant engine1 live gate lives here too,
relocated verbatim — its flag (`ENGINE1_LIVE_GATED_ENABLED`) stays OFF.

State/services reached via the shared `rt` singleton; persistence + ML
instrumentation imported from their leaf modules. Pure relocation: function
bodies are unchanged.
"""

import asyncio
import json
import time

from config.settings import settings, QUICK_SETUP_TYPES, AI_BYPASS_SETUP_TYPES
from shared.logger import setup_logger
from shared.models import Candle, AIDecision
from shared.ml_features import extract_risk_context
from data_service.data_integrity import DataServiceState, can_trade_setup
from execution_service.dual_thrust_shadow import (
    format_telegram as dual_thrust_format_telegram)
from pipeline_runtime import rt
from persistence import (
    _emit_metric,
    _persist_risk_event,
    _log_trade_rejection,
    _persist_ai_decision,
    _persist_ai_pre_filter,
)
from ml_instrumentation import (
    _ml_log_setup,
    _ml_resolve_outcome,
    _engine1_score_log,
    _engine1_kill_check,
    _engine1_emit_kill_alert,
)

logger = setup_logger("pipeline_router")

# Setup deduplication cache — prevents sending the same setup to Claude every 5m candle.
# Key: (pair, direction, setup_type, entry_price), Value: unix timestamp of last eval.
# The cache itself lives on the shared runtime (rt.setup_dedup_cache); these TTLs
# gate its re-evaluation cadence (live vs shadow).
_SETUP_DEDUP_TTL_SECONDS = 3600  # 1 hour — prevents re-sending same setup while limit order is pending
_SHADOW_DEDUP_TTL_SECONDS = 300  # 5 min — shadow is data collection, only dedup same-candle repeats


def _publish_strategy_state(pair: str) -> None:
    """Publish active OBs and HTF bias to Redis for the dashboard."""
    if rt.strategy_service is None or rt.data_service is None:
        return
    try:
        redis = rt.data_service.redis

        # Collect OBs for all pairs
        all_obs = []
        for p in settings.TRADING_PAIRS:
            for ob in rt.strategy_service.get_active_order_blocks(p):
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
        bias = {p: rt.strategy_service.get_htf_bias(p) for p in settings.TRADING_PAIRS}
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

    if rt.strategy_service is None:
        return

    # Shadow monitor: evaluate all tracked shadow positions against this candle
    if rt.shadow_monitor is not None:
        rt.shadow_monitor.check_candle(candle.pair, candle)

    # Dual Thrust shadow (order-free): on each confirmed ETH 4h candle, replay
    # the validated brain + harness fill model on fresh OKX REST 4h bars and
    # record a theoretical flip position. Fetch is blocking (ccxt) so run in an
    # executor; an engine error must never break the pipeline.
    if (settings.DUAL_THRUST_SHADOW_ENABLED and rt.dual_thrust_shadow is not None
            and candle.pair == "ETH/USDT" and candle.timeframe == "4h"):
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, rt.dual_thrust_shadow.on_candle, candle)
            # Push each new theoretical flip/SL to Telegram. Direct send (not the
            # muted alert path): flips are rare + the signal the operator wants
            # during the soak. Order-free — no live trade is implied.
            if result is not None and result.new_trades:
                # Persist completed flips/SLs (idempotent upsert — the tracker
                # re-replays the whole window on restart, re-emitting historical
                # trades as "new"; the first replay after a deploy backfills).
                if rt.data_service is not None and rt.data_service.postgres is not None:
                    await loop.run_in_executor(
                        None, rt.data_service.postgres.store_dt_shadow_trades,
                        result.new_trades, candle.pair, candle.timeframe)
                if rt.notifier is not None:
                    for tr in result.new_trades:
                        await rt.notifier.send(dual_thrust_format_telegram(tr, candle.pair))
        except Exception as e:
            logger.error(f"Dual Thrust shadow hook error: {e}")

    # ============================================================
    # HTF Campaign path — 4H candles trigger campaign evaluation
    # ============================================================
    if (settings.HTF_CAMPAIGN_ENABLED
            and candle.timeframe == settings.HTF_CAMPAIGN_SIGNAL_TF
            and rt.campaign_monitor is not None):

        if rt.campaign_monitor.has_active_campaign(candle.pair):
            # Active campaign on this pair — evaluate pyramid add
            c = rt.campaign_monitor.get_campaign(candle.pair)
            if c is not None and c.phase == "active":
                await rt.campaign_monitor.evaluate_add(c, candle)
        else:
            # No active campaign on this pair — check for new HTF setup
            await _evaluate_htf_pipeline(candle)

        _emit_metric("pipeline_latency_ms", (time.monotonic() - pipeline_start) * 1000, candle.pair)
        # Don't return — still publish strategy state below and allow
        # intraday pipeline to check blocking logic

    # Block intraday when HTF campaign active on this pair
    if (settings.HTF_CAMPAIGN_ENABLED
            and rt.campaign_monitor is not None
            and rt.campaign_monitor.has_active_campaign(candle.pair)):
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

    # Multi-signal collection: evaluate_all() runs the SAME state-update pass
    # as evaluate() and returns ALL valid setups in detection order.
    # Live execution still consumes at most ONE setup per candle (the first
    # ENABLED one); shadow logging consumes every SHADOW_MODE setup.
    # See docs/strategy_redesign_2026_04.md §Phase 0.5.
    all_setups = rt.strategy_service.evaluate_all(candle.pair, candle)
    _publish_strategy_state(candle.pair)

    # Position Guardian — evaluate open positions against live market conditions
    # Only run when data is clean (RUNNING state) to avoid false early closes
    if (settings.POSITION_GUARDIAN_ENABLED
            and rt.execution_service is not None
            and rt.execution_service._guardian is not None
            and rt.data_service is not None
            and rt.data_service.state == DataServiceState.RUNNING):
        try:
            recent = rt.data_service.get_candles(candle.pair, candle.timeframe, count=20)
            snapshot = rt.data_service.get_market_snapshot(candle.pair)
            cvd = snapshot.cvd if snapshot else None
            await rt.execution_service._guardian.evaluate(candle.pair, candle, recent, cvd)
        except Exception as e:
            logger.error(f"Position Guardian error: {e}")

    # Scalp shadow signals (docs/plans/scalp_shadow_v1.md). Independent of
    # the SMC cascade — appended to the multi-setup list so each gets routed
    # through _process_pipeline_setup (shadow path). evaluate_scalp returns
    # at most one TradeSetup per call and is gated by SCALP_SHADOW_ENABLED.
    if settings.SCALP_SHADOW_ENABLED:
        scalp_setup = rt.strategy_service.evaluate_scalp(candle.pair, candle)
        if scalp_setup is not None:
            all_setups = list(all_setups) + [scalp_setup]

    if not all_setups:
        return

    rt.last_setup_detected_time = time.time()

    live_executed = False
    for setup in all_setups:
        live_taken = await _process_pipeline_setup(
            setup, candle, allow_live=not live_executed,
        )
        if live_taken:
            live_executed = True

    _emit_metric("pipeline_latency_ms", (time.monotonic() - pipeline_start) * 1000, candle.pair)


async def _process_pipeline_setup(setup, candle: Candle, *, allow_live: bool) -> bool:
    """Run dedup → ml-log → shadow OR live pipeline for a single setup.

    Returns True if this setup consumed the candle's live-execution slot
    (caller uses the flag to lock further live runs in the same candle).
    Returns False otherwise — including all shadow paths, dedup hits,
    risk rejections, and skipped-because-allow_live=False.
    """
    logger.info(
        f"Trade setup detected: type={setup.setup_type} pair={setup.pair} "
        f"direction={setup.direction} entry={setup.entry_price:.2f} "
        f"sl={setup.sl_price:.2f} tp1={setup.tp1_price:.2f} "
        f"confluences={setup.confluences}"
    )

    # --- Data integrity gate ---
    snapshot = None
    if rt.data_service is not None:
        snapshot = rt.data_service.get_market_snapshot(candle.pair)
        cvd_state = rt.data_service.get_cvd_state(candle.pair)
        allowed, reason = can_trade_setup(
            setup.setup_type, snapshot.health, rt.data_service.state, cvd_state,
        )
        if not allowed:
            logger.info(
                f"Data gate: {reason} | {setup.setup_type} {setup.pair}"
            )
            _ml_log_setup(setup, candle)
            _ml_resolve_outcome(setup.setup_id, "data_blocked")
            return False

    # Dedup: block identical setups within TTL (covers ALL setup types).
    # Key is (pair, direction, setup_type) — already per-setup-type, so
    # multi-emit (engines + legacy + benchmarks on same candle) does not
    # mutually deduplicate. Live and shadow share this cache by design;
    # `dedup_ttl` differs per shadow-vs-live to control re-evaluation cadence.
    dedup_key = (setup.pair, setup.direction, setup.setup_type)
    now = time.time()
    last_eval = rt.setup_dedup_cache.get(dedup_key)
    is_shadow = setup.setup_type in settings.SHADOW_MODE_SETUPS
    dedup_ttl = _SHADOW_DEDUP_TTL_SECONDS if is_shadow else _SETUP_DEDUP_TTL_SECONDS
    if last_eval and (now - last_eval) < dedup_ttl:
        logger.debug(
            f"Setup dedup (pipeline): {setup.pair} {setup.direction} "
            f"{setup.setup_type} entry={setup.entry_price:.2f} — "
            f"already processed {int(now - last_eval)}s ago, skipping"
        )
        return False

    # --- ML: capture feature snapshot AFTER dedup (dedup adds no ML value) ---
    features = _ml_log_setup(setup, candle)

    # --- engine1 meta-label scoring + LIVE gate (Phase 2) ---
    # Score the setup in-process; if the live gate is ON and the score clears
    # the frozen cutoff, route this engine1 setup to REAL execution instead of
    # shadow. The kill switch can still force it back to shadow.
    # docs/plans/engine1-ml-filter-live.md §Phase 2.
    engine1_live = False
    if setup.setup_type == "engine1_trend_pullback" and features is not None:
        score = _engine1_score_log(setup, features)
        if (settings.ENGINE1_LIVE_GATED_ENABLED
                and score is not None
                and score >= settings.ENGINE1_SCORE_CUTOFF):
            kill, kill_reason = _engine1_kill_check()
            if kill:
                logger.warning(
                    f"engine1 live gate: score {score:.4f} eligible but KILL "
                    f"SWITCH active ({kill_reason}) — routing to shadow"
                )
                await _engine1_emit_kill_alert(kill_reason or "threshold breached")
            else:
                engine1_live = True
                logger.info(
                    f"engine1 LIVE gate: {setup.pair} {setup.direction} "
                    f"score={score:.4f} >= {settings.ENGINE1_SCORE_CUTOFF} "
                    f"— routing to REAL execution (risk=${settings.ENGINE1_RISK_USD})"
                )

    # --- Shadow mode: data collection — track ALL setups, minimize filtering ---
    # engine1 live-gated setups skip shadow and fall through to the live path.
    if (setup.setup_type in settings.SHADOW_MODE_SETUPS
            and not engine1_live
            and rt.shadow_monitor is not None):
        # Direction filter: reject proven-broken directions (e.g. setup_a long 5% WR)
        allowed_dirs = settings.SHADOW_DIRECTION_FILTER.get(setup.setup_type)
        if allowed_dirs is not None and setup.direction not in allowed_dirs:
            logger.debug(
                f"Shadow direction filter: {setup.setup_type} {setup.direction} "
                f"not in {allowed_dirs} — skipping"
            )
            _ml_resolve_outcome(setup.setup_id, "shadow_direction_filtered")
            return False
        # Pair filter: quarantine setups to research pairs (BTC+ETH for d_*)
        allowed_pairs = settings.SHADOW_PAIR_FILTER.get(setup.setup_type)
        if allowed_pairs is not None and setup.pair not in allowed_pairs:
            logger.debug(
                f"Shadow pair filter: {setup.setup_type} {setup.pair} "
                f"not in {allowed_pairs} — skipping"
            )
            _ml_resolve_outcome(setup.setup_id, "shadow_pair_filtered")
            return False
        # Risk check: run but do NOT gate on result. Log as ML feature only.
        risk_approval = None
        if rt.risk_service is not None:
            risk_approval = rt.risk_service.check(
                setup, dry_run=True,
                capital_override=settings.effective_shadow_capital,
            )

            # Persist risk check result to ml_setups (ML feature, not a gate)
            if rt.data_service is not None and rt.data_service.postgres is not None:
                rt.data_service.postgres.update_ml_risk_check(
                    setup.setup_id,
                    approved=risk_approval.approved,
                    reason=risk_approval.reason,
                )

            if not risk_approval.approved:
                logger.debug(
                    f"Shadow risk would reject: {setup.setup_type} {setup.pair} "
                    f"{setup.direction} — {risk_approval.reason} (tracking anyway)"
                )

        # Fetch orderbook snapshot for fill quality estimation
        ob_snapshot = None
        if rt.data_service is not None:
            ob_snapshot = rt.data_service.get_orderbook_snapshot(setup.pair)

        accepted = rt.shadow_monitor.add_shadow(
            setup, orderbook=ob_snapshot, risk_approval=risk_approval,
        )
        rt.setup_dedup_cache[dedup_key] = time.time()
        if not accepted:
            _ml_resolve_outcome(setup.setup_id, "shadow_dedup")
            return False
        logger.info(
            f"Shadow mode: {setup.setup_type} {setup.pair} {setup.direction} "
            f"entry={setup.entry_price:.2f} sl={setup.sl_price:.2f} "
            f"risk_ok={risk_approval.approved if risk_approval else 'N/A'} "
            f"— tracking (${settings.effective_shadow_capital} virtual, basis={settings.SHADOW_CAPITAL_BASIS})"
        )
        return False

    # --- Live path begins. Multi-emit guard: at most one live execution
    #     per candle. If a prior setup already consumed the slot, skip.
    if not allow_live:
        logger.info(
            f"Live slot already taken this candle — skipping {setup.setup_type} "
            f"{setup.pair} {setup.direction}"
        )
        return False

    # --- Emergency halt: block new live trades while monitoring continues ---
    if settings.TRADING_HALTED:
        logger.warning(
            f"TRADING HALTED: {setup.setup_type} {setup.pair} {setup.direction} "
            f"— new trades blocked (env TRADING_HALTED=true)"
        )
        _ml_resolve_outcome(setup.setup_id, "trading_halted")
        return False

    # Pre-check: can this pair meet exchange minimum order size?
    min_size = settings.MIN_ORDER_SIZES.get(setup.pair, 0)
    if min_size > 0:
        capital = rt.risk_service._state.get_capital()
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
            return False

    # Layer 3: AI Service — Claude filter
    decision = None
    if (setup.setup_type in QUICK_SETUP_TYPES
            or setup.setup_type in AI_BYPASS_SETUP_TYPES
            or engine1_live):
        if setup.setup_type in QUICK_SETUP_TYPES:
            reason = "data-driven quick setup"
        elif engine1_live:
            reason = "engine1 ML-score gate (frozen model, no AI filter)"
        else:
            reason = "AI bypass (pending recalibration)"
        decision = AIDecision(
            confidence=1.0,
            approved=True,
            reasoning=f"{reason} ({setup.setup_type})",
            adjustments={},
            warnings=[],
        )
        logger.info(f"AI bypass: {setup.setup_type} — {reason}")
    elif rt.ai_service is not None and rt.data_service is not None:
        decision = await _evaluate_with_claude(setup, candle)
        if decision is None:
            _ml_resolve_outcome(setup.setup_id, "ai_rejected")
            return False
        if not decision.approved:
            _ml_resolve_outcome(setup.setup_id, "ai_rejected")
            return False

    # Layer 4: Risk Service (pass AI confidence for bet sizing)
    approval = None
    ai_conf = decision.confidence if decision else 1.0
    # engine1 live trades risk a fixed $ (ENGINE1_RISK_USD) so the kill line is
    # a concrete number; all other setups use RISK_PER_TRADE × capital.
    engine1_risk_usd = settings.ENGINE1_RISK_USD if engine1_live else None
    if rt.risk_service is not None:
        approval = rt.risk_service.check(
            setup, ai_confidence=ai_conf, risk_usd=engine1_risk_usd,
        )
        if not approval.approved:
            logger.info(f"Risk rejected: {approval.reason}")
            if rt.alert_manager:
                await rt.alert_manager.notify_setup_rejected(
                    setup.pair, setup.setup_type, setup.direction,
                    "Risk", approval.reason or "unknown",
                )
            _persist_risk_event("trade_rejected", {
                "pair": setup.pair,
                "direction": setup.direction,
                "reason": approval.reason,
            })
            # Log to trade_rejections table for journal analysis
            _log_trade_rejection(setup, approval.reason or "unknown")
            # ML: store risk check result + resolve outcome
            if rt.data_service is not None and rt.data_service.postgres is not None:
                rt.data_service.postgres.update_ml_risk_check(
                    setup.setup_id,
                    approved=False,
                    reason=approval.reason,
                )
            _ml_resolve_outcome(
                setup.setup_id, "risk_rejected",
                risk_context=extract_risk_context(rt.risk_service),
            )
            # Cache structural risk rejections (SL too close, R:R too low) so the
            # same broken setup doesn't re-trigger Claude after dedup TTL expires.
            reason_lower = (approval.reason or "").lower()
            if "sl too close" in reason_lower or "risk distance" in reason_lower:
                rt.setup_dedup_cache[dedup_key] = time.time()
                logger.debug(f"Dedup: cached risk-rejected setup {dedup_key}")
            return False
        logger.info(
            f"Risk approved: size={approval.position_size:.6f} "
            f"leverage={approval.leverage:.2f}x risk={approval.risk_pct*100:.1f}%"
        )

    # Check if this OB already failed
    if rt.strategy_service is not None and rt.strategy_service.is_ob_failed(
        setup.pair, setup.sl_price, setup.entry_price
    ):
        logger.info(
            f"OB already failed: {setup.pair} entry={setup.entry_price:.2f} "
            f"sl={setup.sl_price:.2f} — skipping"
        )
        _ml_resolve_outcome(setup.setup_id, "risk_rejected")
        rt.setup_dedup_cache[dedup_key] = time.time()
        return False

    # Layer 5: Execution (or Signal). Reaching this point consumes the
    # candle's live-execution slot regardless of whether the order
    # actually places (a NetworkError may mean the order reached OKX
    # but we never saw the response — competing live setups must wait
    # the next candle).
    if approval is not None and approval.approved:
        if settings.SIGNAL_ONLY:
            logger.info(f"Signal mode: sending signal for {setup.pair} {setup.direction}")
            if rt.alert_manager is not None:
                await rt.alert_manager.notify_signal(setup, approval, decision)
            rt.setup_dedup_cache[dedup_key] = time.time()
        elif rt.execution_service is not None:
            ai_confidence = decision.confidence if decision else 0.0
            placed = await rt.execution_service.execute(setup, approval, ai_confidence)
            # Always dedup — even on failure. NetworkError may mean the order
            # reached OKX but we didn't get the response. Without this, the
            # next candle retries and creates a duplicate live order.
            rt.setup_dedup_cache[dedup_key] = time.time()
        return True

    return False


async def _evaluate_htf_pipeline(candle: Candle) -> None:
    """Evaluate a 4H candle for HTF campaign entry.

    Full pipeline: Strategy → AI → Risk → Campaign execution.
    """
    if (rt.strategy_service is None or rt.campaign_monitor is None
            or rt.risk_service is None):
        return

    # Block if intraday position active on this pair
    if (rt.execution_service is not None and rt.execution_service._monitor is not None
            and candle.pair in rt.execution_service._monitor.positions):
        pos = rt.execution_service._monitor.positions[candle.pair]
        if pos.phase != "closed":
            logger.debug(f"HTF blocked: intraday position active on {candle.pair}")
            return

    # Block if max campaigns reached or already have one on this pair
    if not rt.campaign_monitor.can_open_new_campaign():
        return
    if rt.campaign_monitor.has_active_campaign(candle.pair):
        return

    # Data integrity gate for HTF pipeline
    if rt.data_service is not None and rt.data_service.state != DataServiceState.RUNNING:
        logger.debug(f"HTF blocked: data service state={rt.data_service.state.name}")
        return

    setup = rt.strategy_service.evaluate_htf(candle.pair, candle)
    if setup is None:
        return

    logger.info(
        f"HTF campaign setup found: type={setup.setup_type} pair={setup.pair} "
        f"direction={setup.direction} entry={setup.entry_price:.2f} "
        f"sl={setup.sl_price:.2f} confluences={setup.confluences}"
    )

    # AI filter
    decision = None
    if rt.ai_service is not None and rt.data_service is not None:
        decision = await _evaluate_with_claude(setup, candle)
        if decision is None:
            return
        if not decision.approved:
            return

    # Risk check — uses same guardrails (DD, cooldown, max positions)
    approval = rt.risk_service.check(setup)
    if not approval.approved:
        logger.info(f"HTF risk rejected: {approval.reason}")
        return

    # Execute campaign (pass approval so campaign uses risk-approved sizing)
    ai_confidence = decision.confidence if decision else 0.0
    await rt.campaign_monitor.execute_campaign(setup, ai_confidence, approval=approval)


async def _evaluate_with_claude(setup, candle) -> "AIDecision | None":
    """Run pre-filter then Claude evaluation. Returns AIDecision or None if rejected/failed."""
    if rt.ai_service is None or rt.data_service is None:
        return None

    snapshot = rt.data_service.get_market_snapshot(candle.pair)

    # Pre-filter: reject obvious losers before calling Claude
    reject_reason = _pre_filter_for_claude(setup, snapshot)
    if reject_reason:
        logger.info(f"AI PRE-FILTERED: {reject_reason}")
        _persist_ai_pre_filter(setup, reject_reason)
        # Note: ai_pre_filtered notification removed — too noisy.
        return None

    # Claude evaluation
    claude_start = time.monotonic()
    decision = await rt.ai_service.evaluate(setup, snapshot)
    _emit_metric("claude_latency_ms", (time.monotonic() - claude_start) * 1000, setup.pair)

    # Attach snapshot health to adjustments for audit trail
    if snapshot.health is not None:
        decision.adjustments["snapshot_health"] = {
            "completeness_pct": snapshot.health.completeness_pct,
            "critical_ok": snapshot.health.critical_sources_healthy,
            "stale": list(snapshot.health.stale_sources),
            "missing": list(snapshot.health.missing_sources),
        }

    _persist_ai_decision(decision, setup)

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
