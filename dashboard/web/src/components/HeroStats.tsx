"use client";

import { usePolling } from "@/lib/hooks";
import type { StatsData } from "@/lib/api";

function fmt(n: number, d: number = 2): string {
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

export function HeroStats() {
  const { data: stats } = usePolling<StatsData>("/stats", 10000);

  if (!stats) {
    return (
      <div className="hero-stats">
        <div className="hero-stat">
          <div className="hero-stat-label">Total P&L</div>
          <div className="skeleton" style={{ height: 28, width: 100 }} />
        </div>
        <div className="hero-stat">
          <div className="hero-stat-label">Win Rate</div>
          <div className="skeleton" style={{ height: 28, width: 80 }} />
        </div>
        <div className="hero-stat">
          <div className="hero-stat-label">Profit Factor</div>
          <div className="skeleton" style={{ height: 28, width: 60 }} />
        </div>
      </div>
    );
  }

  const pnlPositive = stats.total_pnl_usd >= 0;
  const pnlClass = pnlPositive ? "hero-value-positive" : "hero-value-negative";
  const wrGood = stats.win_rate >= 45;
  const pfGood = stats.profit_factor >= 1.5;

  return (
    <div className="hero-stats">
      <div className="hero-stat">
        <div className="hero-stat-label">Total P&L</div>
        <div className={`hero-value ${pnlClass}`}>
          {pnlPositive ? "+" : ""}${fmt(stats.total_pnl_usd)}
        </div>
      </div>
      <div className="hero-stat">
        <div className="hero-stat-label">Win Rate</div>
        <div className={`hero-value ${wrGood ? "hero-value-positive" : "hero-value-negative"}`}>
          {fmt(stats.win_rate, 1)}%
        </div>
      </div>
      <div className="hero-stat">
        <div className="hero-stat-label">Profit Factor</div>
        <div className={`hero-value ${pfGood ? "hero-value-positive" : "hero-value-negative"}`}>
          {fmt(stats.profit_factor, 2)}
        </div>
      </div>
      <div className="hero-stat-secondary">
        <span><span className="hero-stat-label">Trades</span> {stats.total_trades}</span>
        <span><span className="hero-stat-label">Avg R:R</span> {fmt(stats.avg_rr, 2)}</span>
      </div>
    </div>
  );
}
