"""
Quick Setup Evaluation — Setup D (variants d_bos, d_choch).

Data-driven setups with shorter duration (4h max) and lower R:R (1:1 min).
These fire only when no swing setup (A/B) is detected.

Setup D: LTF Structure Scalp — CHoCH/BOS on 5m + fresh OB, no sweep/FVG needed

Setups C/E/H removed 2026-04-13 (signals demoted to confluence boosters in
setups.py). `setup_c` retained in QUICK_SETUP_TYPES for ml_setups compat only.
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
    """Evaluates quick trade setups (D variants) from market data signals."""

    _ob_scorer = SetupEvaluator()

    def evaluate_setup_d(
        self,
        pair: str,
        htf_bias: str,
        structure_state: MarketStructureState,
        active_obs: list[OrderBlock],
        pd_zone: Optional[PremiumDiscountZone],
        candles: list[Candle],
        snapshot: Optional[MarketSnapshot] = None,
        swing_highs_htf: Optional[list] = None,
        swing_lows_htf: Optional[list] = None,
        volume_profile=None,
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

        # TPs: structural targeting when enabled (swing highs/lows + VP POC/VAH/VAL).
        # Falls back to fixed R:R (TP1_RR_RATIO / SETUP_TP2_RR[variant]) when no
        # structural level meets the R:R minimums. Delegates to SetupEvaluator for
        # single source of truth — matches swing setups A/B/F/G logic.
        tp1, tp2 = self._ob_scorer._calculate_tp_levels(
            entry_price, sl_price, direction, [],
            setup_type=variant,
            swing_highs_htf=swing_highs_htf,
            swing_lows_htf=swing_lows_htf,
            volume_profile=volume_profile,
        )

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
