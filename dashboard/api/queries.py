"""Centralized SQL queries — no ORM, raw asyncpg."""

import asyncpg

from dashboard.api import database as db


async def get_trades(status: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
    async with db.db.pg_pool.acquire() as conn:
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
            """SELECT timestamp, open, high, low, close, volume
               FROM candles
               WHERE pair = $1 AND timeframe = $2
               ORDER BY timestamp DESC LIMIT $3""",
            pair, timeframe, count,
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
