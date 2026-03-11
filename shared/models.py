"""
Typed dataclasses shared between all 5 services.

Every piece of data that flows between layers MUST be one of these types.
No raw dicts. If you need a new data shape, define it here first.

All fields match CLAUDE.md section "Shared Data Models" exactly.
"""

from dataclasses import dataclass, field
from typing import Optional


# ============================================================
# Layer 1: Data Service outputs
# ============================================================

@dataclass(frozen=True)
class Candle:
    """OHLCV candle from OKX. Only process when confirmed=True."""
    timestamp: int          # Unix ms
    open: float
    high: float
    low: float
    close: float
    volume: float           # Base currency (BTC/ETH)
    volume_quote: float     # USDT
    pair: str               # "BTC/USDT"
    timeframe: str          # "5m", "15m", "1h", "4h"
    confirmed: bool         # True only when candle is closed


@dataclass(frozen=True)
class FundingRate:
    """Perpetual funding rate from OKX. Updates every 8 hours."""
    timestamp: int
    pair: str
    rate: float             # e.g., 0.0001 = 0.01%
    next_rate: float        # Estimated next funding rate
    next_funding_time: int  # Unix ms


@dataclass(frozen=True)
class OpenInterest:
    """Open interest snapshot from OKX. Polled every 5 minutes."""
    timestamp: int
    pair: str
    oi_contracts: float     # In contracts
    oi_base: float          # In BTC/ETH
    oi_usd: float           # Approximate USD value


@dataclass(frozen=True)
class CVDSnapshot:
    """Cumulative Volume Delta calculated from OKX trade stream.
    CVD rising = aggressive buyers dominate (bullish).
    CVD falling = aggressive sellers dominate (bearish).
    """
    timestamp: int
    pair: str
    cvd_5m: float           # CVD accumulated last 5 min
    cvd_15m: float          # Last 15 min
    cvd_1h: float           # Last hour
    buy_volume: float       # Buy volume in the period
    sell_volume: float      # Sell volume in the period


@dataclass(frozen=True)
class OIFlushEvent:
    """OI flush event — detected when OI drops >2% in 5min window.
    Indicates a liquidation cascade via OI proxy.
    """
    timestamp: int
    pair: str
    side: str               # "long" or "short"
    size_usd: float
    price: float
    source: str             # "oi_proxy"


@dataclass(frozen=True)
class WhaleMovement:
    """Large crypto transfer detected via Etherscan (ETH) or mempool.space (BTC).
    Exchange deposit = bearish signal (potential sell).
    Exchange withdrawal = bullish signal (accumulation).
    Non-exchange transfer = neutral/informational signal.
    """
    timestamp: int
    wallet: str
    action: str             # "exchange_deposit", "exchange_withdrawal", "transfer_out", "transfer_in"
    amount: float           # ETH or BTC amount
    exchange: str           # Exchange name ("Binance", "OKX") or truncated address ("0xab12...ef34")
    significance: str       # "high" or "medium"
    chain: str              # "ETH" or "BTC"
    wallet_label: str = ""  # Human-readable label ("Vitalik Buterin", "Galaxy Digital")
    amount_usd: float = 0.0    # USD value at time of detection
    market_price: float = 0.0  # Asset price (USD) when movement was detected


@dataclass(frozen=True)
class NewsHeadline:
    """Single news headline from CryptoCompare."""
    title: str
    source: str
    timestamp: int          # Unix ms
    category: str           # "BTC", "ETH", etc.
    url: str = ""           # Link to the article
    sentiment: Optional[str] = None  # "bullish", "bearish", or None if unavailable


@dataclass(frozen=True)
class NewsSentiment:
    """Aggregated news sentiment: Fear & Greed score + recent headlines."""
    score: int                          # 0-100 (Fear & Greed Index)
    label: str                          # "Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed"
    headlines: list                     # list[NewsHeadline]
    fetched_at: int                     # Unix ms


@dataclass
class MarketSnapshot:
    """All market data for a pair at a point in time.
    The main pipeline passes this as a single object instead of
    making multiple calls for each data type.
    """
    pair: str
    timestamp: int
    funding: Optional[FundingRate] = None
    oi: Optional[OpenInterest] = None
    cvd: Optional[CVDSnapshot] = None
    recent_oi_flushes: list[OIFlushEvent] = field(default_factory=list)
    whale_movements: list[WhaleMovement] = field(default_factory=list)
    news_sentiment: Optional[NewsSentiment] = None
    health: Optional["SnapshotHealth"] = None


@dataclass(frozen=True)
class SourceFreshness:
    """Freshness status of a single data source in MarketSnapshot."""
    name: str
    priority: str          # "critical", "supporting", "decorative"
    age_ms: Optional[int]  # None if source unavailable
    is_stale: bool         # True if age > threshold or unavailable


@dataclass(frozen=True)
class SnapshotHealth:
    """Aggregate health of a MarketSnapshot — freshness + completeness."""
    sources: tuple[SourceFreshness, ...]
    completeness_pct: float          # 0.0-1.0 (fraction of sources available)
    critical_sources_healthy: bool   # True only if ALL critical sources are fresh
    stale_sources: tuple[str, ...]   # Names of stale sources
    missing_sources: tuple[str, ...]  # Names of unavailable sources


# ============================================================
# Layer 2: Strategy Service outputs
# ============================================================

@dataclass(frozen=True)
class TradeSetup:
    """Detected trading setup ready for AI + Risk evaluation."""
    timestamp: int
    pair: str
    direction: str          # "long" or "short"
    setup_type: str         # "setup_a" or "setup_b"
    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: float
    confluences: list       # List of confirmation strings found
    htf_bias: str           # "bullish" or "bearish"
    ob_timeframe: str       # Timeframe of the order block used


# ============================================================
# Layer 3: AI Service outputs
# ============================================================

@dataclass(frozen=True)
class AIDecision:
    """Claude's evaluation of a TradeSetup."""
    confidence: float       # 0.0-1.0, minimum 0.60 to proceed
    approved: bool
    reasoning: str
    adjustments: dict       # Optional SL/TP modifications
    warnings: list          # Risk factors detected


# ============================================================
# Layer 4: Risk Service outputs
# ============================================================

@dataclass(frozen=True)
class RiskApproval:
    """Final risk check result. If approved=False, trade does NOT execute."""
    approved: bool
    position_size: float    # In base currency (BTC/ETH)
    leverage: float
    risk_pct: float
    reason: str             # If rejected, explains why
