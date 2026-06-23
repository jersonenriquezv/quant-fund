"use client";

import { useCallback, useEffect, useState } from "react";
import { Header } from "@/components/Header";
import { fetchApi } from "@/lib/api";
import type { ShadowTradeRecord, ShadowStats, ShadowEquityResponse, ShadowEquityPoint, ShadowMLStatus } from "@/lib/api";

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

// Synthetic paper-equity curve (SVG sparkline — no charting lib per dashboard rules).
function EquityCurve({ points }: { points: ShadowEquityPoint[] }) {
  if (points.length < 2) {
    return (
      <div style={{ color: "var(--text-muted)", fontSize: 12, textAlign: "center", padding: "30px 0" }}>
        Need 2+ resolved shadows for a curve
      </div>
    );
  }
  const vals = points.map((p) => p.equity);
  const start = points[0].equity - points[0].pnl_usd; // implied opening balance
  const min = Math.min(start, ...vals);
  const max = Math.max(start, ...vals);
  const range = max - min || 1;

  const w = 600;
  const h = 160;
  const pad = 6;
  const coords = vals.map((v, i) => {
    const x = pad + (i / (vals.length - 1)) * (w - 2 * pad);
    const y = h - pad - ((v - min) / range) * (h - 2 * pad);
    return `${x},${y}`;
  });
  const startY = h - pad - ((start - min) / range) * (h - 2 * pad);
  const last = vals[vals.length - 1];
  const color = last >= start ? "var(--long)" : "var(--short)";
  const area = `${pad},${h - pad} ${coords.join(" ")} ${w - pad},${h - pad}`;

  return (
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" style={{ width: "100%", height: 180 }}>
      {/* opening-balance baseline */}
      <line x1={pad} y1={startY} x2={w - pad} y2={startY} stroke="var(--border)" strokeWidth="1" strokeDasharray="4,4" />
      <polygon points={area} fill={color} opacity="0.08" />
      <polyline points={coords.join(" ")} fill="none" stroke={color} strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}

export default function ShadowPage() {
  const [open, setOpen] = useState<ShadowTradeRecord[]>([]);
  const [closed, setClosed] = useState<ShadowTradeRecord[]>([]);
  const [stats, setStats] = useState<ShadowStats | null>(null);
  const [equity, setEquity] = useState<ShadowEquityResponse | null>(null);
  const [ml, setMl] = useState<ShadowMLStatus | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const [o, c, s, e, m] = await Promise.all([
        fetchApi<ShadowTradeRecord[]>("/shadow/trades?status=open&limit=100"),
        fetchApi<ShadowTradeRecord[]>("/shadow/trades?status=closed&limit=100"),
        fetchApi<ShadowStats>("/shadow/stats"),
        fetchApi<ShadowEquityResponse>("/shadow/equity"),
        fetchApi<ShadowMLStatus>("/shadow/ml-status"),
      ]);
      setOpen(o);
      setClosed(c);
      setStats(s);
      setEquity(e);
      setMl(m);
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

      {/* Synthetic equity curve */}
      <div className="card" style={{ gridColumn: "1 / -1" }}>
        <div className="card-title">Synthetic equity — paper ${fmt(equity?.start_balance ?? 10000, 0)} start</div>
        <div style={{ color: "var(--text-muted)", fontSize: 11, marginBottom: 10 }}>
          Paper curve: starting balance + cumulative shadow P&L, ordered by resolve time. Not a real account.
        </div>
        <div className="hero-stats" style={{ height: "auto", marginBottom: 10 }}>
          <div className="hero-stat">
            <div className="hero-stat-label">Balance</div>
            <div className="hero-value">${fmt(equity?.current_balance ?? 0)}</div>
          </div>
          <div className="hero-stat">
            <div className="hero-stat-label">Profit</div>
            <div className={`hero-value ${(equity?.total_profit ?? 0) >= 0 ? "hero-value-positive" : "hero-value-negative"}`}>
              {(equity?.total_profit ?? 0) >= 0 ? "+" : ""}${fmt(equity?.total_profit ?? 0)}
            </div>
          </div>
          <div className="hero-stat">
            <div className="hero-stat-label">Return</div>
            <div className={`hero-value ${(equity?.return_pct ?? 0) >= 0 ? "hero-value-positive" : "hero-value-negative"}`}>
              {(equity?.return_pct ?? 0) >= 0 ? "+" : ""}{fmt(equity?.return_pct ?? 0, 1)}%
            </div>
          </div>
          <div className="hero-stat-secondary">
            <span><span className="hero-stat-label">Max DD</span> -${fmt(equity?.max_drawdown_usd ?? 0)} ({fmt(equity?.max_drawdown_pct ?? 0, 1)}%)</span>
            <span><span className="hero-stat-label">Resolved</span> {equity?.n ?? 0}</span>
          </div>
        </div>
        <EquityCurve points={equity?.points ?? []} />
      </div>

      {/* ML training — engine1 meta-label forward gate */}
      <div className="card" style={{ gridColumn: "1 / -1" }}>
        <div className="card-title">ML training — engine1 forward gate</div>
        {!ml?.available ? (
          <div style={{ color: "var(--text-muted)", fontSize: 12, padding: "16px 0" }}>
            No forward-check run yet. Status appears once <span className="num">ml_v1_forward_check.py</span> runs (daily timer).
          </div>
        ) : (() => {
          const pfFmt = (a: { pf: number | null; pnl: number | null } | null) =>
            a == null ? "--" : a.pf == null ? ((a.pnl ?? 0) > 0 ? "∞" : "--") : a.pf.toFixed(2);
          const stateColor =
            ml.verdict_state === "pass" ? "var(--long)" :
            ml.verdict_state === "fail" ? "var(--short)" : "var(--accent)";
          const progress = Math.min(100, (ml.n_forward / Math.max(1, ml.n_gate)) * 100);
          return (
            <>
              <div style={{ color: "var(--text-muted)", fontSize: 11, marginBottom: 12 }}>
                Frozen model scores genuinely-unseen forward trades. Does the model&apos;s top-half beat take-all? That&apos;s the real-money gate.
                {ml.cutoff_created_at && <> Freeze cutoff <span className="num">{ml.cutoff_created_at.slice(0, 16)}</span>, trained on N={ml.train_n}.</>}
              </div>

              {/* gate progress */}
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 4 }}>
                <span className="hero-stat-label">Forward gate</span>
                <span className="num" style={{ color: stateColor }}>{ml.n_forward} / {ml.n_gate}</span>
              </div>
              <div style={{ height: 8, borderRadius: 100, background: "var(--bg-secondary)", overflow: "hidden", marginBottom: 12 }}>
                <div style={{ width: `${progress}%`, height: "100%", background: stateColor, borderRadius: 100, transition: "width .3s" }} />
              </div>

              <div style={{
                fontSize: 12, padding: "8px 10px", borderRadius: 8, marginBottom: 12,
                background: "var(--bg-secondary)", borderLeft: `3px solid ${stateColor}`, color: "var(--text-secondary)",
              }}>
                {ml.verdict}
              </div>

              <div className="scroll-y">
                <table>
                  <thead>
                    <tr>
                      <th>Arm</th>
                      <th style={{ textAlign: "right" }}>N</th>
                      <th style={{ textAlign: "right" }}>WR</th>
                      <th style={{ textAlign: "right" }}>PF</th>
                      <th style={{ textAlign: "right" }}>P&L $</th>
                    </tr>
                  </thead>
                  <tbody>
                    {([["Take all", ml.take_all], ["Model top half", ml.top_half], ["Model bottom half", ml.bottom_half]] as const).map(([label, a]) => (
                      <tr key={label} className="animate-in">
                        <td style={{ fontWeight: 600 }}>{label}</td>
                        <td className="num">{a?.n ?? "--"}</td>
                        <td className="num">{a?.wr != null ? a.wr.toFixed(1) + "%" : "--"}</td>
                        <td className="num">{pfFmt(a)}</td>
                        <td className={`num ${(a?.pnl ?? 0) >= 0 ? "pnl-positive" : "pnl-negative"}`}>
                          {a?.pnl != null ? (a.pnl >= 0 ? "+" : "") + fmt(a.pnl) : "--"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {ml.updated_at && (
                <div style={{ color: "var(--text-muted)", fontSize: 10, marginTop: 8 }}>
                  Last check: {ml.updated_at.slice(0, 16)} UTC
                </div>
              )}
            </>
          );
        })()}
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
