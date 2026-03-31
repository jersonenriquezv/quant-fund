"use client";

import { usePolling } from "@/lib/hooks";
import type { ManualAnalyticsData, ManualBalance, ManualTrade } from "@/lib/api";

function fmt(n: number | null | undefined, d: number = 2): string {
  if (n == null) return "--";
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

export function ManualStats() {
  const { data: analytics } = usePolling<ManualAnalyticsData>("/manual/analytics", 15000);
  const { data: balances } = usePolling<ManualBalance[]>("/manual/balances", 30000);
  const { data: active } = usePolling<ManualTrade[]>("/manual/trades?status=active&limit=50", 10000);

  const totalBalance = balances?.reduce((s, b) => s + b.balance, 0) ?? 0;
  const activeCount = active?.length ?? 0;
  const pnl = analytics?.total_pnl_usd ?? 0;
  const wr = analytics?.win_rate ?? 0;
  const totalTrades = analytics?.total_trades ?? 0;
  const streak = analytics?.current_streak ?? 0;

  return (
    <div>
      <div className="card-title">Manual Trading</div>
      <div className="manual-stats-grid">
        <div className="manual-stat">
          <span className="manual-stat-label">Balance</span>
          <span className="manual-stat-value">${fmt(totalBalance)}</span>
        </div>
        <div className="manual-stat">
          <span className="manual-stat-label">Total P&L</span>
          <span className={`manual-stat-value ${pnl >= 0 ? "pnl-positive" : "pnl-negative"}`}>
            {pnl >= 0 ? "+" : ""}${fmt(pnl)}
          </span>
        </div>
        <div className="manual-stat">
          <span className="manual-stat-label">Win Rate</span>
          <span className="manual-stat-value">
            {totalTrades > 0 ? `${(wr * 100).toFixed(0)}%` : "--"}
          </span>
        </div>
        <div className="manual-stat">
          <span className="manual-stat-label">Active</span>
          <span className="manual-stat-value">{activeCount}</span>
        </div>
        <div className="manual-stat">
          <span className="manual-stat-label">Trades</span>
          <span className="manual-stat-value">{totalTrades}</span>
        </div>
        <div className="manual-stat">
          <span className="manual-stat-label">Streak</span>
          <span className={`manual-stat-value ${streak > 0 ? "pnl-positive" : streak < 0 ? "pnl-negative" : ""}`}>
            {streak > 0 ? `+${streak}W` : streak < 0 ? `${streak}L` : "--"}
          </span>
        </div>
      </div>
    </div>
  );
}
