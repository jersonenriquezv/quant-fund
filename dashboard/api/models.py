"""Pydantic response models for the dashboard API."""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    postgres: bool
    redis: bool
    sandbox: bool = True


class MarketData(BaseModel):
    pair: str
    price: float | None = None
    change_pct: float | None = None
    funding_rate: float | None = None
    next_funding_rate: float | None = None
    next_funding_time: int | None = None
    oi_usd: float | None = None
    oi_base: float | None = None


class TradeRecord(BaseModel):
    id: int
    pair: str | None
    direction: str | None
    setup_type: str | None
    entry_price: float | None
    sl_price: float | None
    tp1_price: float | None
    tp2_price: float | None
    tp3_price: float | None
    actual_entry: float | None
    actual_exit: float | None
    exit_reason: str | None
    position_size: float | None
    pnl_usd: float | None
    pnl_pct: float | None
    ai_confidence: float | None
    opened_at: str | None
    closed_at: str | None
    status: str | None


class TradeDetail(TradeRecord):
    ai_decisions: list["AIDecisionRecord"] = []


class AIDecisionRecord(BaseModel):
    id: int
    trade_id: int | None
    pair: str | None = None
    direction: str | None = None
    setup_type: str | None = None
    approved: bool | None = None
    confidence: float | None
    reasoning: str | None
    adjustments: dict | None
    warnings: list | None
    created_at: str | None


class RiskState(BaseModel):
    daily_dd_pct: float | None = None
    weekly_dd_pct: float | None = None
    open_positions: int = 0
    max_positions: int = 3
    cooldown_until: int | None = None
    recent_events: list["RiskEventRecord"] = []


class RiskEventRecord(BaseModel):
    id: int
    event_type: str | None
    details: dict | None
    created_at: str | None


class CandleRecord(BaseModel):
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class StatsResponse(BaseModel):
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl_usd: float = 0.0
    avg_pnl_pct: float = 0.0
    best_trade_pct: float = 0.0
    worst_trade_pct: float = 0.0
    profit_factor: float = 0.0
    avg_rr: float = 0.0


class WhaleMovementRecord(BaseModel):
    timestamp: int
    wallet: str
    label: str
    action: str
    amount: float
    exchange: str
    significance: str
    chain: str


class OrderBlockRecord(BaseModel):
    timestamp: int
    pair: str
    timeframe: str
    direction: str
    high: float
    low: float
    body_high: float
    body_low: float
    entry_price: float
    volume_ratio: float


class HTFBiasResponse(BaseModel):
    bias: dict[str, str]


class PositionData(BaseModel):
    pair: str
    direction: str
    setup_type: str
    phase: str
    entry_price: float
    actual_entry_price: float | None = None
    sl_price: float
    tp1_price: float
    tp2_price: float
    tp3_price: float
    filled_size: float
    leverage: float
    ai_confidence: float
    pnl_pct: float
    created_at: int
    filled_at: int | None = None
