"""Background monitoring loops — session/dry-spell/market/liquidation alerts.

Long-running asyncio loops extracted from main.py (Refactor Phase 6,
docs/plans/main-py-split-phase6.md). Each is launched as a task by main()
and runs for the lifetime of the process, reaching services and per-pair
cooldown state via the shared `rt` singleton. None of them place orders;
they only emit Telegram alerts (already muted in shadow mode).

Pure relocation: function bodies + module constants are unchanged.
"""

import asyncio
import time
from datetime import datetime, timezone

from config.settings import settings
from shared.logger import setup_logger
from data_service.liquidation_estimator import estimate_liquidation_levels
from pipeline_runtime import rt

logger = setup_logger("monitoring_loops")


# ================================================================
# Trading session alerts
# ================================================================

# Sessions defined as (name, start_hour_utc, end_hour_utc, label).
# Intentionally OVERLAPPING — used only to trigger Telegram session-open
# alerts ("europe opens", "us opens"), so end_hour is informational.
# For ML labels (non-overlapping categorical), see `trading_session`
# feature in shared/ml_features.py (asia 0-8, europe 8-14, us 14-21,
# overlap 21-24). The two definitions serve different purposes; do not
# collapse them.
TRADING_SESSIONS = [
    ("asia", 0, 9, "00:00-09:00"),
    ("europe", 7, 16, "07:00-16:00"),
    ("us", 13, 22, "13:00-22:00"),
]


async def _session_alert_loop() -> None:
    """Send Telegram alert when a major trading session opens."""
    # Track which sessions we've already alerted today
    alerted: dict[str, int] = {}  # session_name -> day_of_year

    while True:
        try:
            now = datetime.now(timezone.utc)
            day = now.timetuple().tm_yday

            for name, start_hour, _, hours_label in TRADING_SESSIONS:
                if now.hour == start_hour and now.minute < 5:
                    if alerted.get(name) != day and rt.alert_manager:
                        await rt.alert_manager.notify_session_open(name, hours_label)
                        alerted[name] = day
        except Exception as e:
            logger.error(f"Session alert error: {e}")

        await asyncio.sleep(60)  # Check every minute


# ================================================================
# Dry spell alert — no setups detected in X hours
# ================================================================

_DRY_SPELL_THRESHOLD_HOURS = 4  # Alert after 4 hours of no setups


async def _dry_spell_loop() -> None:
    """Alert when no setups detected for extended period."""

    # Wait for bot to warm up
    await asyncio.sleep(300)

    while True:
        try:
            if rt.last_setup_detected_time > 0 and rt.alert_manager:
                hours_since = (time.time() - rt.last_setup_detected_time) / 3600
                if hours_since >= _DRY_SPELL_THRESHOLD_HOURS and not rt.dry_spell_alerted:
                    await rt.alert_manager.notify_dry_spell(
                        hours_since, settings.TRADING_PAIRS,
                    )
                    rt.dry_spell_alerted = True
                elif hours_since < _DRY_SPELL_THRESHOLD_HOURS:
                    rt.dry_spell_alerted = False  # Reset when setup found
            elif rt.last_setup_detected_time == 0 and rt.alert_manager:
                # Bot just started, no setup yet — check if it's been long enough
                hours_since = (time.time() - rt.bot_start_time) / 3600
                if hours_since >= _DRY_SPELL_THRESHOLD_HOURS and not rt.dry_spell_alerted:
                    await rt.alert_manager.notify_dry_spell(
                        hours_since, settings.TRADING_PAIRS,
                    )
                    rt.dry_spell_alerted = True
        except Exception as e:
            logger.error(f"Dry spell alert error: {e}")

        await asyncio.sleep(600)  # Check every 10 min


# ================================================================
# Volatility spike + funding extreme alerts
# ================================================================

_VOL_SPIKE_RATIO = 2.0  # ATR must be 2x above rolling average
_VOL_ALERT_COOLDOWN = 3600  # 1 hour between alerts per pair
_FUNDING_ALERT_COOLDOWN = 7200  # 2 hours between alerts per pair


async def _market_monitor_loop() -> None:
    """Monitor volatility spikes and funding extremes across all pairs."""
    # Wait for data to populate
    await asyncio.sleep(120)

    while True:
        try:
            now = time.time()

            for pair in settings.TRADING_PAIRS:
                if rt.data_service is None or rt.alert_manager is None:
                    continue

                # --- Volatility spike detection ---
                candles = rt.data_service.get_candles(pair, "5m", 100)
                if candles and len(candles) >= 50:
                    # Simple ATR: avg(high-low) over recent vs older window
                    recent = candles[-14:]
                    older = candles[-50:-14]
                    current_atr = sum(c.high - c.low for c in recent) / len(recent)
                    avg_atr = sum(c.high - c.low for c in older) / len(older)
                    price = candles[-1].close

                    if avg_atr > 0 and price > 0:
                        current_pct = current_atr / price
                        avg_pct = avg_atr / price
                        ratio = current_pct / avg_pct

                        last_alert = rt.vol_spike_cooldown.get(pair, 0)
                        if ratio >= _VOL_SPIKE_RATIO and (now - last_alert) > _VOL_ALERT_COOLDOWN:
                            await rt.alert_manager.notify_volatility_spike(
                                pair, current_pct, avg_pct,
                            )
                            rt.vol_spike_cooldown[pair] = now

                # --- Funding rate extreme detection ---
                funding = rt.data_service.get_funding_rate(pair)
                if funding and abs(funding.rate) >= settings.FUNDING_EXTREME_THRESHOLD:
                    last_alert = rt.funding_extreme_cooldown.get(pair, 0)
                    if (now - last_alert) > _FUNDING_ALERT_COOLDOWN:
                        direction = "long" if funding.rate < 0 else "short"
                        await rt.alert_manager.notify_funding_extreme(
                            pair, funding.rate, direction,
                        )
                        rt.funding_extreme_cooldown[pair] = now

                # --- Drawdown warning ---
                if rt.risk_service is not None:
                    daily_dd = rt.risk_service._state.get_daily_dd_pct()
                    dd_threshold = settings.MAX_DAILY_DRAWDOWN * settings.DD_WARNING_THRESHOLD
                    if daily_dd >= dd_threshold and daily_dd < settings.MAX_DAILY_DRAWDOWN:
                        await rt.alert_manager.notify_dd_warning(
                            daily_dd, settings.MAX_DAILY_DRAWDOWN,
                        )

        except Exception as e:
            logger.error(f"Market monitor error: {e}")

        await asyncio.sleep(300)  # Check every 5 min


async def _liquidation_alert_loop() -> None:
    """Send top liquidation clusters near price every 4 hours via Telegram."""
    # Wait 60s for data to populate on startup
    await asyncio.sleep(60)

    while True:
        try:
            await _send_liquidation_alert()
        except Exception as e:
            logger.error(f"Liquidation alert error: {e}")

        await asyncio.sleep(4 * 3600)  # 4 hours


async def _send_liquidation_alert() -> None:
    """Compute and send top liquidation clusters for all pairs."""
    if rt.data_service is None or rt.alert_manager is None:
        return

    all_clusters: list[dict] = []

    for pair in settings.TRADING_PAIRS:
        candles = rt.data_service.get_candles(pair, "5m", settings.LIQ_CANDLE_COUNT)
        oi = rt.data_service.get_open_interest(pair)
        if not candles or oi is None or oi.oi_usd <= 0:
            continue

        current_price = candles[-1].close
        if current_price <= 0:
            continue

        bins = estimate_liquidation_levels(candles, oi.oi_usd, pair)
        if not bins:
            continue

        # Find top clusters above price (short liquidations — fuel for up moves)
        above = []
        for b in bins:
            if b.price > current_price and b.liq_short_usd > 0:
                dist_pct = (b.price - current_price) / current_price * 100
                if dist_pct <= 10:  # Within 10% above
                    above.append({
                        "price": b.price,
                        "usd": b.liq_short_usd,
                        "dist_pct": dist_pct,
                    })

        # Find top clusters below price (long liquidations — fuel for down moves)
        below = []
        for b in bins:
            if b.price < current_price and b.liq_long_usd > 0:
                dist_pct = (b.price - current_price) / current_price * 100
                if dist_pct >= -10:  # Within 10% below
                    below.append({
                        "price": b.price,
                        "usd": b.liq_long_usd,
                        "dist_pct": dist_pct,
                    })

        # Sort by USD size and take top 3 each
        above.sort(key=lambda x: x["usd"], reverse=True)
        below.sort(key=lambda x: x["usd"], reverse=True)

        all_clusters.append({
            "pair": pair,
            "price": current_price,
            "above": above[:3],
            "below": below[:3],
        })

    if all_clusters:
        await rt.alert_manager.notify_liquidation_clusters(all_clusters)
