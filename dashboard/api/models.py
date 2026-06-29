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


class ShadowTradeRecord(BaseModel):
    setup_id: str | None
    setup_type: str | None
    pair: str | None
    direction: str | None
    entry_price: float | None
    sl_price: float | None
    tp1_price: float | None
    tp2_price: float | None
    actual_entry: float | None
    entry_distance_pct: float | None
    sl_distance_pct: float | None
    outcome_type: str | None
    pnl_pct: float | None
    pnl_usd: float | None
    created_at: str | None
    resolved_at: str | None
    status: str  # "open" | "closed"


class ShadowSetupBreakdown(BaseModel):
    setup_type: str | None
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    total_pnl_usd: float = 0.0
    avg_pnl_pct: float = 0.0
    best_trade_pct: float = 0.0
    worst_trade_pct: float = 0.0
    # Recency context — the all-time PF above can be carried entirely by an old
    # luck cluster. recent_* covers the most-recent HALF of trades by count, so
    # decay is visible. `decayed` flags a headline carried by old trades.
    recent_n: int = 0
    recent_pnl_usd: float = 0.0
    recent_profit_factor: float = 0.0
    decayed: bool = False


class ShadowStats(BaseModel):
    experiment_id: str | None = None
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    total_pnl_usd: float = 0.0
    avg_pnl_pct: float = 0.0
    best_trade_pct: float = 0.0
    worst_trade_pct: float = 0.0
    by_setup_type: list[ShadowSetupBreakdown] = []


class ShadowEquityPoint(BaseModel):
    ts: str
    equity: float
    pnl_usd: float
    setup_type: str | None
    pair: str | None


class ShadowEquityResponse(BaseModel):
    experiment_id: str | None = None
    start_balance: float = 0.0
    current_balance: float = 0.0
    total_profit: float = 0.0
    return_pct: float = 0.0
    max_drawdown_usd: float = 0.0
    max_drawdown_pct: float = 0.0
    n: int = 0
    points: list[ShadowEquityPoint] = []


class ShadowMLArm(BaseModel):
    wr: float | None = None
    pf: float | None = None  # null = infinite (no losers) or undefined
    pnl: float | None = None
    n: int | None = None


class ShadowMLStatus(BaseModel):
    available: bool = False
    cutoff_created_at: str | None = None
    train_n: int | None = None
    n_forward: int = 0
    n_gate: int = 100
    gate_reached: bool = False
    verdict_state: str | None = None  # accumulating | pass | fail
    verdict: str | None = None
    take_all: ShadowMLArm | None = None
    top_half: ShadowMLArm | None = None
    bottom_half: ShadowMLArm | None = None
    updated_at: str | None = None
    # Training-data milestone — DIFFERENT axis from the forward gate above.
    # Counts ALL engine1 binary outcomes (shadow_tp/shadow_sl, fv>=4), pre +
    # post freeze = the dataset size available to RE-TRAIN a new model. The
    # forward gate validates the CURRENT frozen model on unseen data; this just
    # measures whether we have enough data to train a better one. Mirrors
    # scripts/alert_ml_milestone.sh (threshold 500).
    milestone_n: int = 0
    milestone_threshold: int = 500


class ShadowDTTrade(BaseModel):
    pair: str
    side: int  # +1 long, -1 short
    reason: str  # flip | sl
    entry_ts: int
    exit_ts: int
    entry_price: float
    exit_price: float
    qty: float
    pnl_net: float


class ShadowDTPoint(BaseModel):
    ts: int
    equity: float
    pnl_net: float
    reason: str


class ShadowDTResponse(BaseModel):
    available: bool = False
    start_balance: float = 0.0
    current_balance: float = 0.0
    total_profit: float = 0.0
    return_pct: float = 0.0
    max_drawdown_usd: float = 0.0
    max_drawdown_pct: float = 0.0
    n: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    profit_factor: float | None = None
    points: list[ShadowDTPoint] = []
    trades: list[ShadowDTTrade] = []


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


class SentimentResponse(BaseModel):
    score: int | None = None
    label: str | None = None


class HeadlineRecord(BaseModel):
    title: str
    source: str
    timestamp: int
    category: str
    url: str = ""
    sentiment: str | None = None


class HeadlinesResponse(BaseModel):
    headlines: list[HeadlineRecord] = []


class HTFBiasResponse(BaseModel):
    bias: dict[str, str]


class LiqHeatmapBin(BaseModel):
    price: float
    liq_long_usd: float
    liq_short_usd: float


class LiqHeatmapResponse(BaseModel):
    pair: str
    current_price: float
    bins: list[LiqHeatmapBin]


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
