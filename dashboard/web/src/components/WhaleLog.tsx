"use client";

import { usePolling } from "@/lib/hooks";
import type { WhaleMovement } from "@/lib/api";

function formatTime(ts: number): string {
  try {
    const d = new Date(ts);
    return d.toISOString().replace("T", " ").slice(5, 16);
  } catch {
    return "--";
  }
}

function truncateAddr(addr: string): string {
  if (addr.length <= 12) return addr;
  return addr.slice(0, 6) + "..." + addr.slice(-4);
}

function formatAmount(n: number, chain: string): string {
  const digits = chain === "BTC" ? 4 : 2;
  return n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function WhaleLog() {
  const { data: whales, loading } = usePolling<WhaleMovement[]>("/whales?hours=24", 30000);

  return (
    <div>
      <div className="card-title">Whale Movements (24h)</div>
      <div className="scroll-y">
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Wallet</th>
              <th>Action</th>
              <th style={{ textAlign: "right" }}>Amount</th>
              <th>Exchange</th>
              <th>Significance</th>
            </tr>
          </thead>
          <tbody>
            {loading && !whales && (
              <tr><td colSpan={6}><div className="skeleton" style={{ height: 16, width: "100%" }} /></td></tr>
            )}
            {whales?.map((w, i) => {
              const actionConfig: Record<string, { badge: string; label: string }> = {
                exchange_deposit: { badge: "badge-short", label: "deposit" },
                exchange_withdrawal: { badge: "badge-long", label: "withdrawal" },
                transfer_out: { badge: "badge-neutral", label: "transfer out" },
                transfer_in: { badge: "badge-neutral", label: "transfer in" },
              };
              const { badge, label } = actionConfig[w.action] ?? { badge: "badge-neutral", label: w.action };
              return (
                <tr key={`${w.timestamp}-${w.wallet}-${i}`} className="animate-in">
                  <td style={{ color: "var(--text-muted)" }}>{formatTime(w.timestamp)}</td>
                  <td title={w.wallet}>
                    <span style={{ fontWeight: 600 }}>{w.label || truncateAddr(w.wallet)}</span>
                    <span style={{ color: "var(--text-muted)", fontSize: 11, marginLeft: 4 }}>
                      {truncateAddr(w.wallet)}
                    </span>
                  </td>
                  <td>
                    <span className={`badge ${badge}`}>
                      {label}
                    </span>
                  </td>
                  <td className="num" style={{ fontWeight: 600 }}>{formatAmount(w.amount, w.chain)} {w.chain}</td>
                  <td>{w.exchange}</td>
                  <td>
                    <span style={{
                      fontSize: 11, padding: "1px 6px", borderRadius: 3,
                      background: w.significance === "high"
                        ? "rgba(245, 158, 11, 0.15)"
                        : "var(--bg-secondary)",
                      color: w.significance === "high"
                        ? "var(--warning)"
                        : "var(--text-muted)",
                    }}>
                      {w.significance}
                    </span>
                  </td>
                </tr>
              );
            })}
            {whales?.length === 0 && (
              <tr><td colSpan={6} style={{ color: "var(--text-muted)", textAlign: "center", padding: 20 }}>No whale movements detected</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
