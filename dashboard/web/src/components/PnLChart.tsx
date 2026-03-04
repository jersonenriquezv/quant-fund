"use client";

import { usePolling } from "@/lib/hooks";
import type { TradeRecord, StatsData } from "@/lib/api";

function fmt(n: number, d: number = 2): string {
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

function Sparkline({ trades }: { trades: TradeRecord[] }) {
  // Build cumulative PnL from oldest to newest
  const closed = trades
    .filter((t) => t.status === "closed" && t.pnl_usd != null)
    .reverse();

  if (closed.length < 2) {
    return (
      <div style={{ color: "var(--text-muted)", fontSize: 12, textAlign: "center", padding: "10px 0" }}>
        Need 2+ closed trades for chart
      </div>
    );
  }

  let cumulative = 0;
  const points = closed.map((t) => {
    cumulative += t.pnl_usd!;
    return cumulative;
  });

  const min = Math.min(0, ...points);
  const max = Math.max(0, ...points);
  const range = max - min || 1;

  const w = 280;
  const h = 80;
  const padding = 4;

  const coords = points.map((v, i) => {
    const x = padding + (i / (points.length - 1)) * (w - 2 * padding);
    const y = h - padding - ((v - min) / range) * (h - 2 * padding);
    return `${x},${y}`;
  });

  const zeroY = h - padding - ((0 - min) / range) * (h - 2 * padding);
  const lastVal = points[points.length - 1];
  const color = lastVal >= 0 ? "var(--long)" : "var(--short)";

  return (
    <svg viewBox={`0 0 ${w} ${h}`} style={{ width: "100%", height: 80 }}>
      {/* Zero line */}
      <line x1={padding} y1={zeroY} x2={w - padding} y2={zeroY} stroke="var(--border)" strokeWidth="0.5" strokeDasharray="3,3" />
      {/* Line */}
      <polyline
        points={coords.join(" ")}
        fill="none"
        stroke={color}
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function PnLChart() {
  const { data: trades } = usePolling<TradeRecord[]>("/trades?limit=50", 10000);
  const { data: stats } = usePolling<StatsData>("/stats", 10000);

  return (
    <div>
      <div className="card-title">Equity Curve</div>

      <Sparkline trades={trades ?? []} />

      {stats && (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "6px 12px", fontSize: 12, marginTop: 8 }}>
          <div>
            <span style={{ color: "var(--text-muted)" }}>Total P&L</span>
            <div className={`num ${stats.total_pnl_usd >= 0 ? "pnl-positive" : "pnl-negative"}`}>
              ${fmt(stats.total_pnl_usd)}
            </div>
          </div>
          <div>
            <span style={{ color: "var(--text-muted)" }}>Win Rate</span>
            <div className="num">{fmt(stats.win_rate, 1)}%</div>
          </div>
          <div>
            <span style={{ color: "var(--text-muted)" }}>Trades</span>
            <div className="num">{stats.total_trades}</div>
          </div>
          <div>
            <span style={{ color: "var(--text-muted)" }}>Profit Factor</span>
            <div className="num">{fmt(stats.profit_factor, 2)}</div>
          </div>
        </div>
      )}
    </div>
  );
}
