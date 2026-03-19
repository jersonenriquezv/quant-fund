"""
Setup Evaluation — Swing Setups A, B, F, G.

Setup A: Liquidity Sweep + CHoCH + Order Block (primary, most profitable)
Setup B: BOS + FVG + Order Block (secondary, trend continuation)
Setup F: BOS + Order Block (no FVG required — pure OB retest)
Setup G: Breaker Block retest (mitigated OB with flipped direction)

All require minimum 2 confirmations (non-negotiable).
All require correct Premium/Discount alignment.
Evaluation order: A → B → F → G (first match wins).
"""

import time
from typing import Optional

from config.settings import settings
from shared.logger import setup_logger
from shared.models import Candle, MarketSnapshot, TradeSetup
from strategy_service.market_structure import MarketStructureState, StructureBreak
from strategy_service.order_blocks import OrderBlock
from strategy_service.fvg import FairValueGap
from strategy_service.liquidity import (
    LiquiditySweep, LiquidityLevel, PremiumDiscountZone,
)

logger = setup_logger("strategy_setups")


class SetupEvaluator:
    """Evaluates potential trade setups from detected SMC patterns."""

    def __init__(self):
        # OI delta tracking: pair → previous OI USD value
        self._prev_oi: dict[str, float] = {}

    def evaluate_setup_a(
        self,
        structure_state: MarketStructureState,
        active_obs: list[OrderBlock],
        recent_sweeps: list[LiquiditySweep],
        pd_zone: Optional[PremiumDiscountZone],
        market_snapshot: Optional[MarketSnapshot],
        candles: list[Candle],
        pair: str,
        htf_bias: str,
        liquidity_levels: list[LiquidityLevel],
    ) -> Optional[TradeSetup]:
        """Evaluate Setup A — Liquidity Sweep + CHoCH + Order Block.

        Conditions (from CLAUDE.md):
        1. HTF bias defined (bullish or bearish)
        2. Liquidity sweep occurred
        3. CHoCH confirms direction change
        4. Temporal ordering: sweep BEFORE CHoCH
        5. Fresh OB forms (< 48h)
        6. OB in correct PD zone (long=discount, short=premium)
        7. Retrace to OB — price near entry
        8. Volume spike >= 2x + liquidations visible
        9. Minimum 2 confluences

        Returns:
            TradeSetup if all conditions met, None otherwise.
        """
        if htf_bias not in ("bullish", "bearish"):
            return None

        if not recent_sweeps:
            logger.debug(f"Setup A [{pair}]: no recent sweeps")
            return None

        # Find CHoCH in structure breaks
        choch_breaks = [
            b for b in structure_state.structure_breaks
            if b.break_type == "choch"
        ]
        if not choch_breaks:
            logger.debug(f"Setup A [{pair}]: no CHoCH (sweeps={len(recent_sweeps)})")
            return None

        latest_choch = choch_breaks[-1]
        direction = latest_choch.direction  # "bullish" or "bearish"

        # Setup A mode: "continuation", "reversal", or "both".
        # continuation: CHoCH must align with HTF bias (LTF confirms HTF).
        # reversal: CHoCH must oppose HTF bias (counter-trend entry).
        # both: no alignment check (default).
        mode = settings.SETUP_A_MODE
        if mode == "continuation" and direction != htf_bias:
            logger.debug(f"Setup A [{pair}]: continuation mode — CHoCH {direction} != HTF {htf_bias}")
            return None
        elif mode == "reversal" and direction == htf_bias:
            logger.debug(f"Setup A [{pair}]: reversal mode — CHoCH {direction} == HTF {htf_bias}")
            return None
        # Legacy setting — still respected if SETUP_A_MODE is "both"
        elif mode == "both" and settings.REQUIRE_HTF_LTF_ALIGNMENT and direction != htf_bias:
            logger.debug(f"Setup A [{pair}]: CHoCH {direction} != HTF {htf_bias}")
            return None

        # Find the most recent sweep aligned with the direction
        # that occurred BEFORE the CHoCH (temporal ordering)
        aligned_sweep = None
        max_gap = settings.SETUP_A_MAX_SWEEP_CHOCH_GAP
        for sweep in reversed(recent_sweeps):
            if sweep.direction != direction:
                continue
            # Sweep must happen before CHoCH
            if sweep.timestamp >= latest_choch.timestamp:
                continue
            # Proximity check: sweep should be within max_gap candles of CHoCH
            candle_gap = abs(latest_choch.candle_index - self._find_candle_index_by_ts(
                candles, sweep.timestamp
            ))
            if candle_gap <= max_gap:
                aligned_sweep = sweep
                break

        if aligned_sweep is None:
            logger.debug(f"Setup A [{pair}]: no aligned sweep before CHoCH "
                         f"(sweeps={len(recent_sweeps)} dir={direction})")
            return None

        # PD zone alignment
        pd_aligned = self._check_pd_alignment(pd_zone, direction)

        if not settings.PD_AS_CONFLUENCE:
            # Hard gate mode (default) — deferred if high-confluence override enabled
            pd_override_threshold = settings.PD_OVERRIDE_MIN_CONFLUENCES
            if not pd_aligned and pd_override_threshold <= 0:
                zone = pd_zone.zone if pd_zone else "none"
                logger.debug(f"Setup A [{pair}]: PD misaligned (zone={zone} dir={direction})")
                return None

        # Find a fresh OB aligned with direction
        aligned_obs = [
            ob for ob in active_obs
            if ob.direction == direction
        ]
        if not aligned_obs:
            logger.debug(f"Setup A [{pair}]: no aligned OBs (total_obs={len(active_obs)} dir={direction})")
            return None

        # Find the best OB within max distance (zone-based — no proximity requirement)
        current_price = candles[-1].close if candles else 0
        best_ob = self._find_best_ob(aligned_obs, current_price, direction)
        if best_ob is None:
            logger.debug(f"Setup A [{pair}]: no OBs within range "
                         f"(price={current_price:.2f} obs={len(aligned_obs)})")
            return None

        # Volume confirmation
        volume_confirmed, vol_confluences = self._check_volume_confirmation(
            best_ob, aligned_sweep, market_snapshot, candles
        )

        # Build confluences list
        confluences = []
        confluences.append(f"liquidity_sweep_{aligned_sweep.direction}")
        confluences.append(f"choch_{latest_choch.direction}")
        confluences.append(f"order_block_{best_ob.timeframe}")

        # PD zone: add as confluence only if aligned (or always add zone label in hard-gate mode)
        if settings.PD_AS_CONFLUENCE:
            if pd_aligned and pd_zone and pd_zone.zone not in ("undefined",):
                confluences.append(f"pd_zone_{pd_zone.zone}")
        else:
            if pd_zone:
                confluences.append(f"pd_zone_{pd_zone.zone}")

        confluences.extend(vol_confluences)

        # Minimum confluence check — non-negotiable: 2+
        if not self._check_confluence_minimum(confluences):
            logger.debug(f"Setup A [{pair}]: insufficient confluences "
                         f"({len(confluences)}<2 confluences={confluences})")
            return None

        # Deferred PD check with confluence override (hard gate mode only)
        if not settings.PD_AS_CONFLUENCE and not pd_aligned:
            pd_override_threshold = settings.PD_OVERRIDE_MIN_CONFLUENCES
            if len(confluences) < pd_override_threshold:
                zone = pd_zone.zone if pd_zone else "none"
                logger.debug(f"Setup A [{pair}]: PD misaligned (zone={zone} dir={direction})")
                return None
            zone = pd_zone.zone if pd_zone else "none"
            logger.info(f"Setup A [{pair}]: PD override — {len(confluences)} confluences "
                        f"(zone={zone} dir={direction})")

        # Calculate entry at configurable depth into OB body
        sl_price = self._calculate_sl(best_ob, direction)
        body_range = best_ob.body_high - best_ob.body_low
        if body_range <= 0:
            return None
        if direction == "bullish":
            entry_price = best_ob.body_low + body_range * settings.SETUP_A_ENTRY_PCT
        else:
            entry_price = best_ob.body_high - body_range * settings.SETUP_A_ENTRY_PCT

        # Validate SL is on correct side of entry
        if not self._validate_sl_direction(entry_price, sl_price, direction):
            logger.debug(f"Setup A [{pair}]: SL inverted — entry={entry_price:.2f} "
                         f"sl={sl_price:.2f} dir={direction}")
            return None

        # Early SL-too-close filter (avoid pipeline overhead for junk setups)
        risk_pct = abs(entry_price - sl_price) / entry_price if entry_price > 0 else 0
        if risk_pct < settings.MIN_RISK_DISTANCE_PCT:
            logger.debug(f"Setup A [{pair}]: SL too close "
                         f"({risk_pct*100:.2f}% < {settings.MIN_RISK_DISTANCE_PCT*100:.1f}%)")
            return None

        tp1, tp2 = self._calculate_tp_levels(
            entry_price, sl_price, direction, liquidity_levels
        )

        # Validate R:R to tp2 meets minimum
        risk = abs(entry_price - sl_price)
        if risk <= 0:
            return None
        rr = self._compute_rr(entry_price, sl_price, tp2)
        if rr < settings.MIN_RISK_REWARD:
            logger.debug(f"Setup A [{pair}]: R:R too low "
                         f"({rr:.2f} < {settings.MIN_RISK_REWARD})")
            return None

        entry2_price = self._compute_entry2(best_ob, direction)
        return TradeSetup(
            timestamp=int(time.time() * 1000),
            pair=pair,
            direction="long" if direction == "bullish" else "short",
            setup_type="setup_a",
            entry_price=entry_price,
            sl_price=sl_price,
            tp1_price=tp1,
            tp2_price=tp2,
            confluences=confluences,
            htf_bias=htf_bias,
            ob_timeframe=best_ob.timeframe,
            entry2_price=entry2_price,
        )

    def evaluate_setup_b(
        self,
        structure_state: MarketStructureState,
        active_obs: list[OrderBlock],
        active_fvgs: list[FairValueGap],
        pd_zone: Optional[PremiumDiscountZone],
        market_snapshot: Optional[MarketSnapshot],
        candles: list[Candle],
        pair: str,
        htf_bias: str,
        liquidity_levels: list[LiquidityLevel],
    ) -> Optional[TradeSetup]:
        """Evaluate Setup B — BOS + FVG + Order Block.

        Conditions (from CLAUDE.md):
        1. HTF bias defined
        2. BOS on LTF with 0.1%+ close
        3. Fresh OB
        4. FVG inside or adjacent to OB
        5. Premium/Discount aligned
        6. Entry at 50% FVG or OB
        7. Volume >= 1.5x + CVD aligned
        8. Minimum 2 confluences

        Returns:
            TradeSetup if all conditions met, None otherwise.
        """
        if htf_bias not in ("bullish", "bearish"):
            return None

        # Find BOS in structure breaks
        bos_breaks = [
            b for b in structure_state.structure_breaks
            if b.break_type == "bos"
        ]
        if not bos_breaks:
            logger.debug(f"Setup B [{pair}]: no BOS")
            return None

        latest_bos = bos_breaks[-1]
        direction = latest_bos.direction

        # BOS recency filter — reject stale BOS (mirrors Setup F)
        if candles:
            candles_since_bos = len(candles) - 1 - latest_bos.candle_index
            if candles_since_bos > settings.SETUP_B_MAX_BOS_AGE_CANDLES:
                logger.debug(f"Setup B [{pair}]: BOS too old ({candles_since_bos} candles "
                             f"> {settings.SETUP_B_MAX_BOS_AGE_CANDLES})")
                return None

        # Direction must align with HTF bias (unless profile disables it)
        if settings.REQUIRE_HTF_LTF_ALIGNMENT and direction != htf_bias:
            logger.debug(f"Setup B [{pair}]: BOS {direction} != HTF {htf_bias}")
            return None

        # PD zone alignment
        pd_aligned = self._check_pd_alignment(pd_zone, direction)

        if not settings.PD_AS_CONFLUENCE:
            pd_override_threshold = settings.PD_OVERRIDE_MIN_CONFLUENCES
            if not pd_aligned and pd_override_threshold <= 0:
                zone = pd_zone.zone if pd_zone else "none"
                logger.debug(f"Setup B [{pair}]: PD misaligned (zone={zone} dir={direction})")
                return None

        # Find fresh OB aligned with direction
        aligned_obs = [
            ob for ob in active_obs
            if ob.direction == direction
        ]
        if not aligned_obs:
            logger.debug(f"Setup B [{pair}]: no aligned OBs (total={len(active_obs)} dir={direction})")
            return None

        # Find FVG aligned with direction
        aligned_fvgs = [
            fvg for fvg in active_fvgs
            if fvg.direction == direction
        ]
        if not aligned_fvgs:
            logger.debug(f"Setup B [{pair}]: no aligned FVGs (total={len(active_fvgs)} dir={direction})")
            return None

        # Find OB+FVG pair where FVG is inside or adjacent to OB
        current_price = candles[-1].close if candles else 0
        best_ob = None
        best_fvg = None

        # Collect all adjacent pairs, then pick best by volume ratio + recency
        candidates = []
        for ob in aligned_obs:
            if not self._is_ob_within_range(current_price, ob):
                continue
            for fvg in aligned_fvgs:
                if self._is_fvg_adjacent_to_ob(fvg, ob):
                    candidates.append((ob, fvg))

        if candidates:
            best_ob, best_fvg = max(
                candidates, key=lambda pair: (pair[0].volume_ratio, pair[0].timestamp)
            )

        if best_ob is None or best_fvg is None:
            logger.debug(f"Setup B [{pair}]: no adjacent OB+FVG pair within range "
                         f"(obs={len(aligned_obs)} fvgs={len(aligned_fvgs)})")
            return None

        # Volume + CVD + OI + funding confirmation (reuse shared method)
        _, vol_confluences = self._check_volume_confirmation(
            best_ob, None, market_snapshot, candles
        )

        # Build confluences
        confluences = []
        confluences.append(f"bos_{latest_bos.direction}")
        confluences.append(f"order_block_{best_ob.timeframe}")
        confluences.append(f"fvg_{best_fvg.timeframe}")

        if settings.PD_AS_CONFLUENCE:
            if pd_aligned and pd_zone and pd_zone.zone not in ("undefined",):
                confluences.append(f"pd_zone_{pd_zone.zone}")
        else:
            if pd_zone:
                confluences.append(f"pd_zone_{pd_zone.zone}")

        confluences.extend(vol_confluences)

        # Minimum confluence check
        if not self._check_confluence_minimum(confluences):
            return None

        # Deferred PD check with confluence override (hard gate mode only)
        if not settings.PD_AS_CONFLUENCE and not pd_aligned:
            pd_override_threshold = settings.PD_OVERRIDE_MIN_CONFLUENCES
            if len(confluences) < pd_override_threshold:
                zone = pd_zone.zone if pd_zone else "none"
                logger.debug(f"Setup B [{pair}]: PD misaligned (zone={zone} dir={direction})")
                return None
            zone = pd_zone.zone if pd_zone else "none"
            logger.info(f"Setup B [{pair}]: PD override — {len(confluences)} confluences "
                        f"(zone={zone} dir={direction})")

        # Calculate SL/TP
        # Setup B entry at FVG_ENTRY_PCT of the gap (0.75 = shallower, easier fill).
        # SL stays at OB wick for wider distance.
        sl_price = self._calculate_sl(best_ob, direction)
        fvg_range = best_fvg.high - best_fvg.low
        if direction == "bullish":
            entry_price = best_fvg.low + fvg_range * settings.FVG_ENTRY_PCT
        else:
            entry_price = best_fvg.high - fvg_range * settings.FVG_ENTRY_PCT

        # Entry distance filter — reject zombie entries too far from current price
        if current_price > 0:
            entry_dist = abs(entry_price - current_price) / current_price
            if entry_dist > settings.SETUP_B_MAX_ENTRY_DISTANCE_PCT:
                logger.debug(f"Setup B [{pair}]: entry too far from price "
                             f"({entry_dist:.4f} > {settings.SETUP_B_MAX_ENTRY_DISTANCE_PCT})")
                return None

        # Validate SL is on correct side of entry
        if not self._validate_sl_direction(entry_price, sl_price, direction):
            logger.debug(f"Setup B [{pair}]: SL inverted — entry={entry_price:.2f} "
                         f"sl={sl_price:.2f} dir={direction}")
            return None

        tp1, tp2 = self._calculate_tp_levels(
            entry_price, sl_price, direction, liquidity_levels
        )

        # Validate R:R to tp2 meets minimum
        risk = abs(entry_price - sl_price)
        if risk <= 0:
            return None
        rr = self._compute_rr(entry_price, sl_price, tp2)
        if rr < settings.MIN_RISK_REWARD:
            return None

        entry2_price = self._compute_entry2(best_ob, direction)
        return TradeSetup(
            timestamp=int(time.time() * 1000),
            pair=pair,
            direction="long" if direction == "bullish" else "short",
            setup_type="setup_b",
            entry_price=entry_price,
            sl_price=sl_price,
            tp1_price=tp1,
            tp2_price=tp2,
            confluences=confluences,
            htf_bias=htf_bias,
            ob_timeframe=best_ob.timeframe,
            entry2_price=entry2_price,
        )

    def evaluate_setup_f(
        self,
        structure_state: MarketStructureState,
        active_obs: list[OrderBlock],
        pd_zone: Optional[PremiumDiscountZone],
        market_snapshot: Optional[MarketSnapshot],
        candles: list[Candle],
        pair: str,
        htf_bias: str,
        liquidity_levels: list[LiquidityLevel],
    ) -> Optional[TradeSetup]:
        """Evaluate Setup F — Pure OB Retest (BOS + OB, no FVG required).

        Like Setup B but without FVG adjacency requirement. Fires when there's
        a BOS confirming direction + a fresh OB to enter on, but no FVG nearby.

        Conditions:
        1. HTF bias defined
        2. Fresh BOS on LTF (within SETUP_F_MAX_BOS_AGE_CANDLES)
        3. BOS displacement above SETUP_F_MIN_BOS_DISPLACEMENT_PCT
        4. Fresh OB aligned with direction, temporally near BOS
        5. OB composite score >= SETUP_F_MIN_OB_SCORE
        6. PD zone aligned
        7. Entry within SETUP_F_MAX_ENTRY_DISTANCE_PCT of current price
        8. Minimum SETUP_F_MIN_CONFLUENCES (3) structural confluences
        9. R:R >= MIN_RISK_REWARD
        """
        if htf_bias not in ("bullish", "bearish"):
            logger.debug(f"Setup F [{pair}]: HTF bias undefined")
            return None

        bos_breaks = [
            b for b in structure_state.structure_breaks
            if b.break_type == "bos"
        ]
        if not bos_breaks:
            logger.debug(f"Setup F [{pair}]: no BOS")
            return None

        latest_bos = bos_breaks[-1]
        direction = latest_bos.direction

        # BOS recency filter — reject stale BOS
        if candles:
            candles_since_bos = len(candles) - 1 - latest_bos.candle_index
            if candles_since_bos > settings.SETUP_F_MAX_BOS_AGE_CANDLES:
                logger.debug(f"Setup F [{pair}]: BOS too old ({candles_since_bos} candles "
                             f"> {settings.SETUP_F_MAX_BOS_AGE_CANDLES})")
                return None

        # BOS displacement filter — reject micro-breaks
        if latest_bos.broken_level > 0:
            displacement = abs(latest_bos.break_price - latest_bos.broken_level) / latest_bos.broken_level
            if displacement < settings.SETUP_F_MIN_BOS_DISPLACEMENT_PCT:
                logger.debug(f"Setup F [{pair}]: BOS displacement too small "
                             f"({displacement:.4f} < {settings.SETUP_F_MIN_BOS_DISPLACEMENT_PCT})")
                return None

        if settings.REQUIRE_HTF_LTF_ALIGNMENT and direction != htf_bias:
            logger.debug(f"Setup F [{pair}]: BOS {direction} != HTF {htf_bias}")
            return None

        # PD zone alignment
        pd_aligned = self._check_pd_alignment(pd_zone, direction)

        if not settings.PD_AS_CONFLUENCE:
            pd_override_threshold = settings.PD_OVERRIDE_MIN_CONFLUENCES
            if not pd_aligned and pd_override_threshold <= 0:
                zone = pd_zone.zone if pd_zone else "none"
                logger.debug(f"Setup F [{pair}]: PD misaligned (zone={zone} dir={direction})")
                return None

        # Filter OBs: aligned direction + temporally near the BOS
        candle_duration_ms = (
            (candles[1].timestamp - candles[0].timestamp)
            if len(candles) >= 2 else 900_000
        )
        max_gap_ms = settings.SETUP_F_MAX_OB_BOS_GAP_CANDLES * candle_duration_ms

        aligned_obs = [
            ob for ob in active_obs
            if ob.direction == direction
            and abs(ob.timestamp - latest_bos.timestamp) <= max_gap_ms
        ]
        if not aligned_obs:
            all_aligned = [ob for ob in active_obs if ob.direction == direction]
            logger.debug(f"Setup F [{pair}]: no OBs near BOS (aligned={len(all_aligned)} "
                         f"within_gap=0 max_gap={settings.SETUP_F_MAX_OB_BOS_GAP_CANDLES})")
            return None

        current_price = candles[-1].close if candles else 0
        best_ob, best_score = self._find_best_ob_with_score(aligned_obs, current_price, direction)
        if best_ob is None:
            logger.debug(f"Setup F [{pair}]: no OBs within range (aligned={len(aligned_obs)})")
            return None

        # OB composite score minimum
        if best_score < settings.SETUP_F_MIN_OB_SCORE:
            logger.debug(f"Setup F [{pair}]: OB score too low ({best_score:.3f} "
                         f"< {settings.SETUP_F_MIN_OB_SCORE})")
            return None

        # Volume + CVD + OI + funding confirmation (reuse shared method)
        _, vol_confluences = self._check_volume_confirmation(
            best_ob, None, market_snapshot, candles
        )

        confluences = []
        confluences.append(f"bos_{latest_bos.direction}")
        confluences.append(f"order_block_{best_ob.timeframe}")
        if settings.PD_AS_CONFLUENCE:
            if pd_aligned and pd_zone and pd_zone.zone not in ("undefined",):
                confluences.append(f"pd_zone_{pd_zone.zone}")
        else:
            if pd_zone:
                confluences.append(f"pd_zone_{pd_zone.zone}")
        confluences.extend(vol_confluences)

        # Setup F-specific minimum confluences (higher than generic 2)
        if len(confluences) < settings.SETUP_F_MIN_CONFLUENCES:
            logger.debug(f"Setup F [{pair}]: insufficient confluences "
                         f"({len(confluences)}<{settings.SETUP_F_MIN_CONFLUENCES}: {confluences})")
            return None

        # Deferred PD check with confluence override (hard gate mode only)
        if not settings.PD_AS_CONFLUENCE and not pd_aligned:
            pd_override_threshold = settings.PD_OVERRIDE_MIN_CONFLUENCES
            if len(confluences) < pd_override_threshold:
                zone = pd_zone.zone if pd_zone else "none"
                logger.debug(f"Setup F [{pair}]: PD misaligned (zone={zone} dir={direction})")
                return None
            zone = pd_zone.zone if pd_zone else "none"
            logger.info(f"Setup F [{pair}]: PD override — {len(confluences)} confluences "
                        f"(zone={zone} dir={direction})")

        sl_price = self._calculate_sl(best_ob, direction)
        entry_price = best_ob.entry_price

        # Entry distance filter — reject zombie setups pointing at distant OBs
        if current_price > 0:
            entry_dist = abs(entry_price - current_price) / current_price
            if entry_dist > settings.SETUP_F_MAX_ENTRY_DISTANCE_PCT:
                logger.debug(f"Setup F [{pair}]: entry too far from price "
                             f"({entry_dist:.4f} > {settings.SETUP_F_MAX_ENTRY_DISTANCE_PCT})")
                return None

        # Validate SL is on correct side of entry
        if not self._validate_sl_direction(entry_price, sl_price, direction):
            logger.debug(f"Setup F [{pair}]: SL inverted — entry={entry_price:.2f} "
                         f"sl={sl_price:.2f} dir={direction}")
            return None

        tp1, tp2 = self._calculate_tp_levels(
            entry_price, sl_price, direction, liquidity_levels
        )

        risk = abs(entry_price - sl_price)
        if risk <= 0:
            return None
        rr = self._compute_rr(entry_price, sl_price, tp2)
        if rr < settings.MIN_RISK_REWARD:
            logger.debug(f"Setup F [{pair}]: R:R too low ({rr:.2f} < {settings.MIN_RISK_REWARD})")
            return None

        entry2_price = self._compute_entry2(best_ob, direction)
        return TradeSetup(
            timestamp=int(time.time() * 1000),
            pair=pair,
            direction="long" if direction == "bullish" else "short",
            setup_type="setup_f",
            entry_price=entry_price,
            sl_price=sl_price,
            tp1_price=tp1,
            tp2_price=tp2,
            confluences=confluences,
            htf_bias=htf_bias,
            ob_timeframe=best_ob.timeframe,
            entry2_price=entry2_price,
        )

    def evaluate_setup_g(
        self,
        breaker_blocks: list[OrderBlock],
        pd_zone: Optional[PremiumDiscountZone],
        market_snapshot: Optional[MarketSnapshot],
        candles: list[Candle],
        pair: str,
        htf_bias: str,
        liquidity_levels: list[LiquidityLevel],
    ) -> Optional[TradeSetup]:
        """Evaluate Setup G — Breaker Block Retest.

        A mitigated OB becomes inverse support/resistance:
        - Mitigated bullish OB → bearish breaker (resistance) → short entry
        - Mitigated bearish OB → bullish breaker (support) → long entry

        Entry when price retests the breaker block zone.

        Conditions:
        1. HTF bias defined and aligned with breaker direction
        2. Breaker block within range of current price
        3. PD zone aligned
        4. Minimum 2 confluences
        5. Blended R:R >= MIN_RISK_REWARD
        """
        if htf_bias not in ("bullish", "bearish"):
            logger.debug(f"Setup G [{pair}]: HTF bias undefined")
            return None

        if not breaker_blocks:
            logger.debug(f"Setup G [{pair}]: no breaker blocks")
            return None

        aligned_breakers = [
            bb for bb in breaker_blocks
            if bb.direction == htf_bias
        ]
        if not aligned_breakers:
            logger.debug(f"Setup G [{pair}]: no aligned breakers (total={len(breaker_blocks)} bias={htf_bias})")
            return None

        # PD zone alignment
        pd_aligned = self._check_pd_alignment(pd_zone, htf_bias)

        if not settings.PD_AS_CONFLUENCE:
            pd_override_threshold = settings.PD_OVERRIDE_MIN_CONFLUENCES
            if not pd_aligned and pd_override_threshold <= 0:
                zone = pd_zone.zone if pd_zone else "none"
                logger.debug(f"Setup G [{pair}]: PD misaligned (zone={zone} dir={htf_bias})")
                return None

        current_price = candles[-1].close if candles else 0
        best_bb = self._find_best_ob(aligned_breakers, current_price, htf_bias)
        if best_bb is None:
            logger.debug(f"Setup G [{pair}]: no breakers within range (aligned={len(aligned_breakers)})")
            return None

        direction = htf_bias

        # Volume + CVD + OI + funding confirmation (reuse shared method)
        _, vol_confluences = self._check_volume_confirmation(
            best_bb, None, market_snapshot, candles
        )

        confluences = []
        confluences.append(f"breaker_block_{best_bb.timeframe}")
        if settings.PD_AS_CONFLUENCE:
            if pd_aligned and pd_zone and pd_zone.zone not in ("undefined",):
                confluences.append(f"pd_zone_{pd_zone.zone}")
        else:
            if pd_zone:
                confluences.append(f"pd_zone_{pd_zone.zone}")
        confluences.extend(vol_confluences)

        if not self._check_confluence_minimum(confluences):
            logger.debug(f"Setup G [{pair}]: insufficient confluences ({len(confluences)}<2: {confluences})")
            return None

        # Deferred PD check with confluence override (hard gate mode only)
        if not settings.PD_AS_CONFLUENCE and not pd_aligned:
            pd_override_threshold = settings.PD_OVERRIDE_MIN_CONFLUENCES
            if len(confluences) < pd_override_threshold:
                zone = pd_zone.zone if pd_zone else "none"
                logger.debug(f"Setup G [{pair}]: PD misaligned (zone={zone} dir={direction})")
                return None
            zone = pd_zone.zone if pd_zone else "none"
            logger.info(f"Setup G [{pair}]: PD override — {len(confluences)} confluences "
                        f"(zone={zone} dir={direction})")

        sl_price = self._calculate_sl(best_bb, direction)
        entry_price = best_bb.entry_price

        # Validate SL is on correct side of entry
        if not self._validate_sl_direction(entry_price, sl_price, direction):
            logger.debug(f"Setup G [{pair}]: SL inverted — entry={entry_price:.2f} "
                         f"sl={sl_price:.2f} dir={direction}")
            return None

        tp1, tp2 = self._calculate_tp_levels(
            entry_price, sl_price, direction, liquidity_levels
        )

        risk = abs(entry_price - sl_price)
        if risk <= 0:
            return None
        rr = self._compute_rr(entry_price, sl_price, tp2)
        if rr < settings.MIN_RISK_REWARD:
            logger.debug(f"Setup G [{pair}]: R:R too low ({rr:.2f} < {settings.MIN_RISK_REWARD})")
            return None

        return TradeSetup(
            timestamp=int(time.time() * 1000),
            pair=pair,
            direction="long" if direction == "bullish" else "short",
            setup_type="setup_g",
            entry_price=entry_price,
            sl_price=sl_price,
            tp1_price=tp1,
            tp2_price=tp2,
            confluences=confluences,
            htf_bias=htf_bias,
            ob_timeframe=best_bb.timeframe,
        )

    def _calculate_tp_levels(
        self,
        entry: float,
        sl: float,
        direction: str,
        liquidity_levels: list[LiquidityLevel],
    ) -> tuple[float, float]:
        """Calculate TP1 and TP2 from entry/SL.

        TP1: settings.TP1_RR_RATIO (1:1 R:R) — breakeven trigger
        TP2: settings.TP2_RR_RATIO (2:1 R:R) — single TP, 100% close
        """
        risk = abs(entry - sl)

        if direction == "bullish":
            tp1 = entry + (risk * settings.TP1_RR_RATIO)
            tp2 = entry + (risk * settings.TP2_RR_RATIO)
        else:
            tp1 = entry - (risk * settings.TP1_RR_RATIO)
            tp2 = entry - (risk * settings.TP2_RR_RATIO)

        return tp1, tp2

    def _check_volume_confirmation(
        self,
        ob: OrderBlock,
        sweep: Optional[LiquiditySweep],
        market_snapshot: Optional[MarketSnapshot],
        candles: Optional[list[Candle]] = None,
    ) -> tuple[bool, list[str]]:
        """Check volume/institutional confirmation for setups.

        Returns (is_confirmed, list_of_confluence_strings).
        """
        confluences = []

        # OB volume ratio
        if ob.volume_ratio >= settings.OB_MIN_VOLUME_RATIO:
            confluences.append(f"ob_volume_{ob.volume_ratio:.1f}x")

        # Sweep volume ratio — graduated tiers
        if sweep and sweep.volume_ratio >= settings.SWEEP_MIN_VOLUME_RATIO:
            confluences.append(f"sweep_volume_{sweep.volume_ratio:.1f}x")
            if sweep.volume_ratio >= settings.SWEEP_EXTREME_VOLUME_RATIO:
                confluences.append("sweep_strong")
                confluences.append("sweep_extreme")
            elif sweep.volume_ratio >= settings.SWEEP_STRONG_VOLUME_RATIO:
                confluences.append("sweep_strong")

        # OI flush event
        if sweep and sweep.had_oi_flush:
            confluences.append("oi_flush")

        if market_snapshot and market_snapshot.recent_oi_flushes:
            total_liq = sum(
                l.size_usd for l in market_snapshot.recent_oi_flushes
            )
            if total_liq > 0:
                confluences.append(f"oi_flush_usd_{total_liq:.0f}")

        # CVD divergence check (replaces simple cvd_15m > 0 boolean)
        if market_snapshot and market_snapshot.cvd and candles and len(candles) >= 4:
            cvd = market_snapshot.cvd
            direction = ob.direction

            # Compute recent price change (last 3 candles ≈ 15m on 5m TF)
            price_now = candles[-1].close
            price_prev = candles[-4].close
            price_change = (price_now - price_prev) / price_prev if price_prev > 0 else 0

            # Multi-timeframe CVD agreement (5m, 15m, 1h)
            cvd_mtf_agree = (
                (cvd.cvd_5m > 0 and cvd.cvd_15m > 0 and cvd.cvd_1h > 0)
                if direction == "bullish" else
                (cvd.cvd_5m < 0 and cvd.cvd_15m < 0 and cvd.cvd_1h < 0)
            )

            # Divergence: price moving against direction but CVD supporting it
            # This is the strongest CVD signal (absorption / accumulation)
            cvd_bullish = cvd.cvd_15m > 0
            cvd_bearish = cvd.cvd_15m < 0
            if direction == "bullish" and cvd_bullish and price_change < -0.001:
                confluences.append("cvd_divergence_bullish")
            elif direction == "bearish" and cvd_bearish and price_change > 0.001:
                confluences.append("cvd_divergence_bearish")
            elif cvd_mtf_agree:
                confluences.append(f"cvd_mtf_aligned_{direction}")
            elif direction == "bullish" and cvd_bullish:
                confluences.append("cvd_aligned_bullish")
            elif direction == "bearish" and cvd_bearish:
                confluences.append("cvd_aligned_bearish")

            # Buy dominance magnitude — graduated tiers
            total_vol = cvd.buy_volume + cvd.sell_volume
            if total_vol > 0:
                buy_dom = cvd.buy_volume / total_vol
                if direction == "bullish":
                    if buy_dom >= settings.BUY_DOMINANCE_STRONG_PCT:
                        confluences.append("buy_dominance_strong")
                    elif buy_dom >= settings.BUY_DOMINANCE_MODERATE_PCT:
                        confluences.append("buy_dominance_moderate")
                else:  # bearish — check sell dominance
                    sell_dom = 1 - buy_dom
                    if sell_dom >= settings.BUY_DOMINANCE_STRONG_PCT:
                        confluences.append("sell_dominance_strong")
                    elif sell_dom >= settings.BUY_DOMINANCE_MODERATE_PCT:
                        confluences.append("sell_dominance_moderate")

        # OI direction + price direction (institutional positioning signal)
        if market_snapshot and market_snapshot.oi:
            oi = market_snapshot.oi
            pair = market_snapshot.pair
            direction = ob.direction

            # Track OI delta between evaluations — graduated tiers
            prev_oi = self._prev_oi.get(pair)
            if prev_oi is not None and prev_oi > 0:
                oi_delta_pct = (oi.oi_usd - prev_oi) / prev_oi

                # Always record raw delta for ML feature extraction
                confluences.append(f"oi_delta_{oi_delta_pct*100:.2f}pct")

                # OI rising = new positions opening (institutional activity)
                if oi_delta_pct >= settings.OI_DELTA_STRONG_PCT:
                    confluences.append("oi_rising_strong")
                    confluences.append("oi_rising_moderate")
                elif oi_delta_pct >= settings.OI_DELTA_MODERATE_PCT:
                    confluences.append("oi_rising_moderate")
                elif oi_delta_pct >= settings.OI_DELTA_MILD_PCT:
                    confluences.append("oi_rising_mild")
                # OI dropping significantly = liquidation pressure
                elif oi_delta_pct < -settings.OI_DELTA_MODERATE_PCT:
                    confluences.append(f"oi_dropping_{abs(oi_delta_pct)*100:.1f}pct")

            self._prev_oi[pair] = oi.oi_usd

        # Funding rate — graduated symmetric tiers
        # Crowded side is vulnerable to forced exits. Higher crowding = stronger signal.
        if market_snapshot and market_snapshot.funding:
            rate = market_snapshot.funding.rate
            abs_rate = abs(rate)
            # Long opportunity: negative funding = shorts crowded
            if ob.direction == "bullish" and rate < 0:
                if abs_rate >= settings.FUNDING_EXTREME_THRESHOLD:
                    confluences.append("funding_extreme_long")
                    confluences.append("funding_moderate_long")
                elif abs_rate >= settings.FUNDING_MODERATE_THRESHOLD:
                    confluences.append("funding_moderate_long")
                elif abs_rate >= settings.FUNDING_MILD_THRESHOLD:
                    confluences.append("funding_mild_long")
            # Short opportunity: positive funding = longs crowded
            elif ob.direction == "bearish" and rate > 0:
                if abs_rate >= settings.FUNDING_EXTREME_THRESHOLD:
                    confluences.append("funding_extreme_short")
                    confluences.append("funding_moderate_short")
                elif abs_rate >= settings.FUNDING_MODERATE_THRESHOLD:
                    confluences.append("funding_moderate_short")
                elif abs_rate >= settings.FUNDING_MILD_THRESHOLD:
                    confluences.append("funding_mild_short")

        confirmed = len(confluences) >= 1
        return confirmed, confluences

    def _check_confluence_minimum(self, confluences: list[str]) -> bool:
        """Minimum 2 confirmations required. Non-negotiable per CLAUDE.md."""
        return len(confluences) >= 2

    def _check_pd_alignment(
        self,
        pd_zone: Optional[PremiumDiscountZone],
        direction: str,
    ) -> bool:
        """Check if direction aligns with PD zone.

        Long only in discount, short only in premium.
        If no PD zone data, allow the trade (don't block on missing data).
        Profiles can disable this check entirely via REQUIRE_PD_ALIGNMENT.
        """
        if not settings.REQUIRE_PD_ALIGNMENT:
            return True

        if pd_zone is None or pd_zone.zone == "undefined":
            return True

        if direction == "bullish" and pd_zone.zone == "premium":
            return False
        if direction == "bearish" and pd_zone.zone == "discount":
            return False
        if pd_zone.zone == "equilibrium" and not settings.ALLOW_EQUILIBRIUM_TRADES:
            return False

        return True

    @staticmethod
    def _validate_sl_direction(entry: float, sl: float, direction: str) -> bool:
        """Validate SL is on the correct side of entry.

        Bearish/short: SL must be ABOVE entry (price going up = loss).
        Bullish/long: SL must be BELOW entry (price going down = loss).
        """
        if direction == "bullish":
            return sl < entry
        else:
            return sl > entry

    def _is_price_near_ob(self, current_price: float,
                          ob: OrderBlock) -> bool:
        """Check if current price is near the OB entry zone.

        Price must be within the OB body range extended by OB_PROXIMITY_PCT
        of the current price. This prevents triggering setups when price
        is still far from the ideal 50% body entry.
        """
        if current_price <= 0:
            return False

        margin = current_price * settings.OB_PROXIMITY_PCT
        extended_low = ob.body_low - margin
        extended_high = ob.body_high + margin

        return extended_low <= current_price <= extended_high

    def _is_fvg_adjacent_to_ob(self, fvg: FairValueGap,
                               ob: OrderBlock) -> bool:
        """Check if FVG is inside or adjacent to OB.

        Adjacent = overlapping zones or within FVG_OB_MAX_GAP_PCT of each other.
        """
        # Check for overlap
        overlap = max(0, min(fvg.high, ob.high) - max(fvg.low, ob.low))
        if overlap > 0:
            return True

        # Check adjacency (gap between zones <= 0.5% of price)
        gap = min(abs(fvg.low - ob.high), abs(ob.low - fvg.high))
        mid_price = (ob.high + ob.low) / 2
        if mid_price > 0 and (gap / mid_price) <= settings.FVG_OB_MAX_GAP_PCT:
            return True

        return False

    def _is_ob_within_range(self, current_price: float,
                            ob: OrderBlock) -> bool:
        """Check if OB is within OB_MAX_DISTANCE_PCT of current price.

        Prevents placing limit orders at absurdly distant OBs.
        """
        if current_price <= 0:
            return False
        distance = abs(current_price - ob.entry_price) / current_price
        return distance <= settings.OB_MAX_DISTANCE_PCT

    def _score_ob(
        self,
        ob: OrderBlock,
        current_price: float,
        max_distance: float | None = None,
    ) -> float:
        """Score an OB by volume, freshness, proximity, and body size.

        Returns -1 if OB should be filtered out (too small, too far).
        Otherwise returns a 0-1 composite score.
        """
        if current_price <= 0 or ob.body_low <= 0:
            return -1

        # Body size filter — reject micro-OBs that produce tiny SLs
        body_pct = (ob.body_high - ob.body_low) / ob.body_low
        if body_pct < settings.OB_MIN_BODY_PCT:
            return -1

        # Distance filter — reject OBs beyond max range
        max_dist_pct = max_distance if max_distance is not None else settings.OB_MAX_DISTANCE_PCT
        dist = abs(current_price - ob.entry_price) / current_price
        if dist > max_dist_pct:
            return -1

        # Volume score (0-1): normalize to 0-5x range
        vol_score = min(ob.volume_ratio / 5.0, 1.0)

        # Freshness score (0-1): newer = better
        now_ms = int(time.time() * 1000)
        max_age_ms = settings.OB_MAX_AGE_HOURS * 3600 * 1000
        age_ms = now_ms - ob.timestamp
        fresh_score = max(0.0, 1.0 - (age_ms / max_age_ms)) if max_age_ms > 0 else 0.0

        # Proximity score (0-1): closer to price = more likely to fill
        max_dist = settings.OB_MAX_DISTANCE_PCT
        prox_score = max(0.0, 1.0 - (dist / max_dist)) if max_dist > 0 else 0.0

        # Body size score (0-1): bigger body = more meaningful
        size_score = min((body_pct - settings.OB_MIN_BODY_PCT) / 0.02, 1.0)
        size_score = max(0.0, size_score)

        return (
            vol_score * settings.OB_SCORE_VOLUME_W
            + fresh_score * settings.OB_SCORE_FRESHNESS_W
            + prox_score * settings.OB_SCORE_PROXIMITY_W
            + size_score * settings.OB_SCORE_SIZE_W
        )

    def _find_best_ob(
        self,
        obs: list[OrderBlock],
        current_price: float,
        direction: str,
        max_distance: float | None = None,
    ) -> Optional[OrderBlock]:
        """Find the best OB using composite scoring (volume, freshness, proximity, size)."""
        best_ob, _ = self._find_best_ob_with_score(obs, current_price, direction, max_distance)
        return best_ob

    def _find_best_ob_with_score(
        self,
        obs: list[OrderBlock],
        current_price: float,
        direction: str,
        max_distance: float | None = None,
    ) -> tuple[Optional[OrderBlock], float]:
        """Find the best OB and return (ob, score). Score is -1 if no valid OB."""
        best_ob = None
        best_score = -1.0

        for ob in obs:
            score = self._score_ob(ob, current_price, max_distance)
            if score > best_score:
                best_score = score
                best_ob = ob

        return best_ob, best_score

    def _compute_rr(
        self,
        entry: float,
        sl: float,
        tp2: float,
    ) -> float:
        """Compute R:R to tp2 (single TP, 100% close)."""
        risk = abs(entry - sl)
        if risk <= 0:
            return 0.0
        return abs(tp2 - entry) / risk

    def _find_candle_index_by_ts(
        self,
        candles: list[Candle],
        timestamp: int,
    ) -> int:
        """Find the candle index closest to a given timestamp.

        Returns the index of the candle with the closest timestamp.
        Used for temporal proximity checks between events.
        """
        if not candles:
            return 0

        best_idx = 0
        best_diff = abs(candles[0].timestamp - timestamp)

        for i, c in enumerate(candles):
            diff = abs(c.timestamp - timestamp)
            if diff < best_diff:
                best_diff = diff
                best_idx = i

        return best_idx

    def _compute_entry2(self, ob: OrderBlock, direction: str) -> float:
        """Compute deeper split-entry price at OB 75% body level."""
        body_range = ob.body_high - ob.body_low
        if body_range <= 0:
            return ob.entry_price
        if direction == "bullish":
            return ob.body_low + body_range * 0.25
        else:
            return ob.body_high - body_range * 0.25

    def _calculate_sl(self, ob: OrderBlock, direction: str) -> float:
        """Calculate stop loss — below/above entire OB (wick-to-wick)."""
        if direction == "bullish":
            return ob.low   # SL below OB low
        else:
            return ob.high  # SL above OB high
