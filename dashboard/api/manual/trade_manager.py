"""CRUD operations for manual trades + partial closes + balance tracking."""

from datetime import datetime, timezone
from decimal import Decimal

from dashboard.api.manual.calculator import pnl_usd

import asyncpg


async def create_trade(pool: asyncpg.Pool, data: dict) -> dict:
    """Insert a new manual trade with status='planned'."""
    row = await pool.fetchrow(
        """
        INSERT INTO manual_trades (
            pair, direction, timeframe, setup_type, margin_type,
            entry_price, stop_loss, take_profit_1, take_profit_2,
            account_balance, risk_percent, risk_usd, position_size,
            position_value_usd, leverage, margin_used,
            sl_distance_pct, rr_ratio, rr_ratio_tp2,
            status, thesis, tags,
            spot_net_flow_4h, futures_net_flow_4h, cg_ls_ratio,
            cg_funding_rate, fees_trend_wow,
            tvl_delta_7d, upcoming_unlock_usd
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
            $11, $12, $13, $14, $15, $16, $17, $18, $19, 'planned', $20, $21,
            $22, $23, $24, $25, $26, $27, $28
        ) RETURNING *
        """,
        data["pair"], data["direction"], data.get("timeframe"),
        data.get("setup_type"), data.get("margin_type", "linear"),
        Decimal(str(data["entry_price"])), Decimal(str(data["stop_loss"])),
        Decimal(str(data["take_profit_1"])),
        Decimal(str(data["take_profit_2"])) if data.get("take_profit_2") else None,
        Decimal(str(data["account_balance"])), Decimal(str(data["risk_percent"])),
        Decimal(str(data["risk_usd"])), Decimal(str(data["position_size"])),
        Decimal(str(data["position_value_usd"])),
        data.get("leverage", 7),
        Decimal(str(data["margin_used"])),
        Decimal(str(data["sl_distance_pct"])),
        Decimal(str(data["rr_ratio"])),
        Decimal(str(data["rr_ratio_tp2"])) if data.get("rr_ratio_tp2") else None,
        data.get("thesis"), data.get("tags"),
        Decimal(str(data["spot_net_flow_4h"])) if data.get("spot_net_flow_4h") is not None else None,
        Decimal(str(data["futures_net_flow_4h"])) if data.get("futures_net_flow_4h") is not None else None,
        Decimal(str(data["cg_ls_ratio"])) if data.get("cg_ls_ratio") is not None else None,
        Decimal(str(data["cg_funding_rate"])) if data.get("cg_funding_rate") is not None else None,
        Decimal(str(data["fees_trend_wow"])) if data.get("fees_trend_wow") is not None else None,
        Decimal(str(data["tvl_delta_7d"])) if data.get("tvl_delta_7d") is not None else None,
        Decimal(str(data["upcoming_unlock_usd"])) if data.get("upcoming_unlock_usd") is not None else None,
    )
    return _row_to_dict(row)


async def get_trades(
    pool: asyncpg.Pool,
    status: str | None = None,
    pair: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """List trades with optional filters."""
    conditions = []
    params = []
    idx = 1

    if status:
        conditions.append(f"status = ${idx}")
        params.append(status)
        idx += 1
    if pair:
        conditions.append(f"pair = ${idx}")
        params.append(pair)
        idx += 1

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([limit, offset])

    rows = await pool.fetch(
        f"""
        SELECT t.*,
            COALESCE(pc.closed_pct, 0) AS partial_closed_pct,
            COALESCE(pc.partial_pnl, 0) AS partial_pnl_usd,
            COALESCE(pc.n_partials, 0) AS partial_count
        FROM manual_trades t
        LEFT JOIN LATERAL (
            SELECT SUM(percentage) AS closed_pct,
                   SUM(pnl_usd) AS partial_pnl,
                   COUNT(*) AS n_partials
            FROM manual_partial_closes WHERE trade_id = t.id
        ) pc ON TRUE
        {where}
        ORDER BY t.created_at DESC
        LIMIT ${idx} OFFSET ${idx + 1}
        """,
        *params,
    )
    return [_row_to_dict(r) for r in rows]


async def get_trade(pool: asyncpg.Pool, trade_id: int) -> dict | None:
    """Get a single trade by ID."""
    row = await pool.fetchrow("SELECT * FROM manual_trades WHERE id = $1", trade_id)
    if row is None:
        return None
    return _row_to_dict(row)


async def update_trade(pool: asyncpg.Pool, trade_id: int, data: dict) -> dict | None:
    """Update trade fields. Handles status transitions and PnL auto-calc."""
    current = await pool.fetchrow("SELECT * FROM manual_trades WHERE id = $1", trade_id)
    if current is None:
        return None

    updates = {}
    for key in ("thesis", "fundamental_notes", "mistakes", "screenshots", "tags",
                "timeframe", "setup_type"):
        if key in data:
            updates[key] = data[key]

    # Price fields (editable from frontend)
    for key in ("entry_price", "stop_loss", "take_profit_1", "take_profit_2"):
        if key in data:
            updates[key] = Decimal(str(data[key])) if data[key] is not None else None

    # Decimal fields
    for key in ("spot_net_flow_4h", "futures_net_flow_4h", "cg_ls_ratio",
                "cg_funding_rate", "fees_trend_wow", "tvl_delta_7d", "upcoming_unlock_usd"):
        if key in data:
            updates[key] = Decimal(str(data[key])) if data[key] is not None else None

    # Allow manual datetime overrides (ISO format strings)
    for dt_key in ("created_at", "activated_at", "closed_at"):
        if dt_key in data and data[dt_key] is not None:
            updates[dt_key] = datetime.fromisoformat(data[dt_key])

    new_status = data.get("status")
    if new_status:
        updates["status"] = new_status
        if new_status == "active" and current["status"] == "planned":
            if "activated_at" not in updates:
                updates["activated_at"] = datetime.now(timezone.utc)
        elif new_status == "closed":
            if "closed_at" not in updates:
                updates["closed_at"] = datetime.now(timezone.utc)
            # Calculate PnL from close_price or direct pnl_usd
            entry = float(current["entry_price"])
            direction = current["direction"]
            risk_usd = float(current["risk_usd"])
            size = float(current["position_size"])

            margin_type = current["margin_type"] or "linear"

            if "close_price" in data:
                close_price = float(data["close_price"])
                updates["close_price"] = Decimal(str(close_price))
                pnl = pnl_usd(margin_type, direction, entry, close_price, size)
                updates["pnl_usd"] = Decimal(str(round(pnl, 2)))
                updates["pnl_percent"] = Decimal(str(round((pnl / float(current["account_balance"])) * 100, 4)))
                updates["r_multiple"] = Decimal(str(round(pnl / risk_usd, 2))) if risk_usd > 0 else Decimal("0")
            elif "pnl_usd" in data:
                pnl = float(data["pnl_usd"])
                updates["pnl_usd"] = Decimal(str(pnl))
                updates["pnl_percent"] = Decimal(str(round((pnl / float(current["account_balance"])) * 100, 4)))
                updates["r_multiple"] = Decimal(str(round(pnl / risk_usd, 2))) if risk_usd > 0 else Decimal("0")
                # Back-calculate close price
                if size > 0:
                    if margin_type == "inverse":
                        # pnl = contracts × (close - entry) / entry → close = entry + pnl × entry / contracts
                        delta = pnl * entry / size
                        cp = entry + delta if direction == "long" else entry - delta
                    else:
                        cp = entry + pnl / size if direction == "long" else entry - pnl / size
                    updates["close_price"] = Decimal(str(round(cp, 8)))

            # Auto-determine result
            result = data.get("result")
            if not result:
                pnl_val = float(updates.get("pnl_usd", 0))
                if pnl_val > 0:
                    result = "win"
                elif pnl_val < 0:
                    result = "loss"
                else:
                    result = "breakeven"
            updates["result"] = result

            # Auto-update balance for this pair
            pnl_final = float(updates.get("pnl_usd", 0))
            if pnl_final != 0:
                await _update_balance(pool, current["pair"], pnl_final)

    if "result" in data and "status" not in data:
        updates["result"] = data["result"]

    if not updates:
        return _row_to_dict(current)

    updates["updated_at"] = datetime.now(timezone.utc)

    set_clauses = []
    params = []
    for i, (k, v) in enumerate(updates.items(), 1):
        set_clauses.append(f"{k} = ${i}")
        params.append(v)
    params.append(trade_id)

    row = await pool.fetchrow(
        f"UPDATE manual_trades SET {', '.join(set_clauses)} WHERE id = ${len(params)} RETURNING *",
        *params,
    )
    return _row_to_dict(row)


async def delete_trade(pool: asyncpg.Pool, trade_id: int) -> bool:
    """Hard delete a trade and its partial closes."""
    await pool.execute("DELETE FROM manual_partial_closes WHERE trade_id = $1", trade_id)
    result = await pool.execute("DELETE FROM manual_trades WHERE id = $1", trade_id)
    return result == "DELETE 1"


async def partial_close(pool: asyncpg.Pool, trade_id: int, data: dict) -> dict:
    """Record a partial close for a trade.

    Auto-updates trade status if total closed >= 100%.
    Auto-updates pair balance with PnL.
    """
    trade = await pool.fetchrow("SELECT * FROM manual_trades WHERE id = $1", trade_id)
    if trade is None:
        raise ValueError(f"Trade {trade_id} not found")
    if trade["status"] != "active":
        raise ValueError(f"Trade must be active to partial close (current: {trade['status']})")

    entry = float(trade["entry_price"])
    direction = trade["direction"]
    risk_usd = float(trade["risk_usd"])
    total_size = float(trade["position_size"])

    margin_type = trade["margin_type"] or "linear"
    close_price = float(data["close_price"])
    percentage = float(data.get("percentage", 50.0))
    size_closed = total_size * (percentage / 100)

    pnl = pnl_usd(margin_type, direction, entry, close_price, size_closed)

    r_mult = round(pnl / (risk_usd * percentage / 100), 2) if risk_usd > 0 else 0.0

    partial = await pool.fetchrow(
        """
        INSERT INTO manual_partial_closes (
            trade_id, close_price, percentage, position_size_closed,
            pnl_usd, r_multiple, notes
        ) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *
        """,
        trade_id, Decimal(str(close_price)), Decimal(str(percentage)),
        Decimal(str(round(size_closed, 8))),
        Decimal(str(round(pnl, 2))), Decimal(str(r_mult)),
        data.get("notes"),
    )

    # Update balance
    if pnl != 0:
        await _update_balance(pool, trade["pair"], pnl)

    # Check total closed percentage
    total_pct = await pool.fetchval(
        "SELECT COALESCE(SUM(percentage), 0) FROM manual_partial_closes WHERE trade_id = $1",
        trade_id,
    )

    result = {
        "partial_close": _row_to_dict(partial),
        "total_closed_pct": float(total_pct),
    }

    if float(total_pct) >= 100.0:
        # Close the trade — sum all partial PnLs
        total_pnl = await pool.fetchval(
            "SELECT COALESCE(SUM(pnl_usd), 0) FROM manual_partial_closes WHERE trade_id = $1",
            trade_id,
        )
        total_pnl_f = float(total_pnl)
        trade_result = "win" if total_pnl_f > 0 else ("loss" if total_pnl_f < 0 else "breakeven")
        r_total = round(total_pnl_f / risk_usd, 2) if risk_usd > 0 else 0.0

        await pool.execute(
            """
            UPDATE manual_trades
            SET status = 'closed', closed_at = NOW(), result = $2,
                pnl_usd = $3, r_multiple = $4,
                pnl_percent = $5, updated_at = NOW()
            WHERE id = $1
            """,
            trade_id, trade_result,
            Decimal(str(round(total_pnl_f, 2))),
            Decimal(str(r_total)),
            Decimal(str(round((total_pnl_f / float(trade["account_balance"])) * 100, 4))),
        )
        result["trade_closed"] = True
        result["total_pnl_usd"] = round(total_pnl_f, 2)
        result["result"] = trade_result
    else:
        remaining = 100.0 - float(total_pct)
        result["trade_closed"] = False
        result["remaining_pct"] = remaining
        # Remind about SL to breakeven after first partial
        if float(total_pct) <= 50.0:
            result["reminder"] = f"SL moved to breakeven at {entry}"

    return result


async def get_partial_closes(pool: asyncpg.Pool, trade_id: int) -> list[dict]:
    """Get all partial closes for a trade."""
    rows = await pool.fetch(
        "SELECT * FROM manual_partial_closes WHERE trade_id = $1 ORDER BY closed_at",
        trade_id,
    )
    return [_row_to_dict(r) for r in rows]


# --- Balance management ---

async def get_balances(pool: asyncpg.Pool) -> list[dict]:
    rows = await pool.fetch("SELECT * FROM manual_balances ORDER BY pair")
    return [_row_to_dict(r) for r in rows]


async def set_balance(pool: asyncpg.Pool, pair: str, balance: float) -> dict:
    row = await pool.fetchrow(
        """
        INSERT INTO manual_balances (pair, balance, updated_at)
        VALUES ($1, $2, NOW())
        ON CONFLICT (pair) DO UPDATE SET balance = $2, updated_at = NOW()
        RETURNING *
        """,
        pair, Decimal(str(balance)),
    )
    return _row_to_dict(row)


async def _update_balance(pool: asyncpg.Pool, pair: str, pnl: float) -> None:
    """Auto-update balance for a pair after trade PnL."""
    existing = await pool.fetchval(
        "SELECT balance FROM manual_balances WHERE pair = $1", pair,
    )
    if existing is not None:
        new_balance = float(existing) + pnl
        await pool.execute(
            "UPDATE manual_balances SET balance = $1, updated_at = NOW() WHERE pair = $2",
            Decimal(str(round(new_balance, 2))), pair,
        )


def _row_to_dict(row: asyncpg.Record) -> dict:
    """Convert asyncpg Record to dict with JSON-safe types."""
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, Decimal):
            d[k] = float(v)
        elif isinstance(v, datetime):
            d[k] = v.isoformat()
    return d
