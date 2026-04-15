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
        """REMOVED 2026-04-13: Setup C — Funding Squeeze.

        0 resolved trades. Entered at market price with fixed % SL — no structural
        OB anchor. Violates the golden rule: no Order Block = no trade.

        Funding extreme signal now flows as a confluence booster into
        _check_volume_confirmation() (setups.py) for OB-anchored setups.
        """
        return None

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
            cvd_15m_warm = getattr(cvd, "is_window_warm", lambda _window: True)("15m")
            total_vol = cvd.buy_volume + cvd.sell_volume
            if total_vol > 0:
                buy_dom = cvd.buy_volume / total_vol
                if direction == "bullish" and cvd_15m_warm and cvd.cvd_15m > 0:
                    confluences.append("cvd_aligned_bullish")
                    if buy_dom >= settings.BUY_DOMINANCE_STRONG_PCT:
                        confluences.append("buy_dominance_strong")
                elif direction == "bearish" and cvd_15m_warm and cvd.cvd_15m < 0:
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
        """REMOVED 2026-04-13: Setup E — Cascade Reversal.

        0W/1L. Entered at market price when no OB found — no structural anchor.
        OI cascade signal now flows as a confluence booster into
        _check_volume_confirmation() (setups.py) for OB-anchored setups.
        """
        return None

    def evaluate_setup_h(
        self,
        pair: str,
        htf_bias: str,
        structure_state: MarketStructureState,
        candles: list[Candle],
        snapshot: Optional[MarketSnapshot] = None,
    ) -> Optional[TradeSetup]:
        """REMOVED 2026-04-13: Setup H — Momentum/Impulse Entry.

        0/13 live WR, 27 trades at 11% WR, PF 0.10. Entry at market price
        during impulse = adverse selection (AFML Ch.5). This is what institutions
        profit FROM — retail chasing momentum. 74/104 trades in one backtest
        period, accounting for -$1,144.

        Future redesign would require: OB pullback entry (wait for retest),
        structural SL below the OB that caused the impulse.
        """
        return None

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
