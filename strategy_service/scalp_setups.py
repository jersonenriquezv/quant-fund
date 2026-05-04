"""
Scalp Shadow Signals — v1 experiment.

Microstructural scalping signals tested in shadow mode only. No live execution.
Plan: docs/plans/scalp_shadow_v1.md.

Five candidates (4 signals + control baseline):
- scalp_liq_reclaim_v1     — OI drop + wick reclaim
- scalp_sweep_choch_v1     — sweep of 20-bar high/low + 1m CHoCH
- scalp_vol_cvd_div_v1     — 3-sigma volume + CVD/price divergence
- scalp_funding_extreme_v1 — funding extreme + flat price last 30min
- scalp_random_baseline_v1 — random control, frequency-matched

This module is a scaffold. All detector methods return None until their
respective commits land. The wiring into shadow_monitor is also deferred
to per-signal commits via SHADOW_MODE_SETUPS registration.
"""

from typing import Optional

from config.settings import settings
from shared.logger import setup_logger
from shared.models import Candle, MarketSnapshot, TradeSetup

logger = setup_logger("strategy_scalp_setups")


class ScalpSetupEvaluator:
    """Evaluates scalp microstructural setups for shadow tracking only.

    Each evaluate_* method returns a TradeSetup with the proper setup_type and
    TP/SL derived from settings.SCALP_SIGNAL_PARAMS, or None if no signal fires.

    Wiring contract:
    - Caller passes per-pair candle list (1m primary), optional MarketSnapshot.
    - Returned TradeSetup must have setup_type in settings.SCALP_SETUP_TYPES.
    - Pipeline routes to shadow_monitor when setup_type is also in SHADOW_MODE_SETUPS.
    - Per-signal time_stop_seconds applied by shadow_monitor (separate plumbing
      added in the time-stop commit).
    """

    def __init__(self) -> None:
        self._params = settings.SCALP_SIGNAL_PARAMS

    def evaluate_liq_reclaim(
        self,
        pair: str,
        candles_1m: list[Candle],
        snapshot: Optional[MarketSnapshot],
    ) -> Optional[TradeSetup]:
        """Signal 1 — Liquidation reclaim.

        Trigger: OI drop >= 2% in 5min (oi_liquidation_proxy).
        Confirmation: 1m candle wick >= 0.5% with close back inside last 20-bar range.
        Direction: counter to wick.
        """
        return None

    def evaluate_sweep_choch(
        self,
        pair: str,
        candles_1m: list[Candle],
        snapshot: Optional[MarketSnapshot],
    ) -> Optional[TradeSetup]:
        """Signal 2 — Sweep + 1m CHoCH.

        Trigger: price takes high/low of last 20x1m candles.
        Confirmation: next 1m candle closes back inside range, body >= 60% of range.
        Direction: counter to sweep.
        """
        return None

    def evaluate_vol_cvd_divergence(
        self,
        pair: str,
        candles_1m: list[Candle],
        snapshot: Optional[MarketSnapshot],
    ) -> Optional[TradeSetup]:
        """Signal 3 — Volume Z-score + CVD divergence.

        Trigger: 1m volume >= 3 sigma over 20-period mean AND CVD direction
        opposite to candle direction.
        Confirmation: orderbook spread <= 2bps.
        Direction: dirección de CVD (no del precio).
        """
        return None

    def evaluate_funding_extreme(
        self,
        pair: str,
        candles_1m: list[Candle],
        snapshot: Optional[MarketSnapshot],
    ) -> Optional[TradeSetup]:
        """Signal 4 — Funding extreme + flat price.

        Trigger: funding rate >= 0.05% (8h) AND price range last 30min <= 0.3%.
        Direction: counter to funding.
        """
        return None

    def evaluate_random_baseline(
        self,
        pair: str,
        candles_1m: list[Candle],
        snapshot: Optional[MarketSnapshot],
    ) -> Optional[TradeSetup]:
        """Control — Random baseline.

        Uniform random emission, frequency-matched to combined S1-S4 firing rate.
        Direction 50/50. TP/SL/time_stop rotated to match the signal under comparison.
        Purpose: any "winning" signal must beat this baseline by >= 15pp WR.
        """
        return None
