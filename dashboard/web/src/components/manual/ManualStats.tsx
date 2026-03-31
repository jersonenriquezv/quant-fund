"use client";

import { usePolling } from "@/lib/hooks";
import type { ManualAnalyticsData, ManualTrade } from "@/lib/api";

function fmt(n: number | null | undefined, d: number = 2): string {
  if (n == null) return "--";
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

export function ManualStats() {
  const { data: analytics } = usePolling<ManualAnalyticsData>("/manual/analytics", 15000);
  const { data: active } = usePolling<ManualTrade[]>("/manual/trades?status=active&limit=50", 10000);

  const activeCount = active?.length ?? 0;
  const pnl = analytics?.total_pnl_usd ?? 0;
  const wr = analytics?.win_rate ?? 0; // Already a percentage (e.g. 50.0)
  const totalTrades = analytics?.total_trades ?? 0;
  const streak = analytics?.current_streak as { count: number; type: string } | null;
  const avgR = analytics?.avg_r_multiple;
  const pf = analytics?.profit_factor;

  return (
    <div>
      <div className="card-title">Manual Trading</div>
      <div className="manual-stats-grid">
        <div className="manual-stat">
          <span className="manual-stat-label">P&L (USDT)</span>
          <span className={`manual-stat-value ${pnl >= 0 ? "pnl-positive" : "pnl-negative"}`}>
            {pnl >= 0 ? "+" : ""}${fmt(pnl)}
          </span>
        </div>
        <div className="manual-stat">
          <span className="manual-stat-label">Win Rate</span>
          <span className="manual-stat-value">
            {totalTrades > 0 ? `${wr.toFixed(0)}%` : "--"}
          </span>
        </div>
        <div className="manual-stat">
          <span className="manual-stat-label">Trades</span>
          <span className="manual-stat-value">{totalTrades}</span>
        </div>
        <div className="manual-stat">
          <span className="manual-stat-label">Active</span>
          <span className="manual-stat-value">{activeCount}</span>
        </div>
        <div className="manual-stat">
          <span className="manual-stat-label">Avg R</span>
          <span className="manual-stat-value">
            {avgR != null ? `${avgR.toFixed(1)}R` : "--"}
          </span>
        </div>
        <div className="manual-stat">
          <span className="manual-stat-label">Streak</span>
          <span className={`manual-stat-value ${streak && streak.type === "win" ? "pnl-positive" : streak && streak.type === "loss" ? "pnl-negative" : ""}`}>
            {streak && streak.count > 0
              ? `${streak.count}${streak.type === "win" ? "W" : "L"}`
              : "--"}
          </span>
        </div>
      </div>
    </div>
  );
}
