"use client";

import { useCallback, useEffect, useState } from "react";
import { Header } from "@/components/Header";
import { fetchApi } from "@/lib/api";
import type { ShadowTradeRecord, ShadowStats } from "@/lib/api";

function fmt(n: number | null | undefined, d: number = 2): string {
  if (n == null || Number.isNaN(n)) return "--";
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

function pct(n: number | null | undefined): string {
  if (n == null) return "--";
  return (n >= 0 ? "+" : "") + (n * 100).toFixed(2) + "%";
}

function formatTime(ts: string | null): string {
  if (!ts) return "--";
  try {
    return new Date(ts).toISOString().replace("T", " ").slice(5, 16);
  } catch {
    return "--";
  }
}

function ageOf(ts: string | null): string {
  if (!ts) return "--";
  const ms = Date.now() - new Date(ts).getTime();
  if (ms < 0 || Number.isNaN(ms)) return "--";
  const h = Math.floor(ms / 3_600_000);
  const m = Math.floor((ms % 3_600_000) / 60_000);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

// shadow_tp → tp, shadow_sl → sl, etc.
function shortOutcome(o: string | null): string {
  if (!o) return "--";
  return o.replace(/^shadow_/, "");
}

export default function ShadowPage() {
  const [open, setOpen] = useState<ShadowTradeRecord[]>([]);
  const [closed, setClosed] = useState<ShadowTradeRecord[]>([]);
  const [stats, setStats] = useState<ShadowStats | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const [o, c, s] = await Promise.all([
        fetchApi<ShadowTradeRecord[]>("/shadow/trades?status=open&limit=100"),
        fetchApi<ShadowTradeRecord[]>("/shadow/trades?status=closed&limit=100"),
        fetchApi<ShadowStats>("/shadow/stats"),
      ]);
      setOpen(o);
      setClosed(c);
      setStats(s);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
  }, [load]);

  const pnlPos = (stats?.total_pnl_usd ?? 0) >= 0;

  return (
    <div className="dashboard">
      <Header />

      <div className="card" style={{ gridColumn: "1 / -1" }}>
        <div className="card-title">Shadow Mode — theoretical trades (ml_setups)</div>
        <div style={{ color: "var(--text-muted)", fontSize: 11, marginBottom: 12 }}>
          Bot is shadow-only since 2026-04-15 — no live OKX orders. These are simulated
          fills tracked against price. P&L is net of fees but never executed.
          {stats?.experiment_id && (
            <> Experiment: <span className="num">{stats.experiment_id}</span>.</>
          )}
        </div>
        <div className="hero-stats" style={{ height: "auto" }}>
          <div className="hero-stat">
            <div className="hero-stat-label">Total P&L</div>
            <div className={`hero-value ${pnlPos ? "hero-value-positive" : "hero-value-negative"}`}>
              {pnlPos ? "+" : ""}${fmt(stats?.total_pnl_usd ?? 0)}
            </div>
          </div>
          <div className="hero-stat">
            <div className="hero-stat-label">Win Rate</div>
            <div className={`hero-value ${(stats?.win_rate ?? 0) >= 45 ? "hero-value-positive" : "hero-value-negative"}`}>
              {fmt(stats?.win_rate ?? 0, 1)}%
            </div>
          </div>
          <div className="hero-stat">
            <div className="hero-stat-label">Profit Factor</div>
            <div className={`hero-value ${(stats?.profit_factor ?? 0) >= 1.5 ? "hero-value-positive" : "hero-value-negative"}`}>
              {fmt(stats?.profit_factor ?? 0, 2)}
            </div>
          </div>
          <div className="hero-stat-secondary">
            <span><span className="hero-stat-label">Resolved</span> {stats?.total_trades ?? 0}</span>
            <span><span className="hero-stat-label">Open now</span> {open.length}</span>
          </div>
        </div>
      </div>

      {/* Open shadows */}
      <div className="card" style={{ gridColumn: "1 / -1" }}>
        <div className="card-title">Open shadows ({open.length})</div>
        <div className="scroll-y">
          <table>
            <thead>
              <tr>
                <th>Age</th>
                <th>Pair</th>
                <th>Dir</th>
                <th className="col-type">Type</th>
                <th style={{ textAlign: "right" }}>Entry</th>
                <th style={{ textAlign: "right" }}>SL</th>
                <th className="col-exit" style={{ textAlign: "right" }}>TP1</th>
              </tr>
            </thead>
            <tbody>
              {loading && open.length === 0 && (
                <tr><td colSpan={7}><div className="skeleton" style={{ height: 16, width: "100%" }} /></td></tr>
              )}
              {open.map((t) => (
                <tr key={t.setup_id ?? `${t.pair}-${t.created_at}`} className="animate-in">
                  <td style={{ color: "var(--text-muted)" }}>{ageOf(t.created_at)}</td>
                  <td style={{ fontWeight: 600 }}>{t.pair}</td>
                  <td>
                    <span className={`badge ${t.direction === "long" ? "badge-long" : "badge-short"}`}>
                      {t.direction}
                    </span>
                  </td>
                  <td className="col-type" style={{ color: "var(--text-muted)", fontSize: 11 }}>{t.setup_type}</td>
                  <td className="num">{fmt(t.entry_price)}</td>
                  <td className="num">{fmt(t.sl_price)}</td>
                  <td className="num col-exit">{fmt(t.tp1_price)}</td>
                </tr>
              ))}
              {!loading && open.length === 0 && (
                <tr><td colSpan={7} style={{ color: "var(--text-muted)", textAlign: "center", padding: 20 }}>No open shadows in the last 48h</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Closed shadows */}
      <div className="card" style={{ gridColumn: "1 / -1" }}>
        <div className="card-title">Closed shadows ({closed.length})</div>
        <div className="scroll-y">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Pair</th>
                <th>Dir</th>
                <th className="col-type">Type</th>
                <th style={{ textAlign: "right" }}>Entry</th>
                <th style={{ textAlign: "right" }}>P&L</th>
                <th className="col-pnl-usd" style={{ textAlign: "right" }}>P&L $</th>
                <th className="col-exit">Outcome</th>
              </tr>
            </thead>
            <tbody>
              {loading && closed.length === 0 && (
                <tr><td colSpan={8}><div className="skeleton" style={{ height: 16, width: "100%" }} /></td></tr>
              )}
              {closed.map((t) => {
                const pnlClass = (t.pnl_pct ?? 0) >= 0 ? "pnl-positive" : "pnl-negative";
                return (
                  <tr key={t.setup_id ?? `${t.pair}-${t.resolved_at}`} className="animate-in">
                    <td style={{ color: "var(--text-muted)" }}>{formatTime(t.resolved_at)}</td>
                    <td style={{ fontWeight: 600 }}>{t.pair}</td>
                    <td>
                      <span className={`badge ${t.direction === "long" ? "badge-long" : "badge-short"}`}>
                        {t.direction}
                      </span>
                    </td>
                    <td className="col-type" style={{ color: "var(--text-muted)", fontSize: 11 }}>{t.setup_type}</td>
                    <td className="num">{fmt(t.actual_entry ?? t.entry_price)}</td>
                    <td className={`num ${pnlClass}`}>{pct(t.pnl_pct)}</td>
                    <td className={`num ${pnlClass} col-pnl-usd`}>
                      {t.pnl_usd != null ? (t.pnl_usd >= 0 ? "+" : "") + fmt(t.pnl_usd) : "--"}
                    </td>
                    <td className="col-exit" style={{ fontSize: 11 }}>{shortOutcome(t.outcome_type)}</td>
                  </tr>
                );
              })}
              {!loading && closed.length === 0 && (
                <tr><td colSpan={8} style={{ color: "var(--text-muted)", textAlign: "center", padding: 20 }}>No resolved shadows yet</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Per-setup breakdown */}
      <div className="card" style={{ gridColumn: "1 / -1" }}>
        <div className="card-title">By setup type</div>
        <div className="scroll-y">
          <table>
            <thead>
              <tr>
                <th>Setup</th>
                <th style={{ textAlign: "right" }}>N</th>
                <th style={{ textAlign: "right" }}>WR</th>
                <th style={{ textAlign: "right" }}>PF</th>
                <th style={{ textAlign: "right" }}>P&L $</th>
                <th className="col-type" style={{ textAlign: "right" }}>Avg %</th>
              </tr>
            </thead>
            <tbody>
              {stats?.by_setup_type.map((b) => {
                const pnlClass = b.total_pnl_usd >= 0 ? "pnl-positive" : "pnl-negative";
                return (
                  <tr key={b.setup_type ?? "unknown"} className="animate-in">
                    <td style={{ fontWeight: 600 }}>{b.setup_type}</td>
                    <td className="num">{b.total_trades}</td>
                    <td className="num">{fmt(b.win_rate, 1)}%</td>
                    <td className="num">{fmt(b.profit_factor, 2)}</td>
                    <td className={`num ${pnlClass}`}>
                      {(b.total_pnl_usd >= 0 ? "+" : "") + fmt(b.total_pnl_usd)}
                    </td>
                    <td className="num col-type">{pct(b.avg_pnl_pct)}</td>
                  </tr>
                );
              })}
              {!loading && (!stats || stats.by_setup_type.length === 0) && (
                <tr><td colSpan={6} style={{ color: "var(--text-muted)", textAlign: "center", padding: 20 }}>No resolved shadows yet</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
