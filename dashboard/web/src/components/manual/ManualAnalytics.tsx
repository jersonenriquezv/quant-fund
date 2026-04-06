"use client";

import { usePolling } from "@/lib/hooks";
import type { ManualAnalyticsData } from "@/lib/api";

function fmt(n: number | null | undefined, d: number = 2): string {
  if (n == null) return "--";
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

function PairBreakdown({ data }: { data: Record<string, { count: number; pnl_usd: number; win_rate: number; avg_r: number }> }) {
  const entries = Object.entries(data).sort((a, b) => b[1].pnl_usd - a[1].pnl_usd);
  if (entries.length === 0) return <span style={{ color: "var(--text-muted)" }}>--</span>;

  return (
    <div className="manual-breakdown">
      {entries.map(([key, val]) => (
        <div key={key} className="manual-breakdown-row">
          <span className="manual-breakdown-label">{key}</span>
          <span style={{ fontSize: 11 }}>{val.count}t</span>
          <span style={{ fontSize: 11 }}>{val.win_rate.toFixed(0)}%</span>
          <span className={`manual-breakdown-pnl ${val.pnl_usd >= 0 ? "pnl-positive" : "pnl-negative"}`}>
            {val.pnl_usd >= 0 ? "+" : ""}${fmt(val.pnl_usd)}
          </span>
        </div>
      ))}
    </div>
  );
}

export function ManualAnalytics() {
  const { data: analytics, loading } = usePolling<ManualAnalyticsData>("/manual/analytics?days=30", 30000);

  if (loading && !analytics) {
    return (
      <div>
        <div className="card-title">Analytics (30d)</div>
        <div className="skeleton" style={{ height: 100, width: "100%" }} />
      </div>
    );
  }

  if (!analytics || analytics.total_trades === 0) {
    return (
      <div>
        <div className="card-title">Analytics (30d)</div>
        <div style={{ color: "var(--text-muted)", textAlign: "center", padding: 24, fontSize: 13 }}>
          No closed trades to analyze
        </div>
      </div>
    );
  }

  return (
    <div>
      <div className="card-title">Analytics (30d)</div>
      <div className="manual-analytics-grid">
        <div className="manual-analytics-stat">
          <span className="manual-stat-label">Win Rate</span>
          <span className="manual-analytics-big">
            {analytics.win_rate.toFixed(0)}%
          </span>
          <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
            {analytics.wins}W / {analytics.losses}L
          </span>
        </div>
        <div className="manual-analytics-stat">
          <span className="manual-stat-label">Avg R</span>
          <span className="manual-analytics-big">
            {analytics.avg_r_multiple != null ? fmt(analytics.avg_r_multiple, 1) + "R" : "--"}
          </span>
        </div>
        <div className="manual-analytics-stat">
          <span className="manual-stat-label">Profit Factor</span>
          <span className="manual-analytics-big">
            {analytics.profit_factor != null ? fmt(analytics.profit_factor, 1) : "--"}
          </span>
        </div>
        <div className="manual-analytics-stat">
          <span className="manual-stat-label">TP1 Hit</span>
          <span className="manual-analytics-big">
            {analytics.tp1_hit_rate != null ? analytics.tp1_hit_rate.toFixed(0) + "%" : "--"}
          </span>
        </div>

        {analytics.best_trade && (
          <div className="manual-analytics-stat">
            <span className="manual-stat-label">Best Trade</span>
            <span className="pnl-positive" style={{ fontSize: 13 }}>
              {analytics.best_trade.r_multiple}R ({analytics.best_trade.pair})
            </span>
          </div>
        )}
        {analytics.worst_trade && (
          <div className="manual-analytics-stat">
            <span className="manual-stat-label">Worst Trade</span>
            <span className="pnl-negative" style={{ fontSize: 13 }}>
              {analytics.worst_trade.r_multiple}R ({analytics.worst_trade.pair})
            </span>
          </div>
        )}
      </div>

      <div style={{ marginTop: 16 }}>
        <div className="card-title" style={{ marginBottom: 8 }}>By Pair</div>
        <PairBreakdown data={analytics.trades_by_pair} />
      </div>

      <div style={{ marginTop: 12 }}>
        <div className="card-title" style={{ marginBottom: 8 }}>By Direction</div>
        <PairBreakdown data={analytics.trades_by_direction} />
      </div>
    </div>
  );
}
