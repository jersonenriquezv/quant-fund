"""
Data integrity module — central hub for data quality types and logic.

Contains:
- DataServiceState enum (RUNNING / RECOVERING / DEGRADED)
- CVDState enum (VALID / WARMING_UP / INVALID)
- Per-setup data dependency map
- can_trade_setup() gating function
- CircuitBreaker for reconnect storm detection
- Contract size constants (shared with CVD calculator)
"""

import time
from enum import Enum

from shared.models import SnapshotHealth


# ================================================================
# Enums
# ================================================================

class DataServiceState(Enum):
    """Global state of the DataService."""
    RUNNING = "running"
    RECOVERING = "recovering"
    DEGRADED = "degraded"


class CVDState(Enum):
    """CVD calculator validity state per pair."""
    VALID = "valid"
    WARMING_UP = "warming_up"
    INVALID = "invalid"


# ================================================================
# OKX perpetual contract sizes (base currency per contract)
# Shared between exchange_client.py and cvd_calculator.py
# ================================================================

CONTRACT_SIZES = {
    "BTC/USDT": 0.01,    # 1 contract = 0.01 BTC
    "ETH/USDT": 0.1,     # 1 contract = 0.1 ETH
    "SOL/USDT": 1.0,     # 1 contract = 1 SOL
    "DOGE/USDT": 1000.0, # 1 contract = 1000 DOGE
    "XRP/USDT": 100.0,   # 1 contract = 100 XRP
    "LINK/USDT": 1.0,    # 1 contract = 1 LINK
    "AVAX/USDT": 1.0,    # 1 contract = 1 AVAX
}


# ================================================================
# Per-setup data dependencies
# ================================================================

# Maps setup_type → set of data sources required for that setup to trade.
# "candles" is universal — every setup needs clean candle data.
SETUP_DATA_DEPS: dict[str, set[str]] = {
    "setup_a": {"candles"},
    "setup_b": {"candles"},
    "setup_c": {"candles", "funding", "cvd"},
    "setup_d_choch": {"candles"},
    "setup_d_bos": {"candles"},
    "setup_e": {"candles", "oi"},
    "setup_f": {"candles"},
    "setup_h": {"candles"},
}

# These block ALL setups when unavailable
UNIVERSAL_DEPS = {"candles"}


# ================================================================
# Gating function
# ================================================================

def can_trade_setup(
    setup_type: str,
    health: SnapshotHealth | None,
    service_state: DataServiceState,
    cvd_state: CVDState,
) -> tuple[bool, str]:
    """Check if a setup can trade given current data quality.

    Returns:
        (allowed, reason) — if not allowed, reason explains why.
    """
    # Global state check
    if service_state != DataServiceState.RUNNING:
        return False, f"service {service_state.name}"

    # Get deps for this setup type
    deps = SETUP_DATA_DEPS.get(setup_type, UNIVERSAL_DEPS)

    # CVD dependency check
    if "cvd" in deps and cvd_state != CVDState.VALID:
        return False, f"cvd {cvd_state.name}"

    # Health-based checks for non-candle deps
    if health is not None:
        missing = set(health.missing_sources)
        stale = set(health.stale_sources)
        unavailable = missing | stale

        for dep in deps:
            if dep == "candles":
                # Candle health is checked via service_state (RUNNING implies candles OK)
                continue
            if dep == "cvd":
                # Already checked above
                continue
            if dep in unavailable:
                return False, f"{dep} unavailable"

    return True, ""


# ================================================================
# Circuit Breaker
# ================================================================

# ================================================================
# Timeframe helpers
# ================================================================

TIMEFRAME_MS = {
    "1m": 1 * 60 * 1000,
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}


def validate_candle_continuity(
    candles: list,
    timeframe: str,
    tolerance_factor: float = 1.5,
) -> tuple[bool, int]:
    """Check that candle timestamps are continuous for the given timeframe.

    Args:
        candles: List of Candle objects, oldest first.
        timeframe: e.g. "5m", "15m"
        tolerance_factor: How much slack to allow (1.5 = 150% of expected interval).
            Accounts for exchange delays and minor timing jitter.

    Returns:
        (is_continuous, gap_count) — True if no gaps found, plus count of gaps.
    """
    if len(candles) < 2:
        return True, 0

    expected_ms = TIMEFRAME_MS.get(timeframe)
    if expected_ms is None:
        return True, 0  # Unknown timeframe — can't validate

    max_gap_ms = expected_ms * tolerance_factor
    gap_count = 0

    for i in range(1, len(candles)):
        diff = candles[i].timestamp - candles[i - 1].timestamp
        if diff > max_gap_ms:
            gap_count += 1

    return gap_count == 0, gap_count


class CircuitBreaker:
    """Tracks reconnect events in a sliding window.

    Trips after max_events in window_seconds.
    Auto-resets after stable_seconds of no events.
    """

    def __init__(self, max_events: int, window_seconds: int, stable_seconds: int):
        self._max_events = max_events
        self._window_seconds = window_seconds
        self._stable_seconds = stable_seconds
        self._events: list[float] = []
        self._tripped = False

    def record_event(self) -> None:
        """Record a reconnect event."""
        now = time.monotonic()
        self._events.append(now)
        self._prune(now)

        if len(self._events) >= self._max_events:
            self._tripped = True

    @property
    def is_tripped(self) -> bool:
        """Check if circuit breaker is tripped (with auto-reset)."""
        if not self._tripped:
            return False

        now = time.monotonic()
        self._prune(now)

        # Auto-reset: if no events in stable_seconds
        if self._events:
            last = self._events[-1]
            if (now - last) >= self._stable_seconds:
                self._tripped = False
                self._events.clear()
                return False
        else:
            # No events left after prune — reset
            self._tripped = False
            return False

        return True

    def reset(self) -> None:
        """Manually reset the circuit breaker."""
        self._tripped = False
        self._events.clear()

    def _prune(self, now: float) -> None:
        """Remove events outside the sliding window."""
        cutoff = now - self._window_seconds
        self._events = [t for t in self._events if t >= cutoff]
