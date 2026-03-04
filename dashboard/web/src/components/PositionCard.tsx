"use client";

import type { WSMessage, PositionData } from "@/lib/api";

function fmt(n: number | null | undefined, d: number = 2): string {
  if (n == null) return "--";
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

function SinglePosition({ pos }: { pos: PositionData }) {
  const isLong = pos.direction === "long";
  const pnlClass = pos.pnl_pct >= 0 ? "pnl-positive" : "pnl-negative";

  return (
    <div className="animate-in" style={{
      background: "var(--bg-secondary)", borderRadius: 4, padding: 12,
      border: `1px solid ${isLong ? "rgba(16,185,129,0.2)" : "rgba(239,68,68,0.2)"}`,
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span style={{ fontWeight: 700 }}>{pos.pair}</span>
          <span className={`badge ${isLong ? "badge-long" : "badge-short"}`}>{pos.direction}</span>
          <span style={{ fontSize: 11, color: "var(--text-muted)" }}>{pos.phase}</span>
        </div>
        <span className={pnlClass} style={{ fontWeight: 700, fontSize: 16, fontVariantNumeric: "tabular-nums" }}>
          {pos.pnl_pct >= 0 ? "+" : ""}{(pos.pnl_pct * 100).toFixed(2)}%
        </span>
      </div>

      <div className="position-grid" style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 8, fontSize: 12 }}>
        <div>
          <div style={{ color: "var(--text-muted)", fontSize: 10 }}>ENTRY</div>
          <div className="num">{fmt(pos.actual_entry_price ?? pos.entry_price)}</div>
        </div>
        <div>
          <div style={{ color: "var(--text-muted)", fontSize: 10 }}>SL</div>
          <div className="num" style={{ color: "var(--short)" }}>{fmt(pos.sl_price)}</div>
        </div>
        <div>
          <div style={{ color: "var(--text-muted)", fontSize: 10 }}>TP1</div>
          <div className="num" style={{ color: "var(--long)" }}>{fmt(pos.tp1_price)}</div>
        </div>
        <div>
          <div style={{ color: "var(--text-muted)", fontSize: 10 }}>SIZE</div>
          <div className="num">{fmt(pos.filled_size, 4)}</div>
        </div>
      </div>
    </div>
  );
}

export function PositionCard({ ws }: { ws: WSMessage | null }) {
  const positions = ws?.positions ?? [];

  return (
    <div>
      <div className="card-title">Open Positions</div>
      {positions.length === 0 ? (
        <div style={{ color: "var(--text-muted)", fontSize: 13, padding: "20px 0", textAlign: "center" }}>
          No open positions
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {positions.map((p, i) => <SinglePosition key={`${p.pair}-${i}`} pos={p} />)}
        </div>
      )}
    </div>
  );
}
