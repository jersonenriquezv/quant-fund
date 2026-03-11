"use client";

import { usePolling } from "@/lib/hooks";
import type { TradeRecord } from "@/lib/api";

function timeAgo(ts: string | null): string {
  if (!ts) return "--";
  try {
    const diff = Date.now() - new Date(ts).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return `${days}d ago`;
  } catch {
    return "--";
  }
}

function fmt(n: number | null | undefined, d: number = 2): string {
  if (n == null) return "--";
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

function exitLabel(reason: string | null): string {
  if (!reason) return "";
  const map: Record<string, string> = {
    tp: "TP Hit",
    sl: "SL Hit",
    breakeven_sl: "Breakeven SL",
    trailing_sl: "Trailing SL",
    timeout: "Timeout",
    invalidation: "Invalidated",
    emergency: "Emergency",
    orphaned_restart: "Orphaned",
    sl_too_close: "SL Too Close",
    excessive_slippage: "Slippage",
    cancelled: "Cancelled",
    manual_close: "Manual",
  };
  return map[reason] ?? reason;
}

export function RecentTrades() {
  const { data: trades, loading } = usePolling<TradeRecord[]>("/trades?status=closed&limit=5", 15000);

  return (
    <div>
      <div className="card-title">Recent Trades</div>
      {loading && !trades ? (
        <div className="skeleton" style={{ height: 80, width: "100%" }} />
      ) : !trades || trades.length === 0 ? (
        <div style={{ color: "var(--text-muted)", textAlign: "center", padding: 24, fontSize: 13 }}>
          No closed trades yet
        </div>
      ) : (
        <div className="recent-trades-list">
          {trades.map((t) => {
            const isLong = t.direction === "long";
            const pnl = t.pnl_pct ?? 0;
            const isWin = pnl >= 0;

            return (
              <div key={t.id} className={`recent-trade-card ${isWin ? "trade-win" : "trade-loss"}`}>
                <div className="recent-trade-header">
                  <div className="recent-trade-pair">
                    <span className={`badge ${isLong ? "badge-long" : "badge-short"}`}>
                      {t.direction?.toUpperCase()}
                    </span>
                    <span className="recent-trade-symbol">{t.pair}</span>
                  </div>
                  <span className={`recent-trade-pnl ${isWin ? "pnl-positive" : "pnl-negative"}`}>
                    {isWin ? "+" : ""}{(pnl * 100).toFixed(2)}%
                  </span>
                </div>

                <div className="recent-trade-details">
                  <div className="recent-trade-detail">
                    <span className="recent-trade-label">Entry</span>
                    <span className="recent-trade-value">{fmt(t.actual_entry ?? t.entry_price)}</span>
                  </div>
                  <div className="recent-trade-detail">
                    <span className="recent-trade-label">P&L</span>
                    <span className={`recent-trade-value ${isWin ? "pnl-positive" : "pnl-negative"}`}>
                      {t.pnl_usd != null ? (t.pnl_usd >= 0 ? "+" : "") + "$" + fmt(t.pnl_usd) : "--"}
                    </span>
                  </div>
                  <div className="recent-trade-detail">
                    <span className="recent-trade-label">Exit</span>
                    <span className="recent-trade-value">{exitLabel(t.exit_reason)}</span>
                  </div>
                  <div className="recent-trade-detail">
                    <span className="recent-trade-label">AI</span>
                    <span className="recent-trade-value">
                      {t.ai_confidence != null ? (t.ai_confidence * 100).toFixed(0) + "%" : "--"}
                    </span>
                  </div>
                </div>

                <div className="recent-trade-footer">
                  <span className="recent-trade-type">{t.setup_type}</span>
                  <span className="recent-trade-time">{timeAgo(t.closed_at ?? t.opened_at)}</span>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
