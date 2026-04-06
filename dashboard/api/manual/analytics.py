"""Analytics for manual trades — win rate, avg R, PnL, TP hit rates."""

from decimal import Decimal
from datetime import datetime

import asyncpg


async def get_analytics(
    pool: asyncpg.Pool,
    days: int = 30,
    pair: str | None = None,
) -> dict:
    """Compute analytics for closed manual trades."""
    pair_filter = "AND pair = $2" if pair else ""
    params: list = [days]
    if pair:
        params.append(pair)

    # All closed trades in period
    rows = await pool.fetch(
        f"""
        SELECT id, pair, direction, setup_type, result, pnl_usd, r_multiple,
               rr_ratio, rr_ratio_tp2, risk_usd, closed_at
        FROM manual_trades
        WHERE status = 'closed'
          AND closed_at > NOW() - make_interval(days => $1)
          {pair_filter}
        ORDER BY closed_at
        """,
        *params,
    )

    if not rows:
        return _empty_analytics()

    trades = [dict(r) for r in rows]

    # Basic counts
    total = len(trades)
    wins = sum(1 for t in trades if t["result"] == "win")
    losses = sum(1 for t in trades if t["result"] == "loss")
    breakeven = sum(1 for t in trades if t["result"] == "breakeven")
    cancelled = sum(1 for t in trades if t["result"] == "cancelled")

    decided = total - cancelled
    win_rate = round((wins / decided) * 100, 1) if decided > 0 else 0.0

    # R and PnL
    r_multiples = [_f(t["r_multiple"]) for t in trades if t["r_multiple"] is not None]
    avg_r = round(sum(r_multiples) / len(r_multiples), 2) if r_multiples else 0.0
    total_pnl = round(sum(_f(t["pnl_usd"]) for t in trades if t["pnl_usd"] is not None), 2)

    planned_rrs = [_f(t["rr_ratio"]) for t in trades if t["rr_ratio"] is not None]
    avg_rr_planned = round(sum(planned_rrs) / len(planned_rrs), 2) if planned_rrs else 0.0

    # Best/worst
    best = max(trades, key=lambda t: _f(t["r_multiple"]) if t["r_multiple"] else -999)
    worst = min(trades, key=lambda t: _f(t["r_multiple"]) if t["r_multiple"] else 999)

    # Streak
    streak_count, streak_type = _current_streak(trades)

    # TP hit rates from partial closes
    tp_stats = await _tp_hit_rates(pool, days, pair)

    # Breakdowns
    by_pair = _breakdown_by(trades, "pair")
    by_setup = _breakdown_by(trades, "setup_type")
    by_direction = _breakdown_by(trades, "direction")

    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "cancelled": cancelled,
        "win_rate": win_rate,
        "avg_rr_planned": avg_rr_planned,
        "avg_r_multiple": avg_r,
        "total_pnl_usd": total_pnl,
        "best_trade": {"id": best["id"], "r_multiple": _f(best["r_multiple"]), "pair": best["pair"]},
        "worst_trade": {"id": worst["id"], "r_multiple": _f(worst["r_multiple"]), "pair": worst["pair"]},
        "current_streak": {"count": streak_count, "type": streak_type},
        "tp1_hit_rate": tp_stats["tp1_hit_rate"],
        "tp2_hit_rate": tp_stats["tp2_hit_rate"],
        "breakeven_rate": tp_stats["breakeven_rate"],
        "trades_by_pair": by_pair,
        "trades_by_setup": by_setup,
        "trades_by_direction": by_direction,
    }


async def _tp_hit_rates(pool: asyncpg.Pool, days: int, pair: str | None) -> dict:
    """Calculate TP1/TP2 hit rates from partial closes."""
    pair_filter = "AND t.pair = $2" if pair else ""
    params: list = [days]
    if pair:
        params.append(pair)

    rows = await pool.fetch(
        f"""
        SELECT t.id, COUNT(pc.id) AS partial_count,
               SUM(pc.percentage) AS total_closed_pct
        FROM manual_trades t
        LEFT JOIN manual_partial_closes pc ON pc.trade_id = t.id
        WHERE t.status = 'closed'
          AND t.closed_at > NOW() - make_interval(days => $1)
          {pair_filter}
        GROUP BY t.id
        """,
        *params,
    )

    total = len(rows)
    if total == 0:
        return {"tp1_hit_rate": 0.0, "tp2_hit_rate": 0.0, "breakeven_rate": 0.0}

    tp1_hits = sum(1 for r in rows if r["partial_count"] and r["partial_count"] >= 1)
    tp2_hits = sum(1 for r in rows if r["partial_count"] and r["partial_count"] >= 2)

    # Breakeven = hit TP1 but ended as breakeven (SL moved to entry, then stopped)
    be_rows = await pool.fetch(
        f"""
        SELECT COUNT(*) AS cnt FROM manual_trades t
        WHERE t.status = 'closed' AND t.result = 'breakeven'
          AND t.closed_at > NOW() - make_interval(days => $1)
          {pair_filter}
          AND EXISTS (SELECT 1 FROM manual_partial_closes pc WHERE pc.trade_id = t.id)
        """,
        *params,
    )
    be_count = be_rows[0]["cnt"] if be_rows else 0

    return {
        "tp1_hit_rate": round((tp1_hits / total) * 100, 1) if total > 0 else 0.0,
        "tp2_hit_rate": round((tp2_hits / total) * 100, 1) if total > 0 else 0.0,
        "breakeven_rate": round((be_count / total) * 100, 1) if total > 0 else 0.0,
    }


def _breakdown_by(trades: list[dict], key: str) -> dict:
    """Group trades by a field and compute per-group stats."""
    groups: dict[str, list] = {}
    for t in trades:
        val = t.get(key) or "unknown"
        groups.setdefault(val, []).append(t)

    result = {}
    for group_name, group_trades in groups.items():
        total = len(group_trades)
        wins = sum(1 for t in group_trades if t["result"] == "win")
        cancelled = sum(1 for t in group_trades if t["result"] == "cancelled")
        decided = total - cancelled
        pnl = sum(_f(t["pnl_usd"]) for t in group_trades if t["pnl_usd"] is not None)
        rs = [_f(t["r_multiple"]) for t in group_trades if t["r_multiple"] is not None]
        result[group_name] = {
            "count": total,
            "win_rate": round((wins / decided) * 100, 1) if decided > 0 else 0.0,
            "pnl_usd": round(pnl, 2),
            "avg_r": round(sum(rs) / len(rs), 2) if rs else 0.0,
        }
    return result


def _current_streak(trades: list[dict]) -> tuple[int, str]:
    """Calculate current consecutive win/loss streak."""
    relevant = [t for t in trades if t["result"] in ("win", "loss")]
    if not relevant:
        return 0, "none"

    # Trades are ordered by closed_at ASC, check from end
    last_result = relevant[-1]["result"]
    count = 0
    for t in reversed(relevant):
        if t["result"] == last_result:
            count += 1
        else:
            break
    return count, last_result


def _empty_analytics() -> dict:
    return {
        "total_trades": 0, "wins": 0, "losses": 0, "breakeven": 0,
        "cancelled": 0, "win_rate": 0.0, "avg_rr_planned": 0.0,
        "avg_r_multiple": 0.0, "total_pnl_usd": 0.0,
        "best_trade": None, "worst_trade": None,
        "current_streak": {"count": 0, "type": "none"},
        "tp1_hit_rate": 0.0, "tp2_hit_rate": 0.0, "breakeven_rate": 0.0,
        "trades_by_pair": {}, "trades_by_setup": {}, "trades_by_direction": {},
    }


def _f(val) -> float:
    """Safely convert Decimal/None to float."""
    if val is None:
        return 0.0
    return float(val)
