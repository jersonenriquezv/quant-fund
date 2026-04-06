-- Manual trading tables — run once to initialize.

CREATE TABLE IF NOT EXISTS manual_trades (
    id              SERIAL PRIMARY KEY,
    pair            TEXT NOT NULL,
    direction       TEXT NOT NULL,         -- 'long' or 'short'
    timeframe       TEXT,
    setup_type      TEXT,
    margin_type     TEXT DEFAULT 'linear', -- 'linear' or 'inverse'
    entry_price     NUMERIC NOT NULL,
    stop_loss       NUMERIC NOT NULL,
    take_profit_1   NUMERIC,
    take_profit_2   NUMERIC,
    account_balance NUMERIC NOT NULL,
    risk_percent    NUMERIC NOT NULL,
    risk_usd        NUMERIC NOT NULL,
    position_size   NUMERIC NOT NULL,
    position_value_usd NUMERIC,
    leverage        INT DEFAULT 7,
    margin_used     NUMERIC,
    sl_distance_pct NUMERIC,
    rr_ratio        NUMERIC,
    rr_ratio_tp2    NUMERIC,
    status          TEXT NOT NULL DEFAULT 'planned', -- planned, active, closed, cancelled
    result          TEXT,                            -- win, loss, breakeven, cancelled
    close_price     NUMERIC,
    pnl_usd        NUMERIC,
    pnl_percent     NUMERIC,
    r_multiple      NUMERIC,
    thesis          TEXT,
    fundamental_notes TEXT,
    mistakes        TEXT,
    screenshots     TEXT,
    tags            TEXT,
    -- Structured fundamental data (optional, for ML)
    spot_net_flow_4h    NUMERIC,
    futures_net_flow_4h NUMERIC,
    cg_ls_ratio         NUMERIC,
    cg_funding_rate     NUMERIC,
    fees_trend_wow      NUMERIC,
    tvl_delta_7d        NUMERIC,
    upcoming_unlock_usd NUMERIC,
    -- Timestamps
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    activated_at    TIMESTAMPTZ,
    closed_at       TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS manual_partial_closes (
    id                   SERIAL PRIMARY KEY,
    trade_id             INT NOT NULL REFERENCES manual_trades(id) ON DELETE CASCADE,
    close_price          NUMERIC NOT NULL,
    percentage           NUMERIC NOT NULL,
    position_size_closed NUMERIC NOT NULL,
    pnl_usd             NUMERIC,
    r_multiple           NUMERIC,
    notes                TEXT,
    closed_at            TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_manual_partial_trade ON manual_partial_closes(trade_id);

CREATE TABLE IF NOT EXISTS manual_balances (
    pair       TEXT PRIMARY KEY,
    balance    NUMERIC NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
