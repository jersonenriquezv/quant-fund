"""
Quick Setup Evaluation — Setup C, D, E, H.

Data-driven setups with shorter duration (4h max) and lower R:R (1:1 min).
These fire only when no swing setup (A/B) is detected.

Setup C: Funding Squeeze — extreme funding + CVD alignment = momentum entry
Setup D: LTF Structure Scalp — CHoCH/BOS on 5m + fresh OB, no sweep/FVG needed
Setup E: Cascade Reversal — OI drop cascade + CVD reversal = catch the bounce
Setup H: Momentum/Impulse — volume-driven directional move + BOS = ride the wave
"""

import time
from typing import Optional

from config.settings import settings
from shared.logger import setup_logger
from shared.models import Candle, MarketSnapshot, TradeSetup
from strategy_service.market_structure import MarketStructureState
from strategy_service.order_blocks import OrderBlock
from strategy_service.liquidity import PremiumDiscountZone
from strategy_service.setups import SetupEvaluator

logger = setup_logger("strategy_quick_setups")


class QuickSetupEvaluator:
    """Evaluates quick trade setups (C, D, E) from market data signals."""

    _ob_scorer = SetupEvaluator()

    def evaluate_setup_c(
        self,
        pair: str,
        htf_bias: str,
        snapshot: Optional[MarketSnapshot],
        current_price: float,
        candles: list[Candle],
    ) -> Optional[TradeSetup]:
        """Setup C — Funding Squeeze.

        Signal: Extreme funding rate + CVD buy dominance alignment.
        Long:  funding < -threshold, buy dominance > 55%
        Short: funding > +threshold, buy dominance < 45%
        Requires HTF bias alignment.
        """
        if htf_bias not in ("bullish", "bearish"):
            return None
        if snapshot is None or snapshot.funding is None or snapshot.cvd is None:
            return None
        if current_price <= 0 or not candles:
            return None

        rate = snapshot.funding.rate
        if rate is None:
            return None

        threshold = settings.MOMENTUM_FUNDING_THRESHOLD
        cvd = snapshot.cvd
        total_vol = cvd.buy_volume + cvd.sell_volume
        if total_vol <= 0:
            return None
        buy_dominance = cvd.buy_volume / total_vol

        # Determine direction from funding signal
        direction = None
        if rate < -threshold and buy_dominance > settings.MOMENTUM_CVD_LONG_MIN:
            direction = "long"
        elif rate > threshold and buy_dominance < settings.MOMENTUM_CVD_SHORT_MAX:
            direction = "short"

        if direction is None:
            return None

        # HTF bias must align
        expected_bias = "bullish" if direction == "long" else "bearish"
        if htf_bias != expected_bias:
            logger.debug(
                f"Setup C [{pair}]: direction {direction} conflicts with "
                f"HTF bias {htf_bias}"
            )
            return None

        # Entry at current price, SL at 0.5% distance
        entry_price = current_price
        sl_distance = entry_price * settings.MOMENTUM_SL_PCT
        if direction == "long":
            sl_price = entry_price - sl_distance
        else:
            sl_price = entry_price + sl_distance

        # TPs from settings (breakeven trigger + per-setup TP2)
        risk = abs(entry_price - sl_price)
        tp2_rr = settings.SETUP_TP2_RR.get("setup_c", settings.TP2_RR_RATIO)
        if direction == "long":
            tp1 = entry_price + risk * settings.TP1_RR_RATIO
            tp2 = entry_price + risk * tp2_rr
        else:
            tp1 = entry_price - risk * settings.TP1_RR_RATIO
            tp2 = entry_price - risk * tp2_rr

        confluences = [
            f"funding_extreme_{rate*100:.4f}pct",
            f"cvd_buy_dominance_{buy_dominance*100:.1f}pct",
        ]

        logger.info(
            f"Setup C found: {pair} {direction} entry={entry_price:.2f} "
            f"funding={rate*100:.4f}% buy_dom={buy_dominance*100:.1f}%"
        )

        return TradeSetup(
            timestamp=int(time.time() * 1000),
            pair=pair,
            direction=direction,
            setup_type="setup_c",
            entry_price=entry_price,
            sl_price=sl_price,
            tp1_price=tp1,
            tp2_price=tp2,
            confluences=confluences,
            htf_bias=htf_bias,
            ob_timeframe="5m",
        )

    def evaluate_setup_d(
        self,
        pair: str,
        htf_bias: str,
        structure_state: MarketStructureState,
        active_obs: list[OrderBlock],
        pd_zone: Optional[PremiumDiscountZone],
        candles: list[Candle],
        snapshot: Optional[MarketSnapshot] = None,
    ) -> Optional[TradeSetup]:
        """Setup D — LTF Structure Scalp.

        Signal: CHoCH or BOS on 5m + fresh OB nearby.
        No sweep or FVG required. HTF bias + PD zone alignment required.
        """
        if htf_bias not in ("bullish", "bearish"):
            return None
        if not candles:
            return None

        current_price = candles[-1].close
        if current_price <= 0:
            return None

        # Only evaluate on 5m timeframe
        if structure_state.timeframe != "5m":
            return None

        # Find recent CHoCH or BOS
        if not structure_state.structure_breaks:
            return None

        latest_break = structure_state.structure_breaks[-1]
        direction = latest_break.direction  # "bullish" or "bearish"

        # Must align with HTF bias
        if direction != htf_bias:
            logger.debug(
                f"Setup D [{pair}]: break {latest_break.break_type} "
                f"{direction} != HTF {htf_bias}"
            )
            return None

        # PD zone alignment
        pd_aligned = self._check_pd_alignment(pd_zone, direction)
        if not settings.PD_AS_CONFLUENCE and not pd_aligned:
            return None

        trade_dir = "long" if direction == "bullish" else "short"

        # Minimum break displacement filter
        if settings.SETUP_D_MIN_DISPLACEMENT_PCT > 0:
            displacement = abs(latest_break.break_price - latest_break.broken_level)
            if latest_break.broken_level > 0:
                disp_pct = displacement / latest_break.broken_level
                if disp_pct < settings.SETUP_D_MIN_DISPLACEMENT_PCT:
                    logger.debug(
                        f"Setup D [{pair}]: break displacement too small "
                        f"({disp_pct*100:.3f}% < {settings.SETUP_D_MIN_DISPLACEMENT_PCT*100:.1f}%)"
                    )
                    return None

        # Find fresh OB aligned with direction — scored by composite metric
        aligned_obs = [ob for ob in active_obs if ob.direction == direction]
        if not aligned_obs:
            logger.debug(f"Setup D [{pair}]: no aligned OBs (dir={direction})")
            return None

        # Use composite OB scoring with tighter distance for quick setups
        best_ob = self._ob_scorer._find_best_ob(
            aligned_obs, current_price, direction,
            max_distance=settings.QUICK_OB_MAX_DISTANCE_PCT,
        )
        if best_ob is None:
            logger.debug(
                f"Setup D [{pair}]: no OB passes scoring "
                f"(price={current_price:.2f})"
            )
            return None

        # Entry at SETUP_D_ENTRY_PCT of OB body (shallow = close to price for explosive moves)
        pct = settings.SETUP_D_ENTRY_PCT
        if direction == "bullish":
            entry_price = best_ob.body_low + pct * (best_ob.body_high - best_ob.body_low)
        else:
            entry_price = best_ob.body_high - pct * (best_ob.body_high - best_ob.body_low)
        if direction == "bullish":
            sl_price = best_ob.low
        else:
            sl_price = best_ob.high

        risk = abs(entry_price - sl_price)
        if risk <= 0:
            return None

        # SL distance filter (too close = noise, too far = unbounded risk)
        if not self._ob_scorer._check_sl_distance(entry_price, sl_price, pair, "Setup D"):
            return None

        # Variant split: setup_d_bos or setup_d_choch for per-variant measurement
        variant = f"setup_d_{latest_break.break_type}"

        # TPs from settings (breakeven trigger + per-setup TP2)
        tp2_rr = settings.SETUP_TP2_RR.get(variant, settings.TP2_RR_RATIO)
        if direction == "bullish":
            tp1 = entry_price + risk * settings.TP1_RR_RATIO
            tp2 = entry_price + risk * tp2_rr
        else:
            tp1 = entry_price - risk * settings.TP1_RR_RATIO
            tp2 = entry_price - risk * tp2_rr

        confluences = [
            f"{latest_break.break_type}_5m",
            f"order_block_{best_ob.timeframe}",
        ]
        # PD zone: add as confluence only if aligned (confluence mode) or always (hard gate mode)
        if settings.PD_AS_CONFLUENCE:
            if pd_aligned and pd_zone and pd_zone.zone not in ("undefined",):
                confluences.append(f"pd_zone_{pd_zone.zone}")
        elif pd_zone and pd_zone.zone != "undefined":
            confluences.append(f"pd_zone_{pd_zone.zone}")

        # CVD alignment — confirms order flow supports the direction
        if snapshot and snapshot.cvd:
            cvd = snapshot.cvd
            total_vol = cvd.buy_volume + cvd.sell_volume
            if total_vol > 0:
                buy_dom = cvd.buy_volume / total_vol
                if direction == "bullish" and cvd.cvd_15m > 0:
                    confluences.append("cvd_aligned_bullish")
                    if buy_dom >= settings.BUY_DOMINANCE_STRONG_PCT:
                        confluences.append("buy_dominance_strong")
                elif direction == "bearish" and cvd.cvd_15m < 0:
                    confluences.append("cvd_aligned_bearish")
                    sell_dom = 1 - buy_dom
                    if sell_dom >= settings.BUY_DOMINANCE_STRONG_PCT:
                        confluences.append("sell_dominance_strong")

        logger.info(
            f"Setup D ({latest_break.break_type}) found: {pair} {trade_dir} "
            f"entry={entry_price:.2f} sl={sl_price:.2f}"
        )

        return TradeSetup(
            timestamp=int(time.time() * 1000),
            pair=pair,
            direction=trade_dir,
            setup_type=variant,
            entry_price=entry_price,
            sl_price=sl_price,
            tp1_price=tp1,
            tp2_price=tp2,
            confluences=confluences,
            htf_bias=htf_bias,
            ob_timeframe=best_ob.timeframe,
        )

    def evaluate_setup_e(
        self,
        pair: str,
        htf_bias: str,
        snapshot: Optional[MarketSnapshot],
        active_obs: list[OrderBlock],
        candles: list[Candle],
        current_price: float,
    ) -> Optional[TradeSetup]:
        """Setup E — Cascade Reversal.

        Signal: OI drop cascade (liquidation proxy) + CVD reversal.
        After long liquidation cascade (price dropped), look for long entry.
        After short liquidation cascade (price pumped), look for short entry.
        """
        if htf_bias not in ("bullish", "bearish"):
            return None
        if snapshot is None or not candles or current_price <= 0:
            return None

        # Need recent OI flush events
        if not snapshot.recent_oi_flushes:
            return None

        now_ms = int(time.time() * 1000)
        max_age_ms = settings.CASCADE_MAX_AGE_SECONDS * 1000

        # Filter to recent cascades only
        recent = [
            liq for liq in snapshot.recent_oi_flushes
            if (now_ms - liq.timestamp) <= max_age_ms
        ]
        if not recent:
            return None

        # Determine cascade side — majority of liquidations
        long_liq_usd = sum(l.size_usd for l in recent if l.side == "long")
        short_liq_usd = sum(l.size_usd for l in recent if l.side == "short")

        if long_liq_usd <= 0 and short_liq_usd <= 0:
            return None

        # Long liquidation cascade = price dropped = potential long entry
        # Short liquidation cascade = price pumped = potential short entry
        if long_liq_usd > short_liq_usd:
            direction = "long"
            cascade_side = "long"
        else:
            direction = "short"
            cascade_side = "short"

        # HTF bias must align
        expected_bias = "bullish" if direction == "long" else "bearish"
        if htf_bias != expected_bias:
            logger.debug(
                f"Setup E [{pair}]: cascade reversal {direction} conflicts "
                f"with HTF bias {htf_bias}"
            )
            return None

        # CVD must show reversal
        if snapshot.cvd is None:
            return None

        total_vol = snapshot.cvd.buy_volume + snapshot.cvd.sell_volume
        if total_vol <= 0:
            return None
        buy_dominance = snapshot.cvd.buy_volume / total_vol

        if direction == "long" and buy_dominance < settings.CASCADE_CVD_REVERSAL_LONG:
            logger.debug(
                f"Setup E [{pair}]: no CVD reversal for long "
                f"(buy_dom={buy_dominance:.2f} < {settings.CASCADE_CVD_REVERSAL_LONG})"
            )
            return None
        if direction == "short" and buy_dominance > (1 - settings.CASCADE_CVD_REVERSAL_SHORT):
            logger.debug(
                f"Setup E [{pair}]: no CVD reversal for short "
                f"(buy_dom={buy_dominance:.2f} > {1 - settings.CASCADE_CVD_REVERSAL_SHORT})"
            )
            return None

        # Find nearest OB as entry anchor, or use current price
        ob_direction = "bullish" if direction == "long" else "bearish"
        aligned_obs = [ob for ob in active_obs if ob.direction == ob_direction]
        best_ob = self._find_nearest_ob(aligned_obs, current_price) if aligned_obs else None

        if best_ob is not None:
            entry_price = best_ob.entry_price
            # SL beyond OB
            sl_price = best_ob.low if direction == "long" else best_ob.high
        else:
            # No OB — use current price with cascade-based SL
            entry_price = current_price
            # SL at cascade extreme (worst wick during recent candles)
            recent_candles = candles[-6:]  # Last ~30 min of 5m candles
            if direction == "long":
                sl_price = min(c.low for c in recent_candles)
            else:
                sl_price = max(c.high for c in recent_candles)

        risk = abs(entry_price - sl_price)
        if risk <= 0:
            return None

        # SL distance filter (too close = noise, too far = unbounded risk)
        if not self._ob_scorer._check_sl_distance(entry_price, sl_price, pair, "Setup E"):
            return None

        # TPs from settings (breakeven trigger + per-setup TP2)
        tp2_rr = settings.SETUP_TP2_RR.get("setup_e", settings.TP2_RR_RATIO)
        if direction == "long":
            tp1 = entry_price + risk * settings.TP1_RR_RATIO
            tp2 = entry_price + risk * tp2_rr
        else:
            tp1 = entry_price - risk * settings.TP1_RR_RATIO
            tp2 = entry_price - risk * tp2_rr

        total_liq_usd = long_liq_usd + short_liq_usd
        confluences = [
            f"cascade_oi_drop_{cascade_side}",
            f"cvd_reversal_{buy_dominance*100:.1f}pct",
        ]
        if best_ob is not None:
            confluences.append(f"order_block_{best_ob.timeframe}")
        if total_liq_usd > 0:
            confluences.append(f"oi_flush_usd_{total_liq_usd:.0f}")

        logger.info(
            f"Setup E found: {pair} {direction} cascade={cascade_side} "
            f"liq_usd={total_liq_usd:.0f} entry={entry_price:.2f}"
        )

        return TradeSetup(
            timestamp=int(time.time() * 1000),
            pair=pair,
            direction=direction,
            setup_type="setup_e",
            entry_price=entry_price,
            sl_price=sl_price,
            tp1_price=tp1,
            tp2_price=tp2,
            confluences=confluences,
            htf_bias=htf_bias,
            ob_timeframe=best_ob.timeframe if best_ob else "5m",
        )

    def evaluate_setup_h(
        self,
        pair: str,
        htf_bias: str,
        structure_state: MarketStructureState,
        candles: list[Candle],
        snapshot: Optional[MarketSnapshot] = None,
    ) -> Optional[TradeSetup]:
        """Setup H — Momentum/Impulse Entry.

        Signal: volume-driven impulse move in progress. Enter at current price
        (momentum is happening NOW), SL at structural level (initiating OB or
        impulse extreme), ride with progressive trailing.

        Runs on both 5m and 15m candles.
        """
        if htf_bias not in ("bullish", "bearish"):
            return None

        n = settings.SETUP_H_MIN_IMPULSE_CANDLES
        if not candles or len(candles) < n + 20:
            return None

        current_price = candles[-1].close
        if current_price <= 0:
            return None

        # 1. Analyze last N candles for directional impulse
        impulse_candles = candles[-n:]
        bullish_count = sum(1 for c in impulse_candles if c.close >= c.open)
        bearish_count = n - bullish_count

        if bullish_count >= n * settings.SETUP_H_MIN_DIRECTIONAL_PCT:
            direction = "bullish"
        elif bearish_count >= n * settings.SETUP_H_MIN_DIRECTIONAL_PCT:
            direction = "bearish"
        else:
            return None

        # 2. HTF bias must align
        if direction != htf_bias:
            logger.debug(
                f"Setup H [{pair}]: impulse {direction} != HTF {htf_bias}"
            )
            return None

        # 3. Check minimum impulse move size
        impulse_start = impulse_candles[0].open
        impulse_end = impulse_candles[-1].close
        move_pct = abs(impulse_end - impulse_start) / impulse_start if impulse_start > 0 else 0
        if move_pct < settings.SETUP_H_MIN_IMPULSE_PCT:
            logger.debug(
                f"Setup H [{pair}]: impulse too small "
                f"({move_pct*100:.3f}% < {settings.SETUP_H_MIN_IMPULSE_PCT*100:.1f}%)"
            )
            return None

        # 4. Volume spike check — impulse vs prior 20 candles
        prior_candles = candles[-(n + 20):-n]
        avg_impulse_vol = sum(c.volume for c in impulse_candles) / n
        avg_prior_vol = sum(c.volume for c in prior_candles) / len(prior_candles) if prior_candles else 0
        if avg_prior_vol <= 0:
            return None
        vol_ratio = avg_impulse_vol / avg_prior_vol
        if vol_ratio < settings.SETUP_H_VOLUME_SPIKE_RATIO:
            logger.debug(
                f"Setup H [{pair}]: volume ratio too low "
                f"({vol_ratio:.2f}x < {settings.SETUP_H_VOLUME_SPIKE_RATIO}x)"
            )
            return None

        # Compute directional purity for ML features
        directional_pct = max(bullish_count, bearish_count) / n if n > 0 else 0

        # 4b. Exhaustion filter: deceleration check
        # Compare body size of last 2 candles vs first 3 in impulse window
        first_bodies = [abs(c.close - c.open) for c in impulse_candles[:3]]
        last_bodies = [abs(c.close - c.open) for c in impulse_candles[-2:]]
        avg_first_body = sum(first_bodies) / len(first_bodies) if first_bodies else 0
        avg_last_body = sum(last_bodies) / len(last_bodies) if last_bodies else 0
        decel_ratio = avg_last_body / avg_first_body if avg_first_body > 0 else 1.0
        if avg_first_body > 0:
            if decel_ratio < settings.SETUP_H_DECEL_RATIO:
                logger.debug(
                    f"Setup H [{pair}]: impulse decelerating "
                    f"(body ratio {decel_ratio:.2f} < {settings.SETUP_H_DECEL_RATIO})"
                )
                return None

        # 4c. Exhaustion filter: extended move check
        # Reject if total impulse move already exceeds threshold
        if move_pct > settings.SETUP_H_MAX_EXTENDED_PCT:
            logger.debug(
                f"Setup H [{pair}]: impulse already extended "
                f"({move_pct*100:.2f}% > {settings.SETUP_H_MAX_EXTENDED_PCT*100:.1f}%)"
            )
            return None

        # 4d. Exhaustion filter: volume decay check
        # Compare volume of last 2 impulse candles vs first 3
        first_vols = [c.volume for c in impulse_candles[:3]]
        last_vols = [c.volume for c in impulse_candles[-2:]]
        avg_first_vol = sum(first_vols) / len(first_vols) if first_vols else 0
        avg_last_vol = sum(last_vols) / len(last_vols) if last_vols else 0
        vol_decay_ratio = avg_last_vol / avg_first_vol if avg_first_vol > 0 else 1.0
        if avg_first_vol > 0:
            if vol_decay_ratio < settings.SETUP_H_VOL_DECAY_RATIO:
                logger.debug(
                    f"Setup H [{pair}]: volume fading "
                    f"(vol ratio {vol_decay_ratio:.2f} < {settings.SETUP_H_VOL_DECAY_RATIO})"
                )
                return None

        # 5. BOS must exist in impulse direction
        if not structure_state.structure_breaks:
            return None
        has_bos = any(
            b.direction == direction
            for b in structure_state.structure_breaks
        )
        if not has_bos:
            logger.debug(f"Setup H [{pair}]: no BOS in {direction} direction")
            return None

        trade_dir = "long" if direction == "bullish" else "short"

        # 6. Find initiating OB — last opposite-color candle before impulse
        #    with volume >= 1x average (the candle that started the move)
        sl_price = None
        ob_found = False
        pre_impulse = candles[:-(n)]
        for c in reversed(pre_impulse[-10:]):  # Look back up to 10 candles before impulse
            is_opposite = (direction == "bullish" and c.close < c.open) or \
                          (direction == "bearish" and c.close >= c.open)
            if is_opposite and c.volume >= avg_prior_vol:
                if direction == "bullish":
                    sl_price = c.low
                else:
                    sl_price = c.high
                ob_found = True
                break

        # Fallback: use impulse extreme
        if sl_price is None:
            if direction == "bullish":
                sl_price = min(c.low for c in impulse_candles)
            else:
                sl_price = max(c.high for c in impulse_candles)

        # 7. Cap SL distance
        entry_price = current_price
        sl_distance_pct = abs(entry_price - sl_price) / entry_price if entry_price > 0 else 0
        if sl_distance_pct > settings.SETUP_H_MAX_SL_PCT:
            if direction == "bullish":
                sl_price = entry_price * (1 - settings.SETUP_H_MAX_SL_PCT)
            else:
                sl_price = entry_price * (1 + settings.SETUP_H_MAX_SL_PCT)

        risk = abs(entry_price - sl_price)
        if risk <= 0:
            return None

        # SL distance filter (too close = noise, too far = unbounded risk)
        if not self._ob_scorer._check_sl_distance(entry_price, sl_price, pair, "Setup H"):
            return None

        # 8. TPs from settings (breakeven trigger + per-setup TP2)
        tp2_rr = settings.SETUP_TP2_RR.get("setup_h", settings.TP2_RR_RATIO)
        if direction == "bullish":
            tp1 = entry_price + risk * settings.TP1_RR_RATIO
            tp2 = entry_price + risk * tp2_rr
        else:
            tp1 = entry_price - risk * settings.TP1_RR_RATIO
            tp2 = entry_price - risk * tp2_rr

        # 9. Build confluences
        confluences = [
            f"impulse_move_{move_pct*100:.2f}pct",
            f"volume_spike_{vol_ratio:.1f}x",
            "bos_confirmed",
        ]
        if ob_found:
            confluences.append("initiating_ob")
        # Exhaustion metrics for ML feature extraction
        confluences.append(f"decel_ratio_{decel_ratio:.2f}")
        confluences.append(f"vol_decay_{vol_decay_ratio:.2f}")
        confluences.append(f"directional_purity_{directional_pct:.2f}")

        # CVD confirmation — order flow must support impulse direction
        if snapshot and snapshot.cvd:
            cvd = snapshot.cvd
            total_vol = cvd.buy_volume + cvd.sell_volume
            if total_vol > 0:
                buy_dom = cvd.buy_volume / total_vol
                if direction == "bullish" and cvd.cvd_15m > 0:
                    confluences.append("cvd_momentum_confirmed")
                    if buy_dom >= settings.BUY_DOMINANCE_STRONG_PCT:
                        confluences.append("buy_dominance_strong")
                elif direction == "bearish" and cvd.cvd_15m < 0:
                    confluences.append("cvd_momentum_confirmed")
                    sell_dom = 1 - buy_dom
                    if sell_dom >= settings.BUY_DOMINANCE_STRONG_PCT:
                        confluences.append("sell_dominance_strong")

        logger.info(
            f"Setup H found: {pair} {trade_dir} entry={entry_price:.2f} "
            f"sl={sl_price:.2f} move={move_pct*100:.2f}% vol={vol_ratio:.1f}x"
        )

        return TradeSetup(
            timestamp=int(time.time() * 1000),
            pair=pair,
            direction=trade_dir,
            setup_type="setup_h",
            entry_price=entry_price,
            sl_price=sl_price,
            tp1_price=tp1,
            tp2_price=tp2,
            confluences=confluences,
            htf_bias=htf_bias,
            ob_timeframe=candles[-1].timeframe,
        )

    # ================================================================
    # Helpers
    # ================================================================

    def _find_nearest_ob(
        self,
        obs: list[OrderBlock],
        current_price: float,
    ) -> Optional[OrderBlock]:
        """Find the nearest OB to current price within proximity threshold."""
        if not obs or current_price <= 0:
            return None

        margin = current_price * settings.OB_PROXIMITY_PCT
        candidates = []
        for ob in obs:
            extended_low = ob.body_low - margin
            extended_high = ob.body_high + margin
            if extended_low <= current_price <= extended_high:
                dist = abs(current_price - ob.entry_price)
                candidates.append((ob, dist))

        if not candidates:
            return None

        # Return closest, break ties by most recent
        candidates.sort(key=lambda x: (x[1], -x[0].timestamp))
        return candidates[0][0]

    def _check_pd_alignment(
        self,
        pd_zone: Optional[PremiumDiscountZone],
        direction: str,
    ) -> bool:
        """Check premium/discount zone alignment."""
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
