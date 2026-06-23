"""Centralized SQL queries — no ORM, raw asyncpg."""

import asyncpg

from dashboard.api import database as db

# Shadow trades (ml_setups) — terminal market outcomes that represent a
# theoretical trade resolving against price. Everything else (pre-execution
# gates, dedup, orphans, no-fill) is NOT a trade and must be excluded from the
# shadow viewer stats. Mirrors the training-filter intent in MEMORY.md.
SHADOW_TERMINAL_OUTCOMES: tuple[str, ...] = (
    "shadow_tp", "shadow_sl", "shadow_breakeven",
    "shadow_time_stop", "shadow_timeout",
)


async def get_trades(status: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
    async with db.pg_pool.acquire() as conn:
        if status:
            rows = await conn.fetch(
                """SELECT * FROM trades WHERE status = $1
                   ORDER BY opened_at DESC NULLS LAST LIMIT $2 OFFSET $3""",
                status, limit, offset,
            )
        else:
            rows = await conn.fetch(
                """SELECT * FROM trades
                   ORDER BY opened_at DESC NULLS LAST LIMIT $1 OFFSET $2""",
                limit, offset,
            )
    return [dict(r) for r in rows]


async def get_trade_by_id(trade_id: int) -> dict | None:
    async with db.pg_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM trades WHERE id = $1", trade_id)
    return dict(row) if row else None


async def get_ai_decisions_for_trade(trade_id: int) -> list[dict]:
    async with db.pg_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM ai_decisions WHERE trade_id = $1
               ORDER BY created_at DESC""",
            trade_id,
        )
    return [dict(r) for r in rows]


async def get_recent_ai_decisions(limit: int = 20) -> list[dict]:
    async with db.pg_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM ai_decisions
               ORDER BY created_at DESC NULLS LAST LIMIT $1""",
            limit,
        )
    return [dict(r) for r in rows]


async def get_recent_risk_events(limit: int = 20) -> list[dict]:
    async with db.pg_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM risk_events
               ORDER BY created_at DESC NULLS LAST LIMIT $1""",
            limit,
        )
    return [dict(r) for r in rows]


async def get_candles(pair: str, timeframe: str, count: int = 100) -> list[dict]:
    async with db.pg_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT timestamp, open, high, low, close, volume, volume_quote
               FROM candles
               WHERE pair = $1 AND timeframe = $2
               ORDER BY timestamp DESC LIMIT $3""",
            pair, timeframe, count,
        )
    return [dict(r) for r in reversed(rows)]


async def get_candles_range(
    pair: str, timeframe: str, from_ms: int, to_ms: int, limit: int = 5000
) -> list[dict]:
    """Range query for the TradingView Charting Library Datafeed getBars.

    Returns candles in [from_ms, to_ms] ascending. Capped at `limit` rows;
    when the window holds more, the OLDEST are dropped (keep the bars nearest
    `to_ms`, matching how a chart pages backward via countback).
    """
    async with db.pg_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT timestamp, open, high, low, close, volume, volume_quote
               FROM candles
               WHERE pair = $1 AND timeframe = $2
                 AND timestamp >= $3 AND timestamp <= $4
               ORDER BY timestamp DESC LIMIT $5""",
            pair, timeframe, from_ms, to_ms, limit,
        )
    return [dict(r) for r in reversed(rows)]


async def get_weekly_candles(
    pair: str, from_ms: int, to_ms: int, limit: int = 5000
) -> list[dict]:
    """Weekly candles aggregated from stored 1d candles (no 1w stored).

    Buckets daily bars into Monday-00:00-UTC weeks using tz-free integer math
    (epoch day 0 = Thursday, so shift by 3 to land week starts on Monday). The
    frontend's live forming-bar aggregation uses the identical formula, so a
    forming weekly bar lines up exactly with the closed ones. Same output shape
    as get_candles_range. Ascending, capped at `limit` (newest kept).
    """
    async with db.pg_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT
                   (((timestamp / 86400000) - (((timestamp / 86400000) + 3) % 7))
                    * 86400000) AS timestamp,
                   (array_agg(open ORDER BY timestamp ASC))[1]   AS open,
                   MAX(high)                                     AS high,
                   MIN(low)                                      AS low,
                   (array_agg(close ORDER BY timestamp DESC))[1] AS close,
                   SUM(volume)                                   AS volume,
                   SUM(volume_quote)                             AS volume_quote
               FROM candles
               WHERE pair = $1 AND timeframe = '1d'
                 AND timestamp >= $2 AND timestamp <= $3
               GROUP BY 1
               ORDER BY 1 DESC LIMIT $4""",
            pair, from_ms, to_ms, limit,
        )
    return [dict(r) for r in reversed(rows)]


async def get_trade_stats() -> dict:
    async with db.pg_pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT
                COUNT(*)::int AS total_trades,
                COUNT(*) FILTER (WHERE pnl_pct > 0)::int AS winning_trades,
                COUNT(*) FILTER (WHERE pnl_pct <= 0)::int AS losing_trades,
                COALESCE(SUM(pnl_usd), 0)::float AS total_pnl_usd,
                COALESCE(AVG(pnl_pct), 0)::float AS avg_pnl_pct,
                COALESCE(MAX(pnl_pct), 0)::float AS best_trade_pct,
                COALESCE(MIN(pnl_pct), 0)::float AS worst_trade_pct,
                COALESCE(SUM(pnl_usd) FILTER (WHERE pnl_usd > 0), 0)::float AS gross_profit,
                COALESCE(ABS(SUM(pnl_usd) FILTER (WHERE pnl_usd < 0)), 0)::float AS gross_loss
            FROM trades
            WHERE status = 'closed'
              AND exit_reason IS DISTINCT FROM 'orphaned_restart'
        """)
    d = dict(row)
    total = d["total_trades"]
    win = d["winning_trades"]
    d["win_rate"] = (win / total * 100) if total > 0 else 0.0
    gp = d.pop("gross_profit")
    gl = d.pop("gross_loss")
    d["profit_factor"] = (gp / gl) if gl > 0 else 0.0
    d["avg_rr"] = 0.0  # Requires per-trade RR calculation
    return d


async def get_shadow_trades(
    status: str | None = None,
    setup_type: str | None = None,
    experiment_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Shadow 'trades' from ml_setups.

    status='open'   → unresolved AND recent (bounds out ancient orphans).
    status='closed' → terminal market outcomes only.
    Defaults to the live EXPERIMENT_ID unless an explicit experiment_id is given.
    """
    from config.settings import settings

    exp = experiment_id or settings.EXPERIMENT_ID
    cols = (
        "setup_id, setup_type, pair, direction, entry_price, sl_price, "
        "tp1_price, tp2_price, outcome_type, pnl_pct, pnl_usd, actual_entry, "
        "entry_distance_pct, sl_distance_pct, created_at, resolved_at"
    )
    where = ["experiment_id = $1"]
    args: list = [exp]

    if status == "open":
        where.append("outcome_type IS NULL")
        where.append("created_at > now() - interval '48 hours'")
        order = "created_at DESC NULLS LAST"
    elif status == "closed":
        placeholders = ", ".join(f"${i + len(args) + 1}" for i in range(len(SHADOW_TERMINAL_OUTCOMES)))
        where.append(f"outcome_type IN ({placeholders})")
        args.extend(SHADOW_TERMINAL_OUTCOMES)
        order = "resolved_at DESC NULLS LAST"
    else:
        placeholders = ", ".join(f"${i + len(args) + 1}" for i in range(len(SHADOW_TERMINAL_OUTCOMES)))
        where.append(
            f"(outcome_type IN ({placeholders}) OR "
            f"(outcome_type IS NULL AND created_at > now() - interval '48 hours'))"
        )
        args.extend(SHADOW_TERMINAL_OUTCOMES)
        order = "COALESCE(resolved_at, created_at) DESC NULLS LAST"

    if setup_type:
        args.append(setup_type)
        where.append(f"setup_type = ${len(args)}")

    args.append(limit)
    limit_ph = f"${len(args)}"
    args.append(offset)
    offset_ph = f"${len(args)}"

    sql = (
        f"SELECT {cols} FROM ml_setups WHERE {' AND '.join(where)} "
        f"ORDER BY {order} LIMIT {limit_ph} OFFSET {offset_ph}"
    )
    async with db.pg_pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [dict(r) for r in rows]


async def get_shadow_stats(
    setup_type: str | None = None,
    experiment_id: str | None = None,
) -> dict:
    """Aggregate stats over terminal shadow outcomes, plus per-setup breakdown.

    pnl_usd is ALREADY net of taker fees ×2 (compute_pnl) — never re-deduct.
    """
    from config.settings import settings

    exp = experiment_id or settings.EXPERIMENT_ID
    placeholders = ", ".join(f"${i + 2}" for i in range(len(SHADOW_TERMINAL_OUTCOMES)))
    where = [f"experiment_id = $1", f"outcome_type IN ({placeholders})"]
    args: list = [exp, *SHADOW_TERMINAL_OUTCOMES]
    if setup_type:
        args.append(setup_type)
        where.append(f"setup_type = ${len(args)}")
    where_sql = " AND ".join(where)

    agg_sql = """
        COUNT(*)::int AS total_trades,
        COUNT(*) FILTER (WHERE pnl_usd > 0)::int AS winning_trades,
        COUNT(*) FILTER (WHERE pnl_usd <= 0)::int AS losing_trades,
        COALESCE(SUM(pnl_usd), 0)::float AS total_pnl_usd,
        COALESCE(AVG(pnl_pct), 0)::float AS avg_pnl_pct,
        COALESCE(MAX(pnl_pct), 0)::float AS best_trade_pct,
        COALESCE(MIN(pnl_pct), 0)::float AS worst_trade_pct,
        COALESCE(SUM(pnl_usd) FILTER (WHERE pnl_usd > 0), 0)::float AS gross_profit,
        COALESCE(ABS(SUM(pnl_usd) FILTER (WHERE pnl_usd < 0)), 0)::float AS gross_loss
    """
    async with db.pg_pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT {agg_sql} FROM ml_setups WHERE {where_sql}", *args)
        breakdown_rows = await conn.fetch(
            f"""SELECT setup_type, {agg_sql} FROM ml_setups WHERE {where_sql}
                GROUP BY setup_type ORDER BY total_pnl_usd DESC""",
            *args,
        )

    def _finish(d: dict) -> dict:
        total = d["total_trades"]
        d["win_rate"] = (d["winning_trades"] / total * 100) if total > 0 else 0.0
        gp = d.pop("gross_profit")
        gl = d.pop("gross_loss")
        d["profit_factor"] = (gp / gl) if gl > 0 else 0.0
        return d

    out = _finish(dict(row))
    out["experiment_id"] = exp
    out["by_setup_type"] = [_finish(dict(r)) for r in breakdown_rows]
    return out


async def set_cancel_request(pair: str) -> None:
    """Write a cancel request to Redis with 60s TTL."""
    key = f"qf:cancel_request:{pair}"
    await db.redis_client.set(key, "1", ex=60)


async def get_cancel_request(pair: str) -> bool:
    """Check if a cancel request exists for this pair."""
    key = f"qf:cancel_request:{pair}"
    val = await db.redis_client.get(key)
    return val is not None


async def pg_ping() -> bool:
    try:
        async with db.pg_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception:
        return False
