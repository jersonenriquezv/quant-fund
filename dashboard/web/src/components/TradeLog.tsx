"use client";

import { usePolling } from "@/lib/hooks";
import type { TradeRecord } from "@/lib/api";

function fmt(n: number | null | undefined, d: number = 2): string {
  if (n == null) return "--";
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

function formatTime(ts: string | null): string {
  if (!ts) return "--";
  try {
    const d = new Date(ts);
    return d.toISOString().replace("T", " ").slice(5, 16);
  } catch {
    return "--";
  }
}

export function TradeLog() {
  const { data: trades, loading } = usePolling<TradeRecord[]>("/trades?limit=20", 10000);

  return (
    <div>
      <div className="card-title">Trade Log</div>
      <div className="scroll-y">
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Pair</th>
              <th>Dir</th>
              <th>Type</th>
              <th style={{ textAlign: "right" }}>Entry</th>
              <th style={{ textAlign: "right" }}>P&L</th>
              <th style={{ textAlign: "right" }}>P&L $</th>
              <th>Exit</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {loading && !trades && (
              <tr><td colSpan={9}><div className="skeleton" style={{ height: 16, width: "100%" }} /></td></tr>
            )}
            {trades?.map((t) => {
              const isLong = t.direction === "long";
              const pnlClass = (t.pnl_pct ?? 0) >= 0 ? "pnl-positive" : "pnl-negative";
              return (
                <tr key={t.id} className="animate-in">
                  <td style={{ color: "var(--text-muted)" }}>{formatTime(t.opened_at)}</td>
                  <td style={{ fontWeight: 600 }}>{t.pair}</td>
                  <td>
                    <span className={`badge ${isLong ? "badge-long" : "badge-short"}`}>
                      {t.direction}
                    </span>
                  </td>
                  <td style={{ color: "var(--text-muted)", fontSize: 11 }}>{t.setup_type}</td>
                  <td className="num">{fmt(t.actual_entry ?? t.entry_price)}</td>
                  <td className={`num ${pnlClass}`}>
                    {t.pnl_pct != null ? (t.pnl_pct >= 0 ? "+" : "") + (t.pnl_pct * 100).toFixed(2) + "%" : "--"}
                  </td>
                  <td className={`num ${pnlClass}`}>
                    {t.pnl_usd != null ? (t.pnl_usd >= 0 ? "+" : "") + fmt(t.pnl_usd) : "--"}
                  </td>
                  <td style={{ fontSize: 11 }}>{t.exit_reason ?? "--"}</td>
                  <td>
                    <span style={{
                      fontSize: 11, padding: "1px 6px", borderRadius: 3,
                      background: t.status === "open" ? "rgba(59,130,246,0.15)" : "var(--bg-secondary)",
                      color: t.status === "open" ? "var(--accent)" : "var(--text-muted)",
                    }}>
                      {t.status}
                    </span>
                  </td>
                </tr>
              );
            })}
            {trades?.length === 0 && (
              <tr><td colSpan={9} style={{ color: "var(--text-muted)", textAlign: "center", padding: 20 }}>No trades yet</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
