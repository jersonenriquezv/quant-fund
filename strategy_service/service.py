"""
StrategyService Facade — Wires all SMC detectors into a single interface.

This is the ONLY class other services import from the strategy layer.
Main entry: evaluate(pair, trigger_candle) -> TradeSetup | None

Data flow:
    trigger_candle → fetch all candles from DataService →
    HTF analysis (4H/1H) → LTF analysis (15m/5m) →
    Setup A → Setup B → Setup F → Setup G → Quick (C/D/E) → TradeSetup | None
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

        # Cached HTF bias per pair (updated on every evaluate call)
        self._cached_htf_bias: dict[str, str] = {}

        # Quick setup cooldown: (pair, setup_type) → last trigger timestamp
        self._quick_setup_last: dict[tuple[str, str], float] = {}

    def evaluate(self, pair: str,
                 trigger_candle: Candle) -> Optional[TradeSetup]:
        """Main entry point — evaluate a pair for trade setups.

        Called on every confirmed LTF candle. Runs full analysis pipeline.

        Args:
            pair: e.g. "BTC/USDT"
            trigger_candle: The confirmed candle that triggered evaluation.

        Returns:
            TradeSetup if a valid setup is found, None otherwise.
        """
        # Only evaluate on LTF candles
        if trigger_candle.timeframe not in settings.LTF_TIMEFRAMES:
            return None

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

        htf_bias = self._determine_htf_bias(state_4h, state_1h)
        self._cached_htf_bias[pair] = htf_bias
        if htf_bias == "undefined":
            logger.debug(f"No HTF bias for {pair} — skipping "
                         f"(4h_trend={state_4h.trend} 1h_trend={state_1h.trend} "
                         f"4h_breaks={len(state_4h.structure_breaks)} "
                         f"1h_breaks={len(state_1h.structure_breaks)})")
            return None

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
            # Only enabled setups are returned (settings.ENABLED_SETUPS)
            # ============================================================
            setup = self._setups.evaluate_setup_a(
                structure_state=ltf_state,
                active_obs=active_obs,
                recent_sweeps=recent_sweeps,
                pd_zone=pd_zone,
                market_snapshot=market_snapshot,
                candles=candles,
                pair=pair,
                htf_bias=htf_bias,
                liquidity_levels=liq_levels,
            )

            if setup is not None:
                if setup.setup_type not in settings.ENABLED_SETUPS:
                    logger.debug(f"Setup A detected but disabled (not in ENABLED_SETUPS)")
                else:
                    logger.info(
                        f"Setup A found: pair={pair} direction={setup.direction} "
                        f"entry={setup.entry_price:.2f} sl={setup.sl_price:.2f} "
                        f"tp1={setup.tp1_price:.2f} confluences={setup.confluences}"
                    )
                    return setup

            setup = self._setups.evaluate_setup_b(
                structure_state=ltf_state,
                active_obs=active_obs,
                active_fvgs=active_fvgs,
                pd_zone=pd_zone,
                market_snapshot=market_snapshot,
                candles=candles,
                pair=pair,
                htf_bias=htf_bias,
                liquidity_levels=liq_levels,
            )

            if setup is not None:
                if setup.setup_type not in settings.ENABLED_SETUPS:
                    logger.debug(f"Setup B detected but disabled (not in ENABLED_SETUPS)")
                else:
                    logger.info(
                        f"Setup B found: pair={pair} direction={setup.direction} "
                        f"entry={setup.entry_price:.2f} sl={setup.sl_price:.2f} "
                        f"tp1={setup.tp1_price:.2f} confluences={setup.confluences}"
                    )
                    return setup

            # Setup F — Pure OB Retest (BOS + OB, no FVG required)
            setup = self._setups.evaluate_setup_f(
                structure_state=ltf_state,
                active_obs=active_obs,
                pd_zone=pd_zone,
                market_snapshot=market_snapshot,
                candles=candles,
                pair=pair,
                htf_bias=htf_bias,
                liquidity_levels=liq_levels,
            )

            if setup is not None:
                if setup.setup_type not in settings.ENABLED_SETUPS:
                    logger.debug(f"Setup F detected but disabled (not in ENABLED_SETUPS)")
                else:
                    logger.info(
                        f"Setup F found: pair={pair} direction={setup.direction} "
                        f"entry={setup.entry_price:.2f} sl={setup.sl_price:.2f} "
                        f"tp1={setup.tp1_price:.2f} confluences={setup.confluences}"
                    )
                    return setup

            # Setup G — Breaker Block Retest
            breaker_blocks = self._order_blocks.get_breaker_blocks(pair, ltf)
            setup = self._setups.evaluate_setup_g(
                breaker_blocks=breaker_blocks,
                pd_zone=pd_zone,
                market_snapshot=market_snapshot,
                candles=candles,
                pair=pair,
                htf_bias=htf_bias,
                liquidity_levels=liq_levels,
            )

            if setup is not None:
                if setup.setup_type not in settings.ENABLED_SETUPS:
                    logger.debug(f"Setup G detected but disabled (not in ENABLED_SETUPS)")
                else:
                    logger.info(
                        f"Setup G found: pair={pair} direction={setup.direction} "
                        f"entry={setup.entry_price:.2f} sl={setup.sl_price:.2f} "
                        f"tp1={setup.tp1_price:.2f} confluences={setup.confluences}"
                    )
                    return setup

        # ============================================================
        # Step 5: Quick setups (C, D, E) — only if no swing setup found
        # ============================================================
        quick_setup = self._evaluate_quick_setups(
            pair, htf_bias, candles_5m, market_snapshot, pd_zone,
        )
        if quick_setup is not None:
            if quick_setup.setup_type not in settings.ENABLED_SETUPS:
                logger.debug(f"{quick_setup.setup_type} detected but disabled")
            else:
                return quick_setup

        return None

    def _evaluate_quick_setups(
        self,
        pair: str,
        htf_bias: str,
        candles_5m: list[Candle],
        market_snapshot,
        pd_zone,
    ) -> Optional[TradeSetup]:
        """Try quick setups C → D → E in order. Respects per-type cooldown."""
        if not candles_5m:
            return None

        current_price = candles_5m[-1].close
        now = time.time()

        # Setup C — Funding Squeeze
        if not self._is_quick_cooldown_active(pair, "setup_c", now):
            setup = self._quick_setups.evaluate_setup_c(
                pair, htf_bias, market_snapshot, current_price, candles_5m,
            )
            if setup is not None:
                self._quick_setup_last[(pair, "setup_c")] = now
                return setup

        # Setup D — LTF Structure Scalp (5m only)
        if not self._is_quick_cooldown_active(pair, "setup_d", now):
            state_5m = self._market_structure.get_state(pair, "5m")
            if state_5m is not None:
                active_obs_5m = self._order_blocks.get_active_obs(pair, "5m")
                setup = self._quick_setups.evaluate_setup_d(
                    pair, htf_bias, state_5m, active_obs_5m, pd_zone, candles_5m,
                )
                if setup is not None:
                    self._quick_setup_last[(pair, "setup_d")] = now
                    return setup

        # Setup E — Cascade Reversal
        if not self._is_quick_cooldown_active(pair, "setup_e", now):
            active_obs_5m = self._order_blocks.get_active_obs(pair, "5m")
            setup = self._quick_setups.evaluate_setup_e(
                pair, htf_bias, market_snapshot, active_obs_5m, candles_5m,
                current_price,
            )
            if setup is not None:
                self._quick_setup_last[(pair, "setup_e")] = now
                return setup

        return None

    def _is_quick_cooldown_active(
        self, pair: str, setup_type: str, now: float,
    ) -> bool:
        """Check if cooldown is active for a quick setup type on a pair."""
        last = self._quick_setup_last.get((pair, setup_type))
        if last is None:
            return False
        return (now - last) < settings.QUICK_SETUP_COOLDOWN

    def get_active_order_blocks(self, pair: str) -> list[OrderBlock]:
        """Get all active OBs for a pair across LTF timeframes."""
        obs: list[OrderBlock] = []
        for tf in settings.LTF_TIMEFRAMES:
            obs.extend(self._order_blocks.get_active_obs(pair, tf))
        return obs

    def get_htf_bias(self, pair: str) -> str:
        """Get the cached HTF bias for a pair."""
        return self._cached_htf_bias.get(pair, "undefined")

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
