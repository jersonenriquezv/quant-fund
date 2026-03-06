"""
OI-based liquidation proxy — detects liquidation cascades via OI drops.

Uses OKX Open Interest data (polled every 5 minutes) to infer liquidation
cascades. If OI drops >2% within a 5-minute window, a significant number of
positions were forcefully closed — likely a liquidation cascade.

Each detected cascade generates a single LiquidationEvent with the estimated
total USD liquidated (based on OI delta).
"""

import time
from collections import deque

from config.settings import settings
from shared.logger import setup_logger
from shared.models import OpenInterest, LiquidationEvent

logger = setup_logger("data_service")

# Max OI snapshots to keep per pair (12 × 5min = 1 hour of history)
_MAX_SNAPSHOTS = 12


class OILiquidationProxy:
    """Detects liquidation cascades from OI drops.

    Fed new OI data by DataService._oi_loop(). Not an async task —
    it's a passive module that processes snapshots synchronously.
    """

    def __init__(self):
        # Ring buffer of OI snapshots per pair: {pair: deque[OpenInterest]}
        self._snapshots: dict[str, deque[OpenInterest]] = {}

        # Detected liquidation events (pruned to last hour)
        self._events: list[LiquidationEvent] = []

    # ================================================================
    # Public interface
    # ================================================================

    def get_recent_liquidations(self, pair: str | None = None,
                                minutes: int = 60) -> list[LiquidationEvent]:
        """Get proxy liquidation events from the last N minutes."""
        cutoff = int((time.time() - minutes * 60) * 1000)
        events = [e for e in self._events if e.timestamp >= cutoff]
        if pair:
            events = [e for e in events if e.pair == pair]
        return events

    def get_aggregated_stats(self, pair: str,
                             minutes: int = 5) -> dict:
        """Get aggregated liquidation stats for a pair over N minutes."""
        events = self.get_recent_liquidations(pair, minutes)
        long_usd = sum(e.size_usd for e in events if e.side == "long")
        short_usd = sum(e.size_usd for e in events if e.side == "short")
        return {
            "total_usd": long_usd + short_usd,
            "long_usd": long_usd,
            "short_usd": short_usd,
            "count": len(events),
        }

    @property
    def is_connected(self) -> bool:
        """Always True — no WebSocket, fed directly by OI polling loop."""
        return True

    # ================================================================
    # Feed — called by DataService._oi_loop()
    # ================================================================

    def update(self, oi: OpenInterest) -> None:
        """Process a new OI snapshot. Detects drops and generates events.

        Called every OI_CHECK_INTERVAL (5 min) by DataService.
        """
        pair = oi.pair

        if pair not in self._snapshots:
            self._snapshots[pair] = deque(maxlen=_MAX_SNAPSHOTS)

        buf = self._snapshots[pair]

        # Find the snapshot closest to OI_DROP_WINDOW_SECONDS ago
        prev = self._get_window_snapshot(buf, oi.timestamp)

        # Store new snapshot
        buf.append(oi)

        if prev is None:
            # Not enough data yet — need at least one prior snapshot
            return

        if prev.oi_usd <= 0:
            return

        # Calculate OI change
        drop_pct = (prev.oi_usd - oi.oi_usd) / prev.oi_usd

        if drop_pct >= settings.OI_DROP_THRESHOLD_PCT:
            estimated_liq_usd = prev.oi_usd - oi.oi_usd
            self._generate_event(oi, drop_pct, estimated_liq_usd)

    # ================================================================
    # Internal
    # ================================================================

    def _get_window_snapshot(
        self, buf: deque[OpenInterest], current_ts: int
    ) -> OpenInterest | None:
        """Find the snapshot closest to OI_DROP_WINDOW_SECONDS before current_ts."""
        if not buf:
            return None

        window_ms = settings.OI_DROP_WINDOW_SECONDS * 1000
        target_ts = current_ts - window_ms

        # Find the snapshot with timestamp closest to target
        best = None
        best_diff = float("inf")
        for snap in buf:
            diff = abs(snap.timestamp - target_ts)
            if diff < best_diff:
                best_diff = diff
                best = snap

        return best

    def _generate_event(
        self, oi: OpenInterest, drop_pct: float, estimated_usd: float
    ) -> None:
        """Create a LiquidationEvent from an OI drop detection."""
        # OI drop doesn't tell us which side was liquidated.
        # Convention: if price likely dropped, longs were liquidated.
        # Since we don't have price here, mark as "unknown" direction
        # using "long" as the conservative default (the common case in crypto).
        event = LiquidationEvent(
            timestamp=oi.timestamp,
            pair=oi.pair,
            side="long",
            size_usd=estimated_usd,
            price=0.0,  # Unknown from OI data alone
            source="oi_proxy",
        )

        self._events.append(event)
        self._prune_old_events()

        logger.info(
            f"OI proxy: liquidation cascade detected — "
            f"pair={oi.pair} drop={drop_pct:.2%} "
            f"estimated=${estimated_usd:,.0f}"
        )

    def _prune_old_events(self) -> None:
        """Remove events older than 1 hour."""
        cutoff = int((time.time() - 3600) * 1000)
        self._events = [e for e in self._events if e.timestamp >= cutoff]
