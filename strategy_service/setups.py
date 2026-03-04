"""
Setup Evaluation — Setup A (Primary) and Setup B (Secondary).

Setup A: Liquidity Sweep + CHoCH + Order Block (primary, most profitable)
Setup B: BOS + FVG + Order Block (secondary, trend continuation)

Both require minimum 2 confirmations (non-negotiable).
Both require correct Premium/Discount alignment.
Setup A is evaluated first. If both trigger, A wins.
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

        # Setup A is CONTINUATION: CHoCH must align with HTF bias.
        # This means we only trade when LTF structure confirms HTF direction.
        # Reversal setups (CHoCH opposing HTF) are intentionally excluded
        # because they have lower win rates without additional confirmation.
        if direction != htf_bias:
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
        if not self._check_pd_alignment(pd_zone, direction):
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

        # Find the best OB (most recent, closest to current price)
        current_price = candles[-1].close if candles else 0
        best_ob = self._find_best_ob(aligned_obs, current_price, direction)
        if best_ob is None:
            logger.debug(f"Setup A [{pair}]: OBs exist but price not near any "
                         f"(price={current_price:.2f} obs={len(aligned_obs)})")
            return None

        # Check if price is near OB entry
        if not self._is_price_near_ob(current_price, best_ob):
            logger.debug(f"Setup A [{pair}]: price not near best OB "
                         f"(price={current_price:.2f} ob={best_ob.body_low:.2f}-{best_ob.body_high:.2f})")
            return None

        # Volume confirmation
        volume_confirmed, vol_confluences = self._check_volume_confirmation(
            best_ob, aligned_sweep, market_snapshot
        )

        # Build confluences list
        confluences = []
        confluences.append(f"liquidity_sweep_{aligned_sweep.direction}")
        confluences.append(f"choch_{latest_choch.direction}")
        confluences.append(f"order_block_{best_ob.timeframe}")

        if pd_zone:
            confluences.append(f"pd_zone_{pd_zone.zone}")

        confluences.extend(vol_confluences)

        # Minimum confluence check — non-negotiable: 2+
        if not self._check_confluence_minimum(confluences):
            return None

        # Calculate TP levels
        sl_price = self._calculate_sl(best_ob, direction)
        entry_price = best_ob.entry_price
        tp1, tp2, tp3 = self._calculate_tp_levels(
            entry_price, sl_price, direction, liquidity_levels
        )

        # Validate blended R:R meets minimum
        # Weighted average: 50% at TP1 + 30% at TP2 + 20% at TP3
        risk = abs(entry_price - sl_price)
        if risk <= 0:
            return None
        blended_rr = self._compute_blended_rr(entry_price, sl_price, tp1, tp2, tp3)
        if blended_rr < settings.MIN_RISK_REWARD:
            return None

        return TradeSetup(
            timestamp=int(time.time() * 1000),
            pair=pair,
            direction="long" if direction == "bullish" else "short",
            setup_type="setup_a",
            entry_price=entry_price,
            sl_price=sl_price,
            tp1_price=tp1,
            tp2_price=tp2,
            tp3_price=tp3,
            confluences=confluences,
            htf_bias=htf_bias,
            ob_timeframe=best_ob.timeframe,
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

        # Direction must align with HTF bias
        if direction != htf_bias:
            logger.debug(f"Setup B [{pair}]: BOS {direction} != HTF {htf_bias}")
            return None

        # PD zone alignment
        if not self._check_pd_alignment(pd_zone, direction):
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

        for ob in aligned_obs:
            for fvg in aligned_fvgs:
                if self._is_fvg_adjacent_to_ob(fvg, ob):
                    if best_ob is None or self._is_price_near_ob(current_price, ob):
                        best_ob = ob
                        best_fvg = fvg

        if best_ob is None or best_fvg is None:
            logger.debug(f"Setup B [{pair}]: no adjacent OB+FVG pair "
                         f"(obs={len(aligned_obs)} fvgs={len(aligned_fvgs)})")
            return None

        # Check if price is near entry zone
        if not self._is_price_near_ob(current_price, best_ob):
            logger.debug(f"Setup B [{pair}]: price not near OB "
                         f"(price={current_price:.2f} ob={best_ob.body_low:.2f}-{best_ob.body_high:.2f})")
            return None

        # Volume + CVD confirmation
        vol_confluences = []
        if best_ob.volume_ratio >= settings.OB_MIN_VOLUME_RATIO:
            vol_confluences.append(f"ob_volume_{best_ob.volume_ratio:.1f}x")

        # Check CVD alignment
        if market_snapshot and market_snapshot.cvd:
            cvd = market_snapshot.cvd
            if direction == "bullish" and cvd.cvd_15m > 0:
                vol_confluences.append("cvd_aligned_bullish")
            elif direction == "bearish" and cvd.cvd_15m < 0:
                vol_confluences.append("cvd_aligned_bearish")

        # Check OI trend
        if market_snapshot and market_snapshot.oi:
            vol_confluences.append("oi_data_available")

        # Check funding extremes
        if market_snapshot and market_snapshot.funding:
            rate = market_snapshot.funding.rate
            if direction == "bullish" and rate < -0.0001:
                vol_confluences.append("funding_negative_long_opportunity")
            elif direction == "bearish" and rate > 0.0003:
                vol_confluences.append("funding_extreme_positive")

        # Build confluences
        confluences = []
        confluences.append(f"bos_{latest_bos.direction}")
        confluences.append(f"order_block_{best_ob.timeframe}")
        confluences.append(f"fvg_{best_fvg.timeframe}")

        if pd_zone:
            confluences.append(f"pd_zone_{pd_zone.zone}")

        confluences.extend(vol_confluences)

        # Minimum confluence check
        if not self._check_confluence_minimum(confluences):
            return None

        # Calculate SL/TP
        sl_price = self._calculate_sl(best_ob, direction)
        entry_price = best_ob.entry_price
        tp1, tp2, tp3 = self._calculate_tp_levels(
            entry_price, sl_price, direction, liquidity_levels
        )

        # Validate blended R:R meets minimum
        risk = abs(entry_price - sl_price)
        if risk <= 0:
            return None
        blended_rr = self._compute_blended_rr(entry_price, sl_price, tp1, tp2, tp3)
        if blended_rr < settings.MIN_RISK_REWARD:
            return None

        return TradeSetup(
            timestamp=int(time.time() * 1000),
            pair=pair,
            direction="long" if direction == "bullish" else "short",
            setup_type="setup_b",
            entry_price=entry_price,
            sl_price=sl_price,
            tp1_price=tp1,
            tp2_price=tp2,
            tp3_price=tp3,
            confluences=confluences,
            htf_bias=htf_bias,
            ob_timeframe=best_ob.timeframe,
        )

    def _calculate_tp_levels(
        self,
        entry: float,
        sl: float,
        direction: str,
        liquidity_levels: list[LiquidityLevel],
    ) -> tuple[float, float, float]:
        """Calculate TP1, TP2, TP3 from entry/SL.

        TP1: settings.TP1_RR_RATIO (1:1 R:R)
        TP2: settings.TP2_RR_RATIO (1:2 R:R)
        TP3: next liquidity level or 1:3 R:R fallback
        """
        risk = abs(entry - sl)

        if direction == "bullish":
            tp1 = entry + (risk * settings.TP1_RR_RATIO)
            tp2 = entry + (risk * settings.TP2_RR_RATIO)

            # TP3: next BSL level above entry, or 1:3 fallback
            tp3_fallback = entry + (risk * 3.0)
            tp3 = tp3_fallback

            for level in sorted(liquidity_levels, key=lambda l: l.price):
                if level.level_type == "bsl" and level.price > tp2:
                    tp3 = level.price
                    break

        else:
            tp1 = entry - (risk * settings.TP1_RR_RATIO)
            tp2 = entry - (risk * settings.TP2_RR_RATIO)

            # TP3: next SSL level below entry, or 1:3 fallback
            tp3_fallback = entry - (risk * 3.0)
            tp3 = tp3_fallback

            for level in sorted(liquidity_levels, key=lambda l: l.price, reverse=True):
                if level.level_type == "ssl" and level.price < tp2:
                    tp3 = level.price
                    break

        return tp1, tp2, tp3

    def _check_volume_confirmation(
        self,
        ob: OrderBlock,
        sweep: Optional[LiquiditySweep],
        market_snapshot: Optional[MarketSnapshot],
    ) -> tuple[bool, list[str]]:
        """Check volume/institutional confirmation for Setup A.

        Returns (is_confirmed, list_of_confluence_strings).
        """
        confluences = []

        # OB volume ratio
        if ob.volume_ratio >= settings.OB_MIN_VOLUME_RATIO:
            confluences.append(f"ob_volume_{ob.volume_ratio:.1f}x")

        # Sweep volume ratio
        if sweep and sweep.volume_ratio >= settings.SWEEP_MIN_VOLUME_RATIO:
            confluences.append(f"sweep_volume_{sweep.volume_ratio:.1f}x")

        # Liquidation cascade
        if sweep and sweep.had_liquidations:
            confluences.append("liquidation_cascade")

        if market_snapshot and market_snapshot.recent_liquidations:
            total_liq = sum(
                l.size_usd for l in market_snapshot.recent_liquidations
            )
            if total_liq > 0:
                confluences.append(f"liquidations_usd_{total_liq:.0f}")

        # CVD alignment
        if market_snapshot and market_snapshot.cvd:
            cvd = market_snapshot.cvd
            direction = ob.direction
            if direction == "bullish" and cvd.cvd_15m > 0:
                confluences.append("cvd_aligned_bullish")
            elif direction == "bearish" and cvd.cvd_15m < 0:
                confluences.append("cvd_aligned_bearish")

        # Funding extremes
        if market_snapshot and market_snapshot.funding:
            rate = market_snapshot.funding.rate
            if ob.direction == "bullish" and rate < -0.0001:
                confluences.append("funding_negative_long_opportunity")
            elif ob.direction == "bearish" and rate > 0.0003:
                confluences.append("funding_extreme_positive")

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
        """
        if pd_zone is None or pd_zone.zone == "undefined":
            return True

        if direction == "bullish" and pd_zone.zone == "premium":
            return False
        if direction == "bearish" and pd_zone.zone == "discount":
            return False
        if pd_zone.zone == "equilibrium":
            return False

        return True

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

        Adjacent = overlapping zones or within 0.1% of each other.
        """
        # Check for overlap
        overlap = max(0, min(fvg.high, ob.high) - max(fvg.low, ob.low))
        if overlap > 0:
            return True

        # Check adjacency (gap between zones <= 0.1% of price)
        gap = min(abs(fvg.low - ob.high), abs(ob.low - fvg.high))
        mid_price = (ob.high + ob.low) / 2
        if mid_price > 0 and (gap / mid_price) <= 0.001:
            return True

        return False

    def _find_best_ob(
        self,
        obs: list[OrderBlock],
        current_price: float,
        direction: str,
    ) -> Optional[OrderBlock]:
        """Find the best OB — most recent one that price is near."""
        candidates = [
            ob for ob in obs
            if self._is_price_near_ob(current_price, ob)
        ]

        if not candidates:
            return None

        # Return most recent
        return max(candidates, key=lambda ob: ob.timestamp)

    def _compute_blended_rr(
        self,
        entry: float,
        sl: float,
        tp1: float,
        tp2: float,
        tp3: float,
    ) -> float:
        """Compute weighted-average R:R across partial closes.

        Weights: TP1=50%, TP2=30%, TP3=20% (from settings).
        This gives a realistic expected R:R instead of checking a single level.
        """
        risk = abs(entry - sl)
        if risk <= 0:
            return 0.0

        rr1 = abs(tp1 - entry) / risk
        rr2 = abs(tp2 - entry) / risk
        rr3 = abs(tp3 - entry) / risk

        return (
            settings.TP1_CLOSE_PCT * rr1
            + settings.TP2_CLOSE_PCT * rr2
            + settings.TP3_CLOSE_PCT * rr3
        )

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

    def _calculate_sl(self, ob: OrderBlock, direction: str) -> float:
        """Calculate stop loss — below/above entire OB (wick-to-wick)."""
        if direction == "bullish":
            return ob.low   # SL below OB low
        else:
            return ob.high  # SL above OB high
