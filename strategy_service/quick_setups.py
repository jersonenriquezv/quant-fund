"""
Quick Setup Evaluation — Setup C, D, E.

Data-driven setups with shorter duration (4h max) and lower R:R (1:1 min).
These fire only when no swing setup (A/B) is detected.

Setup C: Funding Squeeze — extreme funding + CVD alignment = momentum entry
Setup D: LTF Structure Scalp — CHoCH/BOS on 5m + fresh OB, no sweep/FVG needed
Setup E: Cascade Reversal — OI drop cascade + CVD reversal = catch the bounce
"""

import time
from typing import Optional

from config.settings import settings
from shared.logger import setup_logger
from shared.models import Candle, MarketSnapshot, TradeSetup
from strategy_service.market_structure import MarketStructureState
from strategy_service.order_blocks import OrderBlock
from strategy_service.liquidity import PremiumDiscountZone

logger = setup_logger("strategy_quick_setups")


class QuickSetupEvaluator:
    """Evaluates quick trade setups (C, D, E) from market data signals."""

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

        # TPs at 1:1 (breakeven trigger), 2:1 (single TP)
        risk = abs(entry_price - sl_price)
        if direction == "long":
            tp1 = entry_price + risk * 1.0
            tp2 = entry_price + risk * 2.0
        else:
            tp1 = entry_price - risk * 1.0
            tp2 = entry_price - risk * 2.0

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
        if not self._check_pd_alignment(pd_zone, direction):
            return None

        trade_dir = "long" if direction == "bullish" else "short"

        # Find fresh OB aligned with direction, near current price
        aligned_obs = [ob for ob in active_obs if ob.direction == direction]
        if not aligned_obs:
            logger.debug(f"Setup D [{pair}]: no aligned OBs (dir={direction})")
            return None

        # Find nearest OB to current price
        best_ob = self._find_nearest_ob(aligned_obs, current_price)
        if best_ob is None:
            logger.debug(
                f"Setup D [{pair}]: no OB near price "
                f"(price={current_price:.2f})"
            )
            return None

        # Entry at 50% of OB body, SL beyond OB
        entry_price = best_ob.entry_price
        if direction == "bullish":
            sl_price = best_ob.low
        else:
            sl_price = best_ob.high

        risk = abs(entry_price - sl_price)
        if risk <= 0:
            return None

        # TPs at 1:1 (breakeven trigger), 2:1 (single TP)
        if direction == "bullish":
            tp1 = entry_price + risk * 1.0
            tp2 = entry_price + risk * 2.0
        else:
            tp1 = entry_price - risk * 1.0
            tp2 = entry_price - risk * 2.0

        confluences = [
            f"{latest_break.break_type}_5m",
            f"order_block_{best_ob.timeframe}",
        ]
        if pd_zone and pd_zone.zone != "undefined":
            confluences.append(f"pd_zone_{pd_zone.zone}")

        logger.info(
            f"Setup D found: {pair} {trade_dir} "
            f"{latest_break.break_type} + OB "
            f"entry={entry_price:.2f} sl={sl_price:.2f}"
        )

        return TradeSetup(
            timestamp=int(time.time() * 1000),
            pair=pair,
            direction=trade_dir,
            setup_type="setup_d",
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

        # Need recent liquidation cascades
        if not snapshot.recent_liquidations:
            return None

        now_ms = int(time.time() * 1000)
        max_age_ms = settings.CASCADE_MAX_AGE_SECONDS * 1000

        # Filter to recent cascades only
        recent = [
            liq for liq in snapshot.recent_liquidations
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

        # TPs at 1:1 (breakeven trigger), 2:1 (single TP)
        if direction == "long":
            tp1 = entry_price + risk * 1.0
            tp2 = entry_price + risk * 2.0
        else:
            tp1 = entry_price - risk * 1.0
            tp2 = entry_price - risk * 2.0

        total_liq_usd = long_liq_usd + short_liq_usd
        confluences = [
            f"cascade_oi_drop_{cascade_side}",
            f"cvd_reversal_{buy_dominance*100:.1f}pct",
        ]
        if best_ob is not None:
            confluences.append(f"order_block_{best_ob.timeframe}")
        if total_liq_usd > 0:
            confluences.append(f"liquidations_usd_{total_liq_usd:.0f}")

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
