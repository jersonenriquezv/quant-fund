"""
StrategyService Facade — Wires all SMC detectors into a single interface.

This is the ONLY class other services import from the strategy layer.
Main entry: evaluate(pair, trigger_candle) -> TradeSetup | None

Data flow:
    trigger_candle → fetch all candles from DataService →
    HTF analysis (4H/1H) → LTF analysis (15m/5m) →
    Setup A → Setup B → Setup F → Setup G → Quick (C/D/E/H) → TradeSetup | None
"""

import time
from typing import Optional

from config.settings import settings
from shared.logger import setup_logger
from shared.models import Candle, TradeSetup

from strategy_service.market_structure import MarketStructureAnalyzer
from strategy_service.order_blocks import OrderBlock, OrderBlockDetector
from strategy_service.fvg import FVGDetector
from strategy_service.liquidity import LiquidityAnalyzer
from strategy_service.setups import SetupEvaluator
from strategy_service.quick_setups import QuickSetupEvaluator
from strategy_service.scalp_setups import ScalpSetupEvaluator
from strategy_service.volume_profile import VolumeProfileAnalyzer
from strategy_service.engines.trend_pullback import TrendPullbackEngine

logger = setup_logger("strategy_service")


class StrategyService:
    """Strategy Service (Layer 2) — SMC pattern detection engine.

    Deterministic rules. No AI. Receives candle data from DataService,
    runs all SMC detectors, and produces TradeSetup objects for AI evaluation.

    Synchronous — CPU-bound analysis, no async needed.
    Called from async context via direct call (DataService reads from memory).
    """

    def __init__(self, data_service):
        """
        Args:
            data_service: DataService instance for fetching candles and snapshots.
        """
        self._data = data_service

        # Detectors — each maintains its own state between calls
        self._market_structure = MarketStructureAnalyzer()
        self._order_blocks = OrderBlockDetector()
        self._fvg = FVGDetector()
        self._liquidity = LiquidityAnalyzer()
        self._setups = SetupEvaluator()
        self._quick_setups = QuickSetupEvaluator()
        self._scalp_setups = ScalpSetupEvaluator()
        # Redesign engines (docs/strategy_redesign_2026_04.md §4)
        self._engine1 = TrendPullbackEngine()
        self._volume_profile = VolumeProfileAnalyzer(
            bin_count=settings.VP_BIN_COUNT,
            value_area_pct=settings.VP_VALUE_AREA_PCT,
            hvn_threshold=settings.VP_HVN_THRESHOLD,
            lvn_threshold=settings.VP_LVN_THRESHOLD,
        ) if settings.VP_ENABLED else None

        # Cached HTF bias per pair (updated on every evaluate call)
        self._cached_htf_bias: dict[str, str] = {}

        # Quick setup cooldown: (pair, setup_type) → last trigger timestamp
        self._quick_setup_last: dict[tuple[str, str], float] = {}

        # Failed OB tracking: set of (pair, ob_low, ob_high) that hit SL.
        # Prevents re-entering the same OB after a loss.
        self._failed_obs: set[tuple[str, float, float]] = set()

        # Track last 4H candle timestamp per pair to avoid redundant OB updates
        self._last_4h_ob_ts: dict[str, int] = {}

        # Cross-signal dedup for scalp shadow signals: pair => last fire ts
        # (monotonic time.time()). Prevents two scalp_* setup_types firing
        # within SCALP_DEDUP_WINDOW_SECONDS on the same pair.
        self._scalp_last_fire: dict[str, float] = {}

        # Per-pair orderbook cache for the scalp path. Avoids hitting OKX
        # REST every candle when only Signal 3 needs spread data. Entries
        # are tuples of (fetched_at_ts, orderbook_dict_or_None).
        self._scalp_ob_cache: dict[str, tuple[float, dict | None]] = {}

    @staticmethod
    def _shadow_scope_allows(setup: TradeSetup) -> bool:
        """Return whether a shadow setup is inside its research scope.

        Main.py still owns the generic shadow quarantine path and outcome
        labels. Engine 1 uses this earlier check so filtered primary signals
        do not co-emit orphan benchmark rows.
        """
        if setup.setup_type not in settings.SHADOW_MODE_SETUPS:
            return True

        allowed_pairs = settings.SHADOW_PAIR_FILTER.get(setup.setup_type)
        if allowed_pairs is not None and setup.pair not in allowed_pairs:
            return False

        allowed_dirs = settings.SHADOW_DIRECTION_FILTER.get(setup.setup_type)
        if allowed_dirs is not None and setup.direction not in allowed_dirs:
            return False

        return True

    def evaluate(self, pair: str,
                 trigger_candle: Candle) -> Optional[TradeSetup]:
        """Live-compatible single-setup entry point.

        Returns the first valid setup found (current contract) or None.
        Internally short-circuits via `_iterate_setups` so detectors and
        cooldowns mutate identically to the pre-refactor behavior:
        when the first match is found, subsequent setup evaluators
        (including quick-setup cooldowns) are NOT executed.
        """
        result: list[TradeSetup] = []

        def stop_on_first(setup: TradeSetup) -> bool:
            result.append(setup)
            return True  # short-circuit

        self._iterate_setups(pair, trigger_candle, stop_on_first)
        return result[0] if result else None

    def evaluate_all(self, pair: str,
                     trigger_candle: Candle) -> list[TradeSetup]:
        """Multi-setup entry point — used by shadow data collection.

        Runs the SAME single state-update pass as `evaluate()` and returns
        ALL valid setups found across the per-LTF + quick-setup loop, in
        detection order (A_15m → B_15m → F_15m → G_15m → D_5m).

        IMPORTANT: any side effect that fires on a matched setup
        (e.g. quick-setup cooldown) WILL fire here too — calling
        `evaluate_all()` may trigger D's cooldown when `evaluate()` would
        have short-circuited before reaching D. This is intentional: a
        detection at this candle is a real detection regardless of who
        consumes it. Live execution remains gated by `ENABLED_SETUPS`.
        """
        result: list[TradeSetup] = []

        def accumulate(setup: TradeSetup) -> bool:
            result.append(setup)
            return False  # continue iteration

        self._iterate_setups(pair, trigger_candle, accumulate)
        return result

    def _iterate_setups(self, pair: str, trigger_candle: Candle,
                        on_match) -> None:
        """Single state-update pass + setup evaluation loop.

        For each valid setup found, calls `on_match(setup)`. If the
        callback returns True, iteration stops (live single-setup mode).
        If it returns False, iteration continues (shadow multi-setup mode).

        Detector state updates (market structure, OBs, FVGs, liquidity,
        volume profile) run exactly ONCE regardless of mode — this is
        the property that makes evaluate() and evaluate_all() safe to
        call independently on the same candle without producing
        divergent OB/FVG/liquidity state.
        """
        # Only evaluate on LTF candles
        if trigger_candle.timeframe not in settings.LTF_TIMEFRAMES:
            return

        current_time_ms = int(time.time() * 1000)

        # ============================================================
        # Step 1: Fetch candle data from DataService
        # ============================================================
        candles_4h = self._data.get_candles(pair, "4h", 100)
        candles_1h = self._data.get_candles(pair, "1h", 100)
        candles_15m = self._data.get_candles(pair, "15m", 200)
        candles_5m = self._data.get_candles(pair, "5m", 200)
        market_snapshot = self._data.get_market_snapshot(pair)

        # ============================================================
        # Step 2: HTF analysis — determine bias
        # ============================================================
        state_4h = self._market_structure.analyze(candles_4h, pair, "4h")
        state_1h = self._market_structure.analyze(candles_1h, pair, "1h")

        # Detect 4H OBs (used by manual calculator for suggested SL + swing setups fallback)
        # Only update when the latest 4H candle has changed to avoid redundant work
        if candles_4h:
            latest_4h_ts = candles_4h[-1].timestamp
            cache_key = f"{pair}:4h"
            if self._last_4h_ob_ts.get(cache_key) != latest_4h_ts:
                self._order_blocks.update(
                    candles_4h, state_4h.structure_breaks,
                    pair, "4h", current_time_ms,
                )
                self._last_4h_ob_ts[cache_key] = latest_4h_ts
                # Update Volume Profile on 4H candle change
                if self._volume_profile:
                    self._volume_profile.update(pair, candles_4h)

        # Detect 1H OBs for swing setups (1H OBs have structural significance vs 15m noise)
        active_obs_1h = []
        if candles_1h:
            active_obs_1h = self._order_blocks.update(
                candles_1h, state_1h.structure_breaks,
                pair, "1h", current_time_ms,
            )

        # Build HTF OB list for swing setups: prefer 1H, fall back to 4H
        active_obs_4h = self._order_blocks.get_active_obs(pair, "4h")
        swing_obs = active_obs_1h if active_obs_1h else active_obs_4h
        swing_ob_tf = "1h" if active_obs_1h else "4h"

        # Get volume profile for this pair
        volume_profile = self._volume_profile.get_profile(pair) if self._volume_profile else None

        htf_bias = self._determine_htf_bias(state_4h, state_1h)
        self._cached_htf_bias[pair] = htf_bias
        if htf_bias == "undefined":
            logger.debug(f"No HTF bias for {pair} — skipping "
                         f"(4h_trend={state_4h.trend} 1h_trend={state_1h.trend} "
                         f"4h_breaks={len(state_4h.structure_breaks)} "
                         f"1h_breaks={len(state_1h.structure_breaks)})")
            return

        logger.debug(f"HTF bias={htf_bias} for {pair} "
                     f"(4h={state_4h.trend} 1h={state_1h.trend})")

        # Update premium/discount zone from 4H data
        current_price = trigger_candle.close
        self._liquidity.update_premium_discount(
            candles_4h, state_4h.swing_highs, state_4h.swing_lows,
            pair, current_price, current_time_ms,
        )
        pd_zone = self._liquidity.get_pd_zone(pair)

        # ============================================================
        # Step 3: LTF analysis — run all detectors
        # ============================================================
        setup = None

        for ltf, candles in [("15m", candles_15m), ("5m", candles_5m)]:
            if not candles:
                continue

            # Market structure
            ltf_state = self._market_structure.analyze(candles, pair, ltf)

            # Order blocks (depends on structure breaks)
            active_obs = self._order_blocks.update(
                candles, ltf_state.structure_breaks,
                pair, ltf, current_time_ms,
            )

            # FVGs
            active_fvgs = self._fvg.update(
                candles, pair, ltf, current_time_ms,
            )

            # Liquidity levels and sweeps
            self._liquidity.update(
                candles, ltf_state.swing_highs, ltf_state.swing_lows,
                pair, ltf, market_snapshot, current_time_ms,
            )
            recent_sweeps = self._liquidity.get_recent_sweeps(pair, ltf)
            liq_levels = self._liquidity.get_levels(pair, ltf)

            # Diagnostic: log detected patterns for visibility
            n_breaks = len(ltf_state.structure_breaks)
            n_bos = sum(1 for b in ltf_state.structure_breaks if b.break_type == "bos")
            n_choch = sum(1 for b in ltf_state.structure_breaks if b.break_type == "choch")
            logger.debug(
                f"[{pair} {ltf}] patterns: breaks={n_breaks} "
                f"(bos={n_bos} choch={n_choch}) obs={len(active_obs)} "
                f"fvgs={len(active_fvgs)} sweeps={len(recent_sweeps)} "
                f"liq_levels={len(liq_levels)} price={candles[-1].close}"
            )
            if active_obs:
                for ob in active_obs:
                    logger.debug(
                        f"[{pair} {ltf}] OB: {ob.direction} "
                        f"range={ob.low:.2f}-{ob.high:.2f} "
                        f"entry={ob.entry_price:.2f} vol_ratio={ob.volume_ratio:.1f}x"
                    )

            # Skip swing setup evaluation for timeframes not in SWING_SETUP_TIMEFRAMES.
            # 5m OBs produce micro-SLs (<0.2%) that get eaten by commissions.
            # Detectors still ran above so quick setups (C/D/E) can use 5m data.
            if ltf not in settings.SWING_SETUP_TIMEFRAMES:
                continue

            # ============================================================
            # Step 4: Evaluate setups — A first, then B
            # Allow enabled OR shadow-mode setups through (main.py routes shadow to ShadowMonitor)
            # Swing setups use 1H/4H OBs (structural SLs) instead of 15m OBs (noise).
            # ============================================================
            setup = self._setups.evaluate_setup_a(
                structure_state=ltf_state,
                active_obs=swing_obs,
                recent_sweeps=recent_sweeps,
                pd_zone=pd_zone,
                market_snapshot=market_snapshot,
                candles=candles,
                pair=pair,
                htf_bias=htf_bias,
                liquidity_levels=liq_levels,
                swing_highs_htf=state_4h.swing_highs + state_1h.swing_highs,
                swing_lows_htf=state_4h.swing_lows + state_1h.swing_lows,
                volume_profile=volume_profile,
            )

            if setup is not None:
                if setup.setup_type not in settings.ENABLED_SETUPS and setup.setup_type not in settings.SHADOW_MODE_SETUPS:
                    logger.debug(f"Setup A detected but disabled (not in ENABLED_SETUPS or SHADOW_MODE_SETUPS)")
                    setup = None
                else:
                    reject = self._apply_expectancy_filters(setup, candles_15m, state_4h, state_1h)
                    if reject:
                        logger.info(f"Expectancy filter rejected: {setup.pair} {setup.setup_type} — {reject}")
                        setup = None
                    else:
                        setup = self._enrich_with_ob_depth(setup, candles_15m)
                        logger.info(
                            f"Setup A found: pair={pair} direction={setup.direction} "
                            f"entry={setup.entry_price:.2f} sl={setup.sl_price:.2f} "
                            f"tp1={setup.tp1_price:.2f} confluences={setup.confluences}"
                        )
                        if on_match(setup):
                            return

            setup = self._setups.evaluate_setup_b(
                structure_state=ltf_state,
                active_obs=swing_obs,
                active_fvgs=active_fvgs,
                pd_zone=pd_zone,
                market_snapshot=market_snapshot,
                candles=candles,
                pair=pair,
                htf_bias=htf_bias,
                liquidity_levels=liq_levels,
                swing_highs_htf=state_4h.swing_highs + state_1h.swing_highs,
                swing_lows_htf=state_4h.swing_lows + state_1h.swing_lows,
                volume_profile=volume_profile,
            )

            if setup is not None:
                if setup.setup_type not in settings.ENABLED_SETUPS and setup.setup_type not in settings.SHADOW_MODE_SETUPS:
                    logger.debug(f"Setup B detected but disabled (not in ENABLED_SETUPS or SHADOW_MODE_SETUPS)")
                    setup = None
                else:
                    reject = self._apply_expectancy_filters(setup, candles_15m, state_4h, state_1h)
                    if reject:
                        logger.info(f"Expectancy filter rejected: {setup.pair} {setup.setup_type} — {reject}")
                        setup = None
                    else:
                        setup = self._enrich_with_ob_depth(setup, candles_15m)
                        logger.info(
                            f"Setup B found: pair={pair} direction={setup.direction} "
                            f"entry={setup.entry_price:.2f} sl={setup.sl_price:.2f} "
                            f"tp1={setup.tp1_price:.2f} confluences={setup.confluences}"
                        )
                        if on_match(setup):
                            return

            # Setup F — Pure OB Retest (BOS + OB, no FVG required)
            setup = self._setups.evaluate_setup_f(
                structure_state=ltf_state,
                active_obs=swing_obs,
                pd_zone=pd_zone,
                market_snapshot=market_snapshot,
                candles=candles,
                pair=pair,
                htf_bias=htf_bias,
                liquidity_levels=liq_levels,
                swing_highs_htf=state_4h.swing_highs + state_1h.swing_highs,
                swing_lows_htf=state_4h.swing_lows + state_1h.swing_lows,
                volume_profile=volume_profile,
            )

            if setup is not None:
                if setup.setup_type not in settings.ENABLED_SETUPS and setup.setup_type not in settings.SHADOW_MODE_SETUPS:
                    logger.debug(f"Setup F detected but disabled (not in ENABLED_SETUPS or SHADOW_MODE_SETUPS)")
                    setup = None
                else:
                    reject = self._apply_expectancy_filters(setup, candles_15m, state_4h, state_1h)
                    if reject:
                        logger.info(f"Expectancy filter rejected: {setup.pair} {setup.setup_type} — {reject}")
                        setup = None
                    else:
                        setup = self._enrich_with_ob_depth(setup, candles_15m)
                        logger.info(
                            f"Setup F found: pair={pair} direction={setup.direction} "
                            f"entry={setup.entry_price:.2f} sl={setup.sl_price:.2f} "
                            f"tp1={setup.tp1_price:.2f} confluences={setup.confluences}"
                        )
                        if on_match(setup):
                            return

            # Setup G — Breaker Block Retest (skip evaluation if disabled)
            if "setup_g" in settings.ENABLED_SETUPS or "setup_g" in settings.SHADOW_MODE_SETUPS:
                breaker_blocks = self._order_blocks.get_breaker_blocks(pair, ltf)
                setup = self._setups.evaluate_setup_g(
                    breaker_blocks=breaker_blocks,
                    pd_zone=pd_zone,
                    market_snapshot=market_snapshot,
                    candles=candles,
                    pair=pair,
                    htf_bias=htf_bias,
                    liquidity_levels=liq_levels,
                    swing_highs_htf=state_4h.swing_highs + state_1h.swing_highs,
                    swing_lows_htf=state_4h.swing_lows + state_1h.swing_lows,
                    volume_profile=volume_profile,
                )

                if setup is not None:
                    reject = self._apply_expectancy_filters(setup, candles_15m, state_4h, state_1h)
                    if reject:
                        logger.info(f"Expectancy filter rejected: {setup.pair} {setup.setup_type} — {reject}")
                        setup = None
                    else:
                        setup = self._enrich_with_ob_depth(setup, candles_15m)
                        logger.info(
                            f"Setup G found: pair={pair} direction={setup.direction} "
                            f"entry={setup.entry_price:.2f} sl={setup.sl_price:.2f} "
                            f"tp1={setup.tp1_price:.2f} confluences={setup.confluences}"
                        )
                        if on_match(setup):
                            return

            # ============================================================
            # Redesign Engine 1 — Trend-Pullback / Impulse Retest (15m only).
            # Owns its own gates (entry distance, target space, net R:R) —
            # does NOT inherit `_apply_expectancy_filters`. Pair filter
            # (BTC+ETH only) lives in main.py via SHADOW_PAIR_FILTER, not here.
            # ============================================================
            from strategy_service.engines.trend_pullback import (
                SETUP_TYPE as ENGINE1_SETUP_TYPE,
            )
            if (ENGINE1_SETUP_TYPE in settings.ENABLED_SETUPS
                    or ENGINE1_SETUP_TYPE in settings.SHADOW_MODE_SETUPS):
                swings_htf_prices = [
                    s.price for s in state_4h.swing_highs + state_1h.swing_highs
                ] + [
                    s.price for s in state_4h.swing_lows + state_1h.swing_lows
                ]
                engine_setup = self._engine1.evaluate(
                    pair=pair,
                    candles=candles,
                    current_price=trigger_candle.close,
                    htf_bias=htf_bias,
                    swings_htf=swings_htf_prices,
                    ob_timeframe=ltf,
                )
                if engine_setup is not None:
                    if not self._shadow_scope_allows(engine_setup):
                        logger.debug(
                            f"Engine1 scope filtered: pair={pair} "
                            f"direction={engine_setup.direction}"
                        )
                        continue
                    logger.info(
                        f"Engine1 Trend-Pullback found: pair={pair} "
                        f"direction={engine_setup.direction} "
                        f"entry={engine_setup.entry_price:.2f} "
                        f"sl={engine_setup.sl_price:.2f} "
                        f"tp1={engine_setup.tp1_price:.2f} "
                        f"tp2={engine_setup.tp2_price:.2f} "
                        f"confluences={engine_setup.confluences}"
                    )
                    if on_match(engine_setup):
                        return
                    # Co-emit Engine 1 benchmarks (shadow-only, no-op when
                    # `on_match` short-circuited above for live `evaluate()`).
                    from strategy_service.engines.benchmarks import (
                        emit_engine1_benchmarks,
                    )
                    if emit_engine1_benchmarks(
                        engine_setup,
                        current_price=trigger_candle.close,
                        on_match=on_match,
                    ):
                        return

        # ============================================================
        # Step 5: Quick setups (D) — evaluated after the swing-setup loop.
        # In legacy `evaluate()` semantics this only fires when no swing
        # setup matched (short-circuit). Under `evaluate_all()` it always
        # runs, which is the desired multi-emit behavior.
        # ============================================================
        quick_setup = self._evaluate_quick_setups(
            pair, htf_bias, candles_5m, candles_15m, market_snapshot, pd_zone,
            state_4h, state_1h, volume_profile,
        )
        if quick_setup is not None:
            if quick_setup.setup_type not in settings.ENABLED_SETUPS and quick_setup.setup_type not in settings.SHADOW_MODE_SETUPS:
                logger.debug(f"{quick_setup.setup_type} detected but disabled")
            else:
                if on_match(quick_setup):
                    return

    def _evaluate_quick_setups(
        self,
        pair: str,
        htf_bias: str,
        candles_5m: list[Candle],
        candles_15m: list[Candle],
        market_snapshot,
        pd_zone,
        state_4h,
        state_1h,
        volume_profile,
    ) -> Optional[TradeSetup]:
        """Try quick setups D only. C/E/H removed 2026-04-13. Respects per-type cooldown."""
        if not candles_5m:
            return None

        now = time.time()

        # Setup C removed 2026-04-13: no OB anchor. Signal is now a confluence booster.
        # Setup E removed 2026-04-13: no OB anchor. Signal is now a confluence booster.

        # Setup D — LTF Structure Scalp (5m only)
        if not self._is_quick_cooldown_active(pair, "setup_d", now):
            state_5m = self._market_structure.get_state(pair, "5m")
            if state_5m is not None:
                active_obs_5m = self._order_blocks.get_active_obs(pair, "5m")
                setup = self._quick_setups.evaluate_setup_d(
                    pair, htf_bias, state_5m, active_obs_5m, pd_zone, candles_5m,
                    snapshot=market_snapshot,
                    swing_highs_htf=state_4h.swing_highs + state_1h.swing_highs,
                    swing_lows_htf=state_4h.swing_lows + state_1h.swing_lows,
                    volume_profile=volume_profile,
                )
                if setup is not None:
                    self._quick_setup_last[(pair, "setup_d")] = now
                    return setup

        # Setup H removed 2026-04-13: 0/13 WR, retail momentum chase.

        return None

    def _is_quick_cooldown_active(
        self, pair: str, setup_type: str, now: float,
    ) -> bool:
        """Check if cooldown is active for a quick setup type on a pair."""
        last = self._quick_setup_last.get((pair, setup_type))
        if last is None:
            return False
        return (now - last) < settings.QUICK_SETUP_COOLDOWN

    # ================================================================
    # Scalp shadow signals — independent of the main SMC cascade.
    # Plan: docs/plans/scalp_shadow_v1.md.
    # ================================================================

    def evaluate_scalp(self, pair: str,
                       trigger_candle: Candle) -> Optional[TradeSetup]:
        """Evaluate scalp shadow signals for a pair.

        Returns at most one TradeSetup. The caller is responsible for routing
        through the shadow pipeline (every scalp setup_type sits in
        SHADOW_MODE_SETUPS so the standard handler does the work).

        The detector is gated behind SCALP_SHADOW_ENABLED so this is a no-op
        until the experiment is turned on.

        Cross-signal dedup: if any scalp_* signal fired on this pair within
        SCALP_DEDUP_WINDOW_SECONDS, return None. Prevents two distinct scalp
        signals from firing on the same wick / event when their triggers
        line up. Same setup_type collisions are already covered by the
        pipeline-level dedup_cache in main.py.
        """
        if not settings.SCALP_SHADOW_ENABLED:
            return None

        now = time.time()
        last_fire = self._scalp_last_fire.get(pair, 0.0)
        if now - last_fire < settings.SCALP_DEDUP_WINDOW_SECONDS:
            logger.debug(
                f"Scalp dedup: {pair} — fired {now - last_fire:.1f}s ago "
                f"(window {settings.SCALP_DEDUP_WINDOW_SECONDS}s), skipping"
            )
            return None

        scalp_tf = settings.SCALP_TIMEFRAME
        # 50 candles: ADX(14) Wilder smoothing needs ~42 (period*3) and the
        # sweep_choch lookback needs 22; 50 covers both with margin.
        candles = self._data.get_candles(pair, scalp_tf, count=50)
        if not candles:
            return None
        market_snapshot = self._data.get_market_snapshot(pair)

        setup = self._scalp_setups.evaluate_liq_reclaim(
            pair, candles, market_snapshot,
        )
        if setup is not None:
            self._scalp_last_fire[pair] = now
            return setup

        # Cached orderbook — used by sweep_choch (book_imbalance fade gate,
        # v2) and by vol_cvd (spread chaos gate). Cached per-pair so the REST
        # call is paid at most once per SCALP_ORDERBOOK_CACHE_TTL_SECONDS.
        orderbook = self._get_cached_orderbook(pair, now)
        setup = self._scalp_setups.evaluate_sweep_choch(
            pair, candles, market_snapshot, orderbook=orderbook,
        )
        if setup is not None:
            self._scalp_last_fire[pair] = now
            return setup

        setup = self._scalp_setups.evaluate_vol_cvd_divergence(
            pair, candles, market_snapshot, orderbook=orderbook,
        )
        if setup is not None:
            self._scalp_last_fire[pair] = now
            return setup

        setup = self._scalp_setups.evaluate_funding_extreme(
            pair, candles, market_snapshot,
        )
        if setup is not None:
            self._scalp_last_fire[pair] = now
            return setup

        # Random control — frequency-matched baseline. Sits last so a real
        # signal always wins the slot when both would fire on the same candle.
        setup = self._scalp_setups.evaluate_random_baseline(
            pair, candles, market_snapshot,
        )
        if setup is not None:
            self._scalp_last_fire[pair] = now
            return setup

        return None

    def _get_cached_orderbook(self, pair: str, now: float) -> dict | None:
        """Return a cached orderbook snapshot for `pair`, refreshing if stale.

        The cache stores both successful and failed fetches (as None) so a
        broken exchange call doesn't trigger a REST hammer on every candle.
        TTL is `SCALP_ORDERBOOK_CACHE_TTL_SECONDS`.
        """
        ttl = settings.SCALP_ORDERBOOK_CACHE_TTL_SECONDS
        cached = self._scalp_ob_cache.get(pair)
        if cached is not None:
            fetched_at, ob = cached
            if (now - fetched_at) < ttl:
                return ob
        ob = self._data.get_orderbook_snapshot(pair)
        self._scalp_ob_cache[pair] = (now, ob)
        return ob

    # ================================================================
    # HTF Campaign — evaluate 4H setups with Daily bias
    # ================================================================

    def evaluate_htf(self, pair: str,
                     trigger_candle: Candle) -> Optional[TradeSetup]:
        """Evaluate a pair for HTF campaign setups on 4H candles.

        Uses Daily candles for bias (instead of 4H/1H used by intraday).
        Runs the same SMC detectors on 4H data with wider age/proximity params.
        """
        signal_tf = settings.HTF_CAMPAIGN_SIGNAL_TF  # "4h"
        bias_tf = settings.HTF_CAMPAIGN_BIAS_TF      # "1d"

        # Only evaluate on the signal timeframe
        if trigger_candle.timeframe != signal_tf:
            return None

        current_time_ms = int(time.time() * 1000)

        # Fetch candle data
        candles_daily = self._data.get_candles(pair, bias_tf, 100)
        candles_signal = self._data.get_candles(pair, signal_tf, 200)

        if not candles_signal or len(candles_signal) < 20:
            logger.debug(f"HTF: insufficient {signal_tf} candles for {pair}")
            return None

        # Daily bias (replaces 4H/1H used by intraday)
        if candles_daily and len(candles_daily) >= 10:
            state_daily = self._market_structure.analyze(candles_daily, pair, bias_tf)
            htf_bias = state_daily.trend
        else:
            # Fallback to 4H bias if no daily data
            state_4h = self._market_structure.analyze(candles_signal, pair, signal_tf)
            htf_bias = state_4h.trend

        if htf_bias == "undefined":
            logger.debug(f"HTF: no daily bias for {pair} — skipping")
            return None

        # Run detectors on signal timeframe with HTF params
        signal_state = self._market_structure.analyze(candles_signal, pair, signal_tf)

        # Use wider age/proximity for HTF OBs
        active_obs = self._order_blocks.update(
            candles_signal, signal_state.structure_breaks,
            pair, signal_tf, current_time_ms,
            max_age_hours=settings.HTF_OB_MAX_AGE_HOURS,
        )

        active_fvgs = self._fvg.update(
            candles_signal, pair, signal_tf, current_time_ms,
            max_age_hours=settings.HTF_FVG_MAX_AGE_HOURS,
        )

        # Premium/Discount from Daily swing range (not 4H)
        current_price = trigger_candle.close
        if candles_daily and len(candles_daily) >= 10:
            daily_state = self._market_structure.analyze(candles_daily, pair, bias_tf)
            self._liquidity.update_premium_discount(
                candles_daily, daily_state.swing_highs, daily_state.swing_lows,
                pair, current_price, current_time_ms,
            )
        pd_zone = self._liquidity.get_pd_zone(pair)

        # Liquidity levels and sweeps on signal TF
        market_snapshot = self._data.get_market_snapshot(pair)
        self._liquidity.update(
            candles_signal, signal_state.swing_highs, signal_state.swing_lows,
            pair, signal_tf, market_snapshot, current_time_ms,
        )
        recent_sweeps = self._liquidity.get_recent_sweeps(pair, signal_tf)
        liq_levels = self._liquidity.get_levels(pair, signal_tf)

        logger.debug(
            f"[HTF {pair} {signal_tf}] patterns: breaks={len(signal_state.structure_breaks)} "
            f"obs={len(active_obs)} fvgs={len(active_fvgs)} "
            f"sweeps={len(recent_sweeps)} bias={htf_bias}"
        )

        # Temporarily override settings for HTF evaluation
        orig_proximity = settings.OB_PROXIMITY_PCT
        orig_distance = settings.OB_MAX_DISTANCE_PCT
        orig_min_risk = settings.MIN_RISK_DISTANCE_PCT
        settings.OB_PROXIMITY_PCT = settings.HTF_OB_PROXIMITY_PCT
        settings.OB_MAX_DISTANCE_PCT = settings.HTF_OB_MAX_DISTANCE_PCT
        settings.MIN_RISK_DISTANCE_PCT = settings.HTF_MIN_RISK_DISTANCE_PCT

        try:
            # Evaluate setups A, B, F in order
            for eval_fn, setup_name, extra_args in [
                (self._setups.evaluate_setup_a, "A",
                 {"recent_sweeps": recent_sweeps}),
                (self._setups.evaluate_setup_b, "B",
                 {"active_fvgs": active_fvgs}),
                (self._setups.evaluate_setup_f, "F", {}),
            ]:
                kwargs = {
                    "structure_state": signal_state,
                    "active_obs": active_obs,
                    "pd_zone": pd_zone,
                    "market_snapshot": market_snapshot,
                    "candles": candles_signal,
                    "pair": pair,
                    "htf_bias": htf_bias,
                    "liquidity_levels": liq_levels,
                }
                kwargs.update(extra_args)
                setup = eval_fn(**kwargs)

                if setup is not None:
                    if setup.setup_type not in settings.HTF_ENABLED_SETUPS:
                        logger.debug(f"HTF Setup {setup_name} detected but not in HTF_ENABLED_SETUPS")
                        continue

                    logger.info(
                        f"HTF Setup {setup_name} found: pair={pair} direction={setup.direction} "
                        f"entry={setup.entry_price:.2f} sl={setup.sl_price:.2f} "
                        f"confluences={setup.confluences}"
                    )
                    return setup
        finally:
            # Restore original settings
            settings.OB_PROXIMITY_PCT = orig_proximity
            settings.OB_MAX_DISTANCE_PCT = orig_distance
            settings.MIN_RISK_DISTANCE_PCT = orig_min_risk

        return None

    def get_htf_swing_levels(self, pair: str) -> tuple[list, list]:
        """Get 4H swing highs and swing lows for trailing SL computation.

        Returns:
            (swing_highs, swing_lows) as lists of SwingPoint.
        """
        signal_tf = settings.HTF_CAMPAIGN_SIGNAL_TF
        candles = self._data.get_candles(pair, signal_tf, 100)
        if not candles or len(candles) < 10:
            return [], []
        state = self._market_structure.analyze(candles, pair, signal_tf)
        return state.swing_highs, state.swing_lows

    def mark_ob_failed(self, pair: str, sl_price: float, entry_price: float) -> None:
        """Mark an OB range as failed (trade hit SL). Prevents re-entry.

        Uses (pair, sl_price, entry_price) as the key — these uniquely
        identify the OB that was traded (SL = OB edge, entry = 50% body).
        """
        key = (pair, round(sl_price, 2), round(entry_price, 2))
        self._failed_obs.add(key)
        logger.info(f"OB marked as failed: {pair} sl={sl_price:.2f} entry={entry_price:.2f}")

    def is_ob_failed(self, pair: str, sl_price: float, entry_price: float) -> bool:
        """Check if an OB was already traded and lost."""
        key = (pair, round(sl_price, 2), round(entry_price, 2))
        return key in self._failed_obs

    def get_active_order_blocks(self, pair: str) -> list[OrderBlock]:
        """Get all active OBs for a pair across LTF + HTF timeframes."""
        obs: list[OrderBlock] = []
        for tf in settings.LTF_TIMEFRAMES + settings.HTF_TIMEFRAMES:
            obs.extend(self._order_blocks.get_active_obs(pair, tf))
        return obs

    def get_htf_bias(self, pair: str) -> str:
        """Get the cached HTF bias for a pair."""
        return self._cached_htf_bias.get(pair, "undefined")

    def _apply_atr_sl_floor(
        self, setup: TradeSetup, candles: list[Candle],
    ) -> TradeSetup:
        """Widen SL to ATR floor if structural SL is too tight.

        SL = max(structural_SL, entry ± ATR_SL_FLOOR_MULTIPLIER × ATR(14)).
        This prevents noise stop-outs in ranging markets while keeping the
        structural SL when it's already wider than the ATR floor.
        """
        from dataclasses import replace

        atr = self._compute_atr(candles, 14)
        if atr is None or atr <= 0 or setup.entry_price <= 0:
            return setup

        min_sl_distance = atr * settings.ATR_SL_FLOOR_MULTIPLIER
        current_sl_distance = abs(setup.entry_price - setup.sl_price)

        if current_sl_distance >= min_sl_distance:
            return setup  # structural SL is already wider

        # Widen SL to ATR floor
        if setup.direction == "long":
            new_sl = setup.entry_price - min_sl_distance
        else:
            new_sl = setup.entry_price + min_sl_distance

        # Recalculate TPs based on new risk distance (preserve R:R)
        new_risk = min_sl_distance
        rr1 = settings.TP1_RR_RATIO
        rr2 = settings.SETUP_TP2_RR.get(setup.setup_type, settings.TP2_RR_RATIO)
        if setup.direction == "long":
            new_tp1 = setup.entry_price + new_risk * rr1
            new_tp2 = setup.entry_price + new_risk * rr2
        else:
            new_tp1 = setup.entry_price - new_risk * rr1
            new_tp2 = setup.entry_price - new_risk * rr2

        logger.info(
            f"ATR SL floor: {setup.setup_type} {setup.pair} SL widened "
            f"{current_sl_distance/setup.entry_price*100:.2f}% → "
            f"{min_sl_distance/setup.entry_price*100:.2f}% "
            f"({settings.ATR_SL_FLOOR_MULTIPLIER}× ATR={atr:.4f})"
        )

        return replace(
            setup,
            sl_price=new_sl,
            tp1_price=new_tp1,
            tp2_price=new_tp2,
        )

    def _enrich_with_ob_depth(
        self, setup: TradeSetup, candles: list[Candle],
    ) -> TradeSetup:
        """Enrich setup with orderbook depth analysis around the OB zone.

        Fetches real-time orderbook and checks if there's institutional
        liquidity confirming the OB. Adds confluence + ML features.
        Never blocks a trade — confirmation only.
        """
        from dataclasses import replace

        if self._data is None:
            return setup

        orderbook = self._data.get_orderbook_depth(setup.pair)
        if orderbook is None:
            return setup

        direction = "bullish" if setup.direction == "long" else "bearish"

        # Reconstruct OB zone from setup geometry for search zone calculation.
        ob_body_size = abs(setup.entry_price - setup.sl_price) * 0.6
        atr = self._compute_atr(candles, 14)
        zone_base = max(ob_body_size, atr) if atr and atr > 0 else ob_body_size
        zone_radius = zone_base * settings.OB_DEPTH_ZONE_MULTIPLIER

        # Zone centered on entry price
        zone_low = setup.entry_price - zone_radius
        zone_high = setup.entry_price + zone_radius

        # Select relevant side
        if direction == "bullish":
            levels = orderbook.get("bid_levels", [])
            opp_levels = orderbook.get("ask_levels", [])
        else:
            levels = orderbook.get("ask_levels", [])
            opp_levels = orderbook.get("bid_levels", [])

        zone_levels = [(p, usd) for p, usd in levels if zone_low <= p <= zone_high]
        opp_zone = [(p, usd) for p, usd in opp_levels if zone_low <= p <= zone_high]

        total_depth = sum(usd for _, usd in zone_levels)
        opp_depth = sum(usd for _, usd in opp_zone)

        depth_ratio = total_depth / opp_depth if opp_depth > 0 else (
            2.0 if total_depth > 0 else 0.0
        )

        concentration = 0.0
        if zone_levels and total_depth > 0:
            max_level = max(usd for _, usd in zone_levels)
            concentration = max_level / total_depth

        snapshot_ts = orderbook.get("timestamp_ms", 0)
        snapshot_age_ms = int(time.time() * 1000) - snapshot_ts if snapshot_ts > 0 else 0

        # Add confluence if confirmed
        new_confluences = list(setup.confluences)
        if (depth_ratio >= settings.OB_DEPTH_RATIO_THRESHOLD
                and concentration >= settings.OB_DEPTH_CONCENTRATION_THRESHOLD):
            new_confluences.append("ob_depth_confirmed")
            logger.info(
                f"OB depth confirmed [{setup.pair}]: ratio={depth_ratio:.2f} "
                f"concentration={concentration:.2f} depth=${total_depth:.0f} "
                f"age={snapshot_age_ms}ms"
            )

        # Store depth features in confluences for ML extraction
        # (ml_features.py can parse these, or we add dedicated features)
        new_confluences.append(f"ob_depth_ratio_{depth_ratio:.2f}")
        new_confluences.append(f"ob_depth_conc_{concentration:.2f}")

        return replace(setup, confluences=new_confluences)

    def _apply_expectancy_filters(
        self, setup: TradeSetup, candles: list[Candle],
        state_4h, state_1h,
    ) -> Optional[str]:
        """Post-detection expectancy filters. Returns reject reason or None."""
        # ATR volatility filter — reject if market too quiet
        atr = self._compute_atr(candles, 14)
        if atr is not None and setup.entry_price > 0:
            atr_pct = atr / setup.entry_price
            if atr_pct < settings.MIN_ATR_PCT:
                return (f"ATR too low: {atr_pct*100:.3f}% "
                        f"< {settings.MIN_ATR_PCT*100:.3f}%")

        # Target space filter — reject if nearest opposing swing too close
        risk = abs(setup.entry_price - setup.sl_price)
        if risk <= 0:
            return None

        min_space = risk * settings.MIN_TARGET_SPACE_R
        current_price = candles[-1].close if candles else setup.entry_price
        if setup.direction == "long":
            # Ignore swing highs the current price already broke through
            highs = [s.price for s in state_4h.swing_highs + state_1h.swing_highs
                     if s.price > setup.entry_price and s.price > current_price]
            if highs:
                nearest = min(highs)
                space = nearest - setup.entry_price
                if space < min_space:
                    return (f"Target space too tight: {space:.2f} "
                            f"< {min_space:.2f} (1H/4H swing high at {nearest:.2f})")
        else:
            # Ignore swing lows the current price already broke through
            lows = [s.price for s in state_4h.swing_lows + state_1h.swing_lows
                    if s.price < setup.entry_price and s.price < current_price]
            if lows:
                nearest = max(lows)
                space = setup.entry_price - nearest
                if space < min_space:
                    return (f"Target space too tight: {space:.2f} "
                            f"< {min_space:.2f} (1H/4H swing low at {nearest:.2f})")

        return None

    @staticmethod
    def _compute_atr(candles: list[Candle], period: int = 14) -> Optional[float]:
        """Compute ATR(period) from candles. Returns None if insufficient data."""
        if len(candles) < period + 1:
            return None
        trs = []
        for i in range(-period, 0):
            c = candles[i]
            prev_c = candles[i - 1]
            tr = max(
                c.high - c.low,
                abs(c.high - prev_c.close),
                abs(c.low - prev_c.close),
            )
            trs.append(tr)
        return sum(trs) / len(trs)

    def _determine_htf_bias(self, state_4h, state_1h) -> str:
        """Determine HTF bias from 4H and 1H analysis.

        Default (HTF_BIAS_REQUIRE_4H=True): 4H must define trend, 1H fallback.
        Scalping (HTF_BIAS_REQUIRE_4H=False): 1H alone is sufficient.
        If all required timeframes are undefined, no trading.
        """
        if state_4h.trend != "undefined":
            return state_4h.trend
        if state_1h.trend != "undefined":
            if settings.HTF_BIAS_REQUIRE_4H:
                logger.debug("4H trend undefined, falling back to 1H")
            return state_1h.trend
        return "undefined"
