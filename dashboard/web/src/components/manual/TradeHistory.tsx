"use client";

import { useState } from "react";
import { usePolling } from "@/lib/hooks";
import type { ManualTrade } from "@/lib/api";

function fmt(n: number | null | undefined, d: number = 2): string {
  if (n == null) return "--";
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

function formatDuration(from: string | null, to: string | null): string {
  if (!from || !to) return "--";
  const diff = new Date(to).getTime() - new Date(from).getTime();
  if (diff < 0) return "--";
  const mins = Math.floor(diff / 60000);
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ${mins % 60}m`;
  return `${Math.floor(hours / 24)}d`;
}

function TradeRow({ trade }: { trade: ManualTrade }) {
  const [expanded, setExpanded] = useState(false);
  const isWin = trade.result === "win";
  const isLoss = trade.result === "loss";
  const isLong = trade.direction === "long";

  return (
    <>
      <tr
        className="manual-history-row"
        onClick={() => setExpanded(!expanded)}
        style={{ cursor: "pointer" }}
      >
        <td>
          <span className={`badge-sm ${isLong ? "badge-long" : "badge-short"}`}>
            {trade.direction?.charAt(0).toUpperCase()}
          </span>
          {" "}{trade.pair}
        </td>
        <td>{fmt(trade.entry_price)}</td>
        <td>{fmt(trade.close_price)}</td>
        <td className={isWin ? "pnl-positive" : isLoss ? "pnl-negative" : ""}>
          {trade.pnl_usd != null ? (trade.pnl_usd >= 0 ? "+" : "") + "$" + fmt(trade.pnl_usd) : "--"}
        </td>
        <td className={isWin ? "pnl-positive" : isLoss ? "pnl-negative" : ""}>
          {trade.pnl_percent != null ? (trade.pnl_percent >= 0 ? "+" : "") + fmt(trade.pnl_percent * 100, 1) + "%" : "--"}
        </td>
        <td>{trade.r_multiple != null ? fmt(trade.r_multiple, 1) + "R" : "--"}</td>
        <td className="hide-mobile">
          {formatDuration(trade.activated_at || trade.created_at, trade.closed_at)}
        </td>
        <td>
          <span className={`result-badge ${isWin ? "result-win" : isLoss ? "result-loss" : "result-be"}`}>
            {trade.result?.toUpperCase() || "BE"}
          </span>
        </td>
      </tr>
      {expanded && (
        <tr className="manual-history-detail">
          <td colSpan={8}>
            <div className="manual-detail-grid">
              {trade.thesis && (
                <div className="manual-detail-item">
                  <span className="manual-detail-label">Thesis</span>
                  <span>{trade.thesis}</span>
                </div>
              )}
              {trade.mistakes && (
                <div className="manual-detail-item">
                  <span className="manual-detail-label">Mistakes</span>
                  <span style={{ color: "var(--short)" }}>{trade.mistakes}</span>
                </div>
              )}
              {trade.notes && (
                <div className="manual-detail-item">
                  <span className="manual-detail-label">Notes</span>
                  <span>{trade.notes}</span>
                </div>
              )}
              <div className="manual-detail-item">
                <span className="manual-detail-label">Setup</span>
                <span>{trade.setup_type || "--"} | {trade.timeframe || "--"} | {trade.leverage}x</span>
              </div>
              {trade.partial_closes && trade.partial_closes.length > 0 && (
                <div className="manual-detail-item">
                  <span className="manual-detail-label">Partials ({trade.partial_closes.length})</span>
                  <div>
                    {trade.partial_closes.map((pc) => (
                      <div key={pc.id} style={{ fontSize: 11, color: "var(--text-secondary)" }}>
                        {pc.close_pct}% @ {fmt(pc.close_price)}
                        {pc.pnl_usd != null && ` → $${fmt(pc.pnl_usd)}`}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

export function TradeHistory() {
  const { data: trades, loading } = usePolling<ManualTrade[]>(
    "/manual/trades?status=closed&limit=30", 15000
  );

  return (
    <div>
      <div className="card-title">Trade History</div>
      {loading && !trades ? (
        <div className="skeleton" style={{ height: 120, width: "100%" }} />
      ) : !trades || trades.length === 0 ? (
        <div style={{ color: "var(--text-muted)", textAlign: "center", padding: 24, fontSize: 13 }}>
          No closed trades yet
        </div>
      ) : (
        <div className="scroll-x">
          <table className="manual-history-table">
            <thead>
              <tr>
                <th>Pair</th>
                <th>Entry</th>
                <th>Exit</th>
                <th>P&L $</th>
                <th>P&L %</th>
                <th>R</th>
                <th className="hide-mobile">Duration</th>
                <th>Result</th>
              </tr>
            </thead>
            <tbody>
              {trades.map((t) => (
                <TradeRow key={t.id} trade={t} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
