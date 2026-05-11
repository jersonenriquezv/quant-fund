"use client";

import { useEffect, useState, useCallback, useMemo } from "react";
import Link from "next/link";
import { fetchApi } from "@/lib/api";
import type { BybitAnnotation, BybitSummary, BybitEquityPoint, BybitPendingOrder, BybitGradeStats, BybitGradeStatsRow } from "@/lib/api";

const GRADE_COLORS: Record<string, string> = {
  A: "#b2fd02",
  B: "#9ca3af",
  C: "#f59e0b",
  D: "#ff4d4d",
};

function fmt(n: number | null | undefined, d: number = 2): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

function fmtShort(iso: string | null): { date: string; time: string } {
  if (!iso) return { date: "—", time: "" };
  const d = new Date(iso);
  return {
    date: d.toLocaleString("en-US", { month: "short", day: "2-digit" }).toUpperCase(),
    time: d.toLocaleString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false }),
  };
}

const FILTERS: { k: string; label: string }[] = [
  { k: "all", label: "ALL" },
  { k: "open", label: "LIVE" },
  { k: "closed", label: "CLOSED" },
  { k: "unannotated", label: "UNTOUCHED" },
];

export default function BybitLogPage() {
  const [trades, setTrades] = useState<BybitAnnotation[]>([]);
  const [pending, setPending] = useState<BybitPendingOrder[]>([]);
  const [summary, setSummary] = useState<BybitSummary | null>(null);
  const [equity, setEquity] = useState<BybitEquityPoint[]>([]);
  const [gradeStats, setGradeStats] = useState<BybitGradeStatsRow[]>([]);
  const [filter, setFilter] = useState<string>("all");
  const [days, setDays] = useState(30);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const status = filter === "open" || filter === "closed" ? filter : undefined;
      const qs = status ? `?status=${status}&limit=100` : "?limit=100";
      const [list, sum, eq, pend, gs] = await Promise.all([
        fetchApi<BybitAnnotation[]>(`/bybit/annotations${qs}`),
        fetchApi<BybitSummary>(`/bybit/summary?days=${days}`),
        fetchApi<{ points: BybitEquityPoint[] }>(`/bybit/equity-curve?days=${days}`),
        fetchApi<BybitPendingOrder[]>(`/bybit/pending?status=pending&limit=50`),
        fetchApi<BybitGradeStats>(`/bybit/grade-stats?days=${days}`),
      ]);
      let final = list;
      if (filter === "unannotated") final = list.filter((t) => !t.thesis_pre);
      setTrades(final);
      setSummary(sum);
      setEquity(eq.points);
      setPending(pend);
      setGradeStats(gs.by_grade);
    } finally {
      setLoading(false);
    }
  }, [filter, days]);

  useEffect(() => { load(); }, [load]);

  const t = summary?.totals;
  const pnlClass = (t?.total_pnl ?? 0) >= 0 ? "pos" : "neg";

  return (
    <div className="log-root">
      <div className="grain" />

      <header className="log-header">
        <div className="brand-row">
          <Link href="/" className="brand-back">← QF</Link>
          <div className="brand-divider" />
          <span className="brand-tag">MANUAL · BYBIT UTA</span>
        </div>
        <h1 className="log-title">
          <span className="serif">Trade</span>
          <span className="mono">LOG/</span>
          <span className="serif italic">journal</span>
        </h1>
        <p className="log-subtitle">
          Manual trades, processed. Pattern-hunted. Graded.
        </p>
      </header>

      <section className="hero-stats">
        <HeroStat
          label="NET P&L"
          value={t ? `${t.total_pnl >= 0 ? "+" : ""}$${fmt(t.total_pnl)}` : "—"}
          className={pnlClass}
          big
          i={0}
        />
        <HeroStat
          label="WIN RATE"
          value={t?.win_rate_pct != null ? `${fmt(t.win_rate_pct, 1)}%` : "—"}
          sub={t ? `${t.wins}W · ${t.losses}L` : ""}
          i={1}
        />
        <HeroStat
          label="CLOSED"
          value={String(t?.closed ?? 0)}
          sub={t ? `${t.open} live` : ""}
          i={2}
        />
        <HeroStat
          label="ANNOTATED"
          value={t ? `${t.annotated} / ${t.closed + t.open}` : "—"}
          sub={t && t.closed > 0 ? `${Math.round((t.annotated / (t.closed + t.open)) * 100)}% logged` : ""}
          i={3}
        />
      </section>

      {pending.length > 0 && (
        <section className="pending-section">
          <div className="section-head">
            <span className="eyebrow">PENDING ORDERS · {pending.length}</span>
          </div>
          <div className="pending-list">
            {pending.map((p) => <PendingRow key={p.id} p={p} />)}
          </div>
        </section>
      )}

      <section className="curve-section">
        <div className="section-head">
          <span className="eyebrow">EQUITY — CUMULATIVE</span>
          <div className="day-switch">
            {[7, 30, 90, 365].map((d) => (
              <button
                key={d}
                className={`dbtn ${days === d ? "on" : ""}`}
                onClick={() => setDays(d)}
              >
                {d === 365 ? "1Y" : `${d}D`}
              </button>
            ))}
          </div>
        </div>
        <EquityChart points={equity} />
      </section>

      <section className="grade-section">
        <div className="section-head">
          <span className="eyebrow">DECISION QUALITY — BY AUTO GRADE</span>
          <span className="grade-hint">closed trades · {days}D</span>
        </div>
        <GradeStatsTable rows={gradeStats} />
      </section>

      <section className="log-section">
        <div className="section-head">
          <span className="eyebrow">THE TRADES</span>
          <div className="filter-chips">
            {FILTERS.map((f) => (
              <button
                key={f.k}
                className={`fchip ${filter === f.k ? "on" : ""}`}
                onClick={() => setFilter(f.k)}
              >
                {f.label}
              </button>
            ))}
          </div>
        </div>

        {loading && <div className="loading">— loading —</div>}

        <div className="tlist">
          {trades.map((tr, i) => <TradeRow key={tr.id} t={tr} i={i} />)}
          {!loading && trades.length === 0 && (
            <div className="empty">
              <span className="serif italic">No trades in this slice.</span>
            </div>
          )}
        </div>
      </section>

      <style jsx global>{`
        .log-root {
          min-height: 100vh;
          background: #050505;
          color: #f5f5f7;
          font-family: "JetBrains Mono", monospace;
          position: relative;
          overflow: hidden;
        }
        .grain {
          position: fixed;
          inset: 0;
          pointer-events: none;
          z-index: 100;
          opacity: 0.035;
          background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='3'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
        }
        .log-root .serif { font-family: "Fraunces", Georgia, serif; font-weight: 400; letter-spacing: -0.02em; }
        .log-root .serif.italic { font-style: italic; }
        .log-root .mono { font-family: "JetBrains Mono", monospace; }
      `}</style>

      <style jsx>{`
        .log-header {
          padding: 48px 32px 24px 32px;
          max-width: 1400px;
          margin: 0 auto;
          animation: fade-in 0.8s ease both;
        }
        .brand-row {
          display: flex;
          align-items: center;
          gap: 12px;
          font-size: 11px;
          letter-spacing: 0.12em;
          text-transform: uppercase;
          color: rgba(255, 255, 255, 0.4);
          margin-bottom: 32px;
        }
        .brand-back {
          color: rgba(255, 255, 255, 0.6);
          text-decoration: none;
          font-weight: 600;
          transition: color 0.2s;
        }
        .brand-back:hover { color: #fff; }
        .brand-divider { flex: 0 0 1px; height: 12px; background: rgba(255, 255, 255, 0.2); }
        .brand-tag { font-weight: 500; }
        .log-title {
          font-size: clamp(48px, 9vw, 120px);
          line-height: 0.9;
          font-weight: 400;
          margin: 0;
          letter-spacing: -0.04em;
          display: flex;
          align-items: baseline;
          flex-wrap: wrap;
          gap: 0.1em;
        }
        .log-title .mono {
          color: #b2fd02;
          font-size: 0.5em;
          font-weight: 700;
          letter-spacing: 0.02em;
          transform: translateY(-0.25em);
          display: inline-block;
        }
        .log-subtitle {
          margin-top: 16px;
          font-family: "Fraunces", serif;
          font-style: italic;
          font-weight: 300;
          font-size: 20px;
          color: rgba(255, 255, 255, 0.55);
          max-width: 520px;
        }

        .hero-stats {
          display: grid;
          grid-template-columns: repeat(4, 1fr);
          border-top: 1px solid rgba(255, 255, 255, 0.09);
          border-bottom: 1px solid rgba(255, 255, 255, 0.09);
          max-width: 1400px;
          margin: 24px auto 0 auto;
        }

        .curve-section, .log-section, .pending-section, .grade-section {
          max-width: 1400px;
          margin: 0 auto;
          padding: 40px 32px;
        }
        .grade-section { padding-top: 24px; padding-bottom: 24px; }
        .grade-hint {
          font-size: 10px;
          letter-spacing: 0.15em;
          color: rgba(255,255,255,0.35);
          font-family: "JetBrains Mono", monospace;
        }
        .pending-section { padding-top: 32px; padding-bottom: 8px; }
        .pending-list { border-top: 1px solid rgba(255,255,255,0.08); }
        .section-head {
          display: flex;
          justify-content: space-between;
          align-items: center;
          margin-bottom: 20px;
          flex-wrap: wrap;
          gap: 12px;
        }
        .eyebrow {
          font-size: 10px;
          letter-spacing: 0.2em;
          text-transform: uppercase;
          color: rgba(255, 255, 255, 0.4);
          font-weight: 600;
        }
        .day-switch, .filter-chips { display: flex; gap: 4px; }
        .dbtn, .fchip {
          background: transparent;
          color: rgba(255, 255, 255, 0.5);
          border: 1px solid rgba(255, 255, 255, 0.12);
          padding: 6px 12px;
          font-family: "JetBrains Mono", monospace;
          font-size: 11px;
          font-weight: 600;
          letter-spacing: 0.08em;
          cursor: pointer;
          transition: all 0.15s;
          border-radius: 0;
        }
        .dbtn:hover, .fchip:hover { color: #fff; border-color: rgba(255, 255, 255, 0.3); }
        .dbtn.on, .fchip.on {
          background: #b2fd02;
          color: #000;
          border-color: #b2fd02;
        }
        .loading { padding: 40px; text-align: center; color: rgba(255,255,255,0.4); font-size: 12px; letter-spacing: 0.2em; }
        .empty { padding: 80px 20px; text-align: center; color: rgba(255,255,255,0.4); font-size: 22px; }
        .tlist { display: flex; flex-direction: column; }

        @keyframes fade-in {
          from { opacity: 0; transform: translateY(8px); }
          to { opacity: 1; transform: translateY(0); }
        }
        @media (max-width: 900px) {
          .log-header { padding: 28px 16px 16px; }
          .log-subtitle { font-size: 16px; }
          .hero-stats { grid-template-columns: repeat(2, 1fr); }
          .curve-section, .log-section, .grade-section { padding: 28px 16px; }
        }
      `}</style>
    </div>
  );
}

function HeroStat({
  label,
  value,
  sub,
  className,
  big,
  i,
}: {
  label: string;
  value: string;
  sub?: string;
  className?: string;
  big?: boolean;
  i: number;
}) {
  return (
    <div className={`hstat ${className || ""} ${big ? "big" : ""}`} style={{ animationDelay: `${0.1 + i * 0.08}s` }}>
      <div className="hstat-label">{label}</div>
      <div className="hstat-value">{value}</div>
      {sub && <div className="hstat-sub">{sub}</div>}
      <style jsx>{`
        .hstat {
          padding: 28px 24px;
          border-right: 1px solid rgba(255, 255, 255, 0.09);
          animation: rise 0.7s ease both;
        }
        .hstat:last-child { border-right: none; }
        .hstat-label {
          font-size: 9px;
          letter-spacing: 0.2em;
          color: rgba(255, 255, 255, 0.35);
          font-weight: 600;
          margin-bottom: 14px;
        }
        .hstat-value {
          font-family: "Fraunces", serif;
          font-size: 44px;
          font-weight: 400;
          letter-spacing: -0.03em;
          line-height: 1;
          font-feature-settings: "tnum" 1, "ss01" 1;
        }
        .hstat.big .hstat-value { font-size: 56px; font-weight: 500; }
        .hstat.pos .hstat-value { color: #b2fd02; }
        .hstat.neg .hstat-value { color: #ff4d4d; }
        .hstat-sub {
          margin-top: 10px;
          font-size: 11px;
          letter-spacing: 0.1em;
          color: rgba(255, 255, 255, 0.35);
          font-family: "JetBrains Mono", monospace;
        }
        @keyframes rise {
          from { opacity: 0; transform: translateY(20px); }
          to { opacity: 1; transform: translateY(0); }
        }
        @media (max-width: 900px) {
          .hstat { padding: 20px 16px; }
          .hstat-value { font-size: 32px !important; }
          .hstat.big .hstat-value { font-size: 38px !important; }
        }
      `}</style>
    </div>
  );
}

function TradeRow({ t, i }: { t: BybitAnnotation; i: number }) {
  const pnl = t.pnl_usd ?? 0;
  const ctx = t.context_snapshot as Record<string, unknown> | null;
  const htf = (ctx?.htf_bias as Record<string, unknown> | undefined) || {};
  const warnings = (ctx?.warnings as string[] | undefined) || [];
  const opened = fmtShort(t.opened_at);
  const isLong = t.side === "Buy";
  const isClosed = t.status === "closed";
  const aligned = htf.aligned_with_trade;

  return (
    <Link href={`/annotate/${t.id}`} className="trow-link" style={{ animationDelay: `${Math.min(i * 0.03, 0.5)}s` }}>
      <article className={`trow ${isClosed ? "closed" : "live"}`}>
        {/* LEFT: timestamp block */}
        <div className="trow-time">
          <div className="t-date">{opened.date}</div>
          <div className="t-hour">{opened.time}</div>
        </div>

        {/* MIDDLE: identity + details */}
        <div className="trow-body">
          <div className="trow-head">
            <span className={`dir-tag ${isLong ? "long" : "short"}`}>
              {isLong ? "LONG" : "SHORT"}
            </span>
            <span className="sym serif">{t.symbol}</span>
            {t.leverage && <span className="lev">×{fmt(t.leverage, 0)}</span>}
            {!t.thesis_pre && <span className="untouch">◌ untouched</span>}
            {!isClosed && <span className="live-pulse">● live</span>}
          </div>

          {t.setup_type && (
            <div className="setup-line">
              <span className="setup-pill">{t.setup_type}</span>
              {t.confidence != null && (
                <span className="stars">{"★".repeat(t.confidence)}{"☆".repeat(5 - t.confidence)}</span>
              )}
              {t.emotional_state && <span className="emo">· {t.emotional_state}</span>}
            </div>
          )}

          {t.thesis_pre && (
            <p className="thesis serif italic">
              &ldquo;{t.thesis_pre.slice(0, 180)}{t.thesis_pre.length > 180 ? "…" : ""}&rdquo;
            </p>
          )}

          <div className="ctx-row">
            <span className="ctx-item">
              <span className="ctx-l">ENTRY</span>
              <span className="ctx-v">{fmt(t.entry_price, 4)}</span>
            </span>
            {isClosed && (
              <span className="ctx-item">
                <span className="ctx-l">EXIT</span>
                <span className="ctx-v">{fmt(t.exit_price, 4)}</span>
              </span>
            )}
            {aligned === true && <span className="ctx-flag ok">HTF ✓</span>}
            {aligned === false && <span className="ctx-flag warn">HTF ⚠</span>}
            {warnings.length > 0 && <span className="ctx-flag warn">{warnings.length} warn</span>}
            {t.grade_self && <span className={`grade g-${t.grade_self}`}>{t.grade_self}</span>}
          </div>
        </div>

        {/* RIGHT: P&L block */}
        <div className="trow-pnl">
          {isClosed ? (
            <>
              <div className={`pnl-num ${pnl >= 0 ? "pos" : "neg"}`}>
                {pnl >= 0 ? "+" : ""}${fmt(pnl)}
              </div>
              <div className={`pnl-pct ${pnl >= 0 ? "pos" : "neg"}`}>
                {fmt(t.pnl_pct, 2)}%
              </div>
            </>
          ) : (
            <div className="pnl-live">OPEN</div>
          )}
        </div>
      </article>
      <style jsx>{`
        .trow-link, .trow-link:visited, .trow-link:hover, .trow-link:active {
          text-decoration: none;
          color: #f5f5f7;
          animation: slide 0.5s ease both;
          display: block;
        }
        .trow {
          display: grid;
          grid-template-columns: 88px 1fr 140px;
          gap: 24px;
          padding: 22px 24px;
          border-bottom: 1px solid rgba(255, 255, 255, 0.06);
          transition: background 0.2s ease;
          cursor: pointer;
          position: relative;
        }
        .trow::before {
          content: "";
          position: absolute;
          left: 0; top: 0; bottom: 0;
          width: 2px;
          background: transparent;
          transition: background 0.2s;
        }
        .trow:hover { background: rgba(255, 255, 255, 0.025); }
        .trow:hover::before { background: #b2fd02; }
        .trow.live { background: rgba(178, 253, 2, 0.03); }
        .trow.live::before { background: rgba(178, 253, 2, 0.35); }

        .trow-time {
          font-family: "JetBrains Mono", monospace;
          color: rgba(255, 255, 255, 0.4);
          font-size: 11px;
          letter-spacing: 0.06em;
        }
        .t-date { font-weight: 700; color: rgba(255, 255, 255, 0.6); }
        .t-hour { margin-top: 4px; font-size: 10px; }

        .trow-body { min-width: 0; }
        .trow-head {
          display: flex;
          align-items: center;
          gap: 10px;
          flex-wrap: wrap;
        }
        .dir-tag {
          font-size: 10px;
          font-weight: 700;
          letter-spacing: 0.14em;
          padding: 3px 8px;
          border-radius: 2px;
        }
        .dir-tag.long { color: #b2fd02; background: rgba(178, 253, 2, 0.08); }
        .dir-tag.short { color: #ff4d4d; background: rgba(255, 77, 77, 0.08); }
        .sym {
          font-family: "Instrument Serif", Georgia, serif !important;
          font-size: 20px;
          font-weight: 400;
          letter-spacing: -0.01em;
          color: #f5f5f7 !important;
        }
        .lev {
          font-size: 11px;
          color: rgba(255, 255, 255, 0.5);
          font-weight: 500;
          letter-spacing: 0.05em;
        }
        .untouch {
          font-size: 10px;
          letter-spacing: 0.1em;
          color: #f59e0b;
          background: rgba(245, 158, 11, 0.08);
          padding: 2px 8px;
          border-radius: 2px;
        }
        .live-pulse {
          font-size: 10px;
          color: #b2fd02;
          letter-spacing: 0.12em;
          animation: pulse 1.5s ease-in-out infinite;
        }
        @keyframes pulse { 0%,100% { opacity: 0.55; } 50% { opacity: 1; } }

        .setup-line {
          margin-top: 8px;
          display: flex;
          gap: 10px;
          align-items: center;
          font-size: 11px;
          letter-spacing: 0.05em;
        }
        .setup-pill {
          background: rgba(255, 255, 255, 0.06);
          padding: 2px 8px;
          border-radius: 2px;
          text-transform: uppercase;
          font-weight: 600;
          color: rgba(255, 255, 255, 0.75);
        }
        .stars { color: #f59e0b; font-size: 12px; letter-spacing: 0.1em; }
        .emo { color: rgba(255, 255, 255, 0.5); text-transform: lowercase; }

        .thesis {
          margin-top: 10px;
          font-size: 15px !important;
          line-height: 1.45;
          color: rgba(255, 255, 255, 0.72);
          font-family: "Fraunces", serif !important;
          font-style: italic !important;
          font-weight: 300;
          max-width: 70ch;
        }

        .ctx-row {
          margin-top: 10px;
          display: flex;
          gap: 16px;
          flex-wrap: wrap;
          align-items: center;
        }
        .ctx-item { display: inline-flex; gap: 6px; align-items: baseline; }
        .ctx-l { font-size: 9px; letter-spacing: 0.12em; color: rgba(255,255,255,0.35); }
        .ctx-v { font-size: 12px; font-weight: 500; font-family: "JetBrains Mono", monospace; color: rgba(255,255,255,0.8); }
        .ctx-flag {
          font-size: 10px;
          padding: 2px 7px;
          border-radius: 2px;
          letter-spacing: 0.06em;
          font-weight: 600;
        }
        .ctx-flag.ok { color: #b2fd02; background: rgba(178,253,2,0.08); }
        .ctx-flag.warn { color: #f59e0b; background: rgba(245, 158, 11, 0.08); }
        .grade {
          font-family: "Fraunces", serif;
          font-size: 16px;
          font-weight: 700;
          margin-left: auto;
          padding: 2px 8px;
          border-radius: 2px;
        }
        .g-A { color: #b2fd02; background: rgba(178,253,2,0.1); }
        .g-B { color: #9ca3af; background: rgba(156,163,175,0.1); }
        .g-C { color: #f59e0b; background: rgba(245,158,11,0.1); }
        .g-D, .g-F { color: #ff4d4d; background: rgba(255,77,77,0.08); }

        .trow-pnl {
          text-align: right;
          align-self: center;
        }
        .pnl-num {
          font-family: "Fraunces", serif;
          font-size: 30px;
          font-weight: 500;
          letter-spacing: -0.02em;
          line-height: 1;
          font-feature-settings: "tnum" 1;
        }
        .pnl-pct {
          margin-top: 6px;
          font-size: 12px;
          font-family: "JetBrains Mono", monospace;
          font-weight: 500;
        }
        .pnl-num.pos, .pnl-pct.pos { color: #b2fd02; }
        .pnl-num.neg, .pnl-pct.neg { color: #ff4d4d; }
        .pnl-live {
          font-size: 11px;
          letter-spacing: 0.2em;
          color: #b2fd02;
          font-weight: 600;
        }

        @keyframes slide {
          from { opacity: 0; transform: translateY(8px); }
          to { opacity: 1; transform: translateY(0); }
        }
        @media (max-width: 900px) {
          .trow {
            grid-template-columns: 68px 1fr;
            gap: 14px;
            padding: 16px 16px;
          }
          .trow-pnl {
            grid-column: 1 / -1;
            text-align: left;
            margin-top: 4px;
            display: flex;
            gap: 16px;
            align-items: baseline;
          }
          .pnl-num { font-size: 22px; }
          .sym { font-size: 17px !important; }
          .thesis { font-size: 13px !important; }
        }
      `}</style>
    </Link>
  );
}

function PendingRow({ p }: { p: BybitPendingOrder }) {
  const isLong = p.side === "Buy";
  const displayType = p.stop_order_type || p.order_type || "LIMIT";
  const priceStr = p.price ? fmt(p.price, 4) : p.trigger_price ? `trig ${fmt(p.trigger_price, 4)}` : "—";
  const placed = fmtShort(p.placed_at);
  return (
    <Link href={`/pending/${p.id}`} className="pending-link">
      <article className="prow">
        <div className="prow-time">
          <div className="t-date">{placed.date}</div>
          <div className="t-hour">{placed.time}</div>
        </div>
        <div className="prow-body">
          <div className="prow-head">
            <span className={`dir-tag ${isLong ? "long" : "short"}`}>{isLong ? "LONG" : "SHORT"}</span>
            <span className="sym">{p.symbol}</span>
            <span className="otype">{displayType}</span>
            {!p.thesis_pre && <span className="untouch">◌ no thesis</span>}
            <span className="pulse-dot">● pending</span>
          </div>
          {p.thesis_pre && (
            <p className="thesis">&ldquo;{p.thesis_pre.slice(0, 140)}{p.thesis_pre.length > 140 ? "…" : ""}&rdquo;</p>
          )}
        </div>
        <div className="prow-price">
          <div className="p-qty">{fmt(p.qty, 2)}</div>
          <div className="p-price">@ {priceStr}</div>
        </div>
      </article>
      <style jsx>{`
        .pending-link, .pending-link:visited, .pending-link:hover, .pending-link:active { text-decoration: none; color: #f5f5f7; display: block; }
        .prow {
          display: grid;
          grid-template-columns: 88px 1fr 140px;
          gap: 24px;
          padding: 20px 24px;
          border-bottom: 1px solid rgba(255,255,255,0.06);
          background: rgba(245, 158, 11, 0.025);
          position: relative;
          transition: background 0.2s;
        }
        .prow::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 2px; background: rgba(245,158,11,0.45); }
        .prow:hover { background: rgba(245, 158, 11, 0.06); }
        .prow:hover::before { background: #f59e0b; }
        .prow-time { font-family: "JetBrains Mono", monospace; color: rgba(255,255,255,0.4); font-size: 11px; }
        .t-date { font-weight: 700; color: rgba(255,255,255,0.6); }
        .t-hour { margin-top: 4px; font-size: 10px; }
        .prow-body { min-width: 0; }
        .prow-head { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
        .dir-tag { font-size: 10px; font-weight: 700; letter-spacing: 0.14em; padding: 3px 8px; border-radius: 2px; }
        .dir-tag.long { color: #b2fd02; background: rgba(178,253,2,0.08); }
        .dir-tag.short { color: #ff4d4d; background: rgba(255,77,77,0.08); }
        .sym { font-family: "Instrument Serif", Georgia, serif; font-size: 20px; font-weight: 400; letter-spacing: -0.01em; color: #f5f5f7; }
        .otype { font-size: 10px; letter-spacing: 0.1em; color: rgba(255,255,255,0.5); background: rgba(255,255,255,0.06); padding: 2px 7px; border-radius: 2px; }
        .untouch { font-size: 10px; letter-spacing: 0.1em; color: #f59e0b; background: rgba(245,158,11,0.08); padding: 2px 8px; border-radius: 2px; }
        .pulse-dot { font-size: 10px; color: #f59e0b; letter-spacing: 0.12em; margin-left: auto; animation: pulse 1.5s ease-in-out infinite; }
        @keyframes pulse { 0%,100% { opacity: 0.55; } 50% { opacity: 1; } }
        .thesis { margin-top: 8px; font-family: "Fraunces", Georgia, serif; font-style: italic; font-weight: 300; font-size: 14px; line-height: 1.45; color: rgba(255,255,255,0.72); max-width: 70ch; }
        .prow-price { text-align: right; align-self: center; }
        .p-qty { font-family: "Instrument Serif", Georgia, serif; font-size: 22px; color: #f5f5f7; }
        .p-price { font-size: 11px; font-family: "JetBrains Mono", monospace; color: rgba(255,255,255,0.5); margin-top: 4px; letter-spacing: 0.04em; }
        @media (max-width: 900px) {
          .prow { grid-template-columns: 68px 1fr; gap: 14px; padding: 14px 16px; }
          .prow-price { grid-column: 1 / -1; text-align: left; margin-top: 4px; display: flex; gap: 12px; align-items: baseline; }
          .p-qty { font-size: 18px; }
          .sym { font-size: 17px; }
        }
      `}</style>
    </Link>
  );
}

function GradeStatsTable({ rows }: { rows: BybitGradeStatsRow[] }) {
  if (!rows.length) {
    return (
      <div className="gs-empty">
        — no closed trades graded in this window —
      </div>
    );
  }
  const order = ["A", "B", "C", "D"];
  const sorted = [...rows].sort((a, b) => order.indexOf(a.auto_grade) - order.indexOf(b.auto_grade));
  return (
    <div className="gs-wrap">
      <table className="gs-tbl">
        <thead>
          <tr>
            <th className="gs-grade-col">GRADE</th>
            <th className="num">N</th>
            <th className="num">WR</th>
            <th className="num">PF</th>
            <th className="num">AVG $</th>
            <th className="num">AVG %</th>
            <th className="num">NET $</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => {
            const color = GRADE_COLORS[r.auto_grade] || "#fff";
            const netCls = r.total_pnl_usd >= 0 ? "pos" : "neg";
            return (
              <tr key={r.auto_grade}>
                <td className="gs-grade-cell">
                  <span className="g-pill" style={{ color, borderColor: color, background: `${color}1a` }}>{r.auto_grade}</span>
                </td>
                <td className="num">{r.n}</td>
                <td className="num">{r.win_rate_pct != null ? `${r.win_rate_pct.toFixed(1)}%` : "—"}</td>
                <td className="num">{r.profit_factor != null ? r.profit_factor.toFixed(2) : "—"}</td>
                <td className={`num ${r.avg_pnl_usd >= 0 ? "pos" : "neg"}`}>{r.avg_pnl_usd >= 0 ? "+" : ""}${fmt(r.avg_pnl_usd)}</td>
                <td className={`num ${r.avg_pnl_pct >= 0 ? "pos" : "neg"}`}>{r.avg_pnl_pct >= 0 ? "+" : ""}{r.avg_pnl_pct.toFixed(2)}%</td>
                <td className={`num ${netCls}`}>{r.total_pnl_usd >= 0 ? "+" : ""}${fmt(r.total_pnl_usd)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="gs-foot">
        Auto-grade scores <strong>decision quality</strong> (confluences at entry) — not outcome.
        If <code>D</code> trades outperform <code>A</code>, your rubric is mis-calibrated for current regime,
        or the auto-classifier is missing structural signals you actually use.
      </p>
      <style jsx>{`
        .gs-empty {
          padding: 40px 0;
          text-align: center;
          color: rgba(255,255,255,0.35);
          font-size: 12px;
          letter-spacing: 0.18em;
          border-top: 1px solid rgba(255,255,255,0.08);
          border-bottom: 1px solid rgba(255,255,255,0.08);
        }
        .gs-wrap {
          border-top: 1px solid rgba(255,255,255,0.08);
          border-bottom: 1px solid rgba(255,255,255,0.08);
        }
        .gs-tbl {
          width: 100%;
          border-collapse: collapse;
          font-family: "JetBrains Mono", monospace;
          font-size: 12px;
        }
        .gs-tbl th {
          text-align: left;
          font-size: 9px;
          letter-spacing: 0.18em;
          color: rgba(255,255,255,0.4);
          font-weight: 600;
          padding: 14px 14px;
          border-bottom: 1px solid rgba(255,255,255,0.08);
        }
        .gs-tbl th.num { text-align: right; }
        .gs-tbl td {
          padding: 14px 14px;
          border-bottom: 1px solid rgba(255,255,255,0.04);
        }
        .gs-tbl td.num {
          text-align: right;
          font-variant-numeric: tabular-nums;
          color: rgba(255,255,255,0.88);
        }
        .gs-tbl td.num.pos { color: #b2fd02; }
        .gs-tbl td.num.neg { color: #ff4d4d; }
        .gs-tbl tr:hover td { background: rgba(255,255,255,0.02); }
        .gs-grade-col { width: 80px; }
        .gs-grade-cell { padding-left: 14px !important; }
        .g-pill {
          display: inline-block;
          font-family: "Fraunces", serif;
          font-weight: 700;
          font-size: 18px;
          padding: 2px 12px;
          border: 1px solid;
          border-radius: 2px;
          min-width: 28px;
          text-align: center;
        }
        .gs-foot {
          margin: 14px 0 0 0;
          padding: 0 4px;
          font-family: "Fraunces", Georgia, serif;
          font-style: italic;
          font-weight: 300;
          font-size: 13px;
          line-height: 1.5;
          color: rgba(255,255,255,0.55);
        }
        .gs-foot strong {
          font-style: normal;
          font-weight: 500;
          color: rgba(255,255,255,0.85);
        }
        .gs-foot code {
          font-family: "JetBrains Mono", monospace;
          font-size: 11px;
          padding: 0 4px;
          color: rgba(255,255,255,0.75);
          background: rgba(255,255,255,0.06);
          border-radius: 2px;
        }
        @media (max-width: 639px) {
          .gs-tbl { font-size: 11px; }
          .gs-tbl th, .gs-tbl td { padding: 10px 8px; }
          .gs-tbl th.hide-mob, .gs-tbl td.hide-mob { display: none; }
          .g-pill { font-size: 15px; padding: 2px 8px; }
          .gs-foot { font-size: 12px; }
        }
      `}</style>
    </div>
  );
}

function EquityChart({ points }: { points: BybitEquityPoint[] }) {
  const chart = useMemo(() => {
    if (!points.length) return null;
    const max = Math.max(...points.map((p) => p.cumulative_pnl), 0);
    const min = Math.min(...points.map((p) => p.cumulative_pnl), 0);
    const range = (max - min) || 1;
    const W = 1200, H = 220, PAD_Y = 24, PAD_X = 0;
    const step = (W - 2 * PAD_X) / Math.max(points.length - 1, 1);
    const pts = points.map((p, i) => {
      const x = PAD_X + i * step;
      const y = H - PAD_Y - ((p.cumulative_pnl - min) / range) * (H - 2 * PAD_Y);
      return { x, y, ...p };
    });
    const pathD = pts.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" ");
    const areaD = `${pathD} L ${pts[pts.length - 1].x.toFixed(1)} ${H - PAD_Y} L ${pts[0].x.toFixed(1)} ${H - PAD_Y} Z`;
    const zeroY = H - PAD_Y - ((0 - min) / range) * (H - 2 * PAD_Y);
    const final = pts[pts.length - 1];
    const isPos = final.cumulative_pnl >= 0;
    return { pts, pathD, areaD, zeroY, W, H, isPos, max, min };
  }, [points]);

  if (!chart) return <div className="chart-empty">— no data —</div>;

  return (
    <div className="eq-chart">
      <svg viewBox={`0 0 ${chart.W} ${chart.H}`} preserveAspectRatio="none" width="100%" height="220">
        <defs>
          <linearGradient id="eqFill" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor={chart.isPos ? "#b2fd02" : "#ff4d4d"} stopOpacity="0.22" />
            <stop offset="100%" stopColor={chart.isPos ? "#b2fd02" : "#ff4d4d"} stopOpacity="0" />
          </linearGradient>
        </defs>
        <line x1="0" y1={chart.zeroY} x2={chart.W} y2={chart.zeroY} stroke="rgba(255,255,255,0.1)" strokeDasharray="2 4" />
        <path d={chart.areaD} fill="url(#eqFill)" />
        <path d={chart.pathD} fill="none" stroke={chart.isPos ? "#b2fd02" : "#ff4d4d"} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
        {chart.pts.map((p, i) => (
          <circle key={i} cx={p.x} cy={p.y} r="2.5" fill={p.trade_pnl >= 0 ? "#b2fd02" : "#ff4d4d"} opacity="0.8" />
        ))}
      </svg>
      <div className="chart-scale">
        <span>MIN ${fmt(chart.min)}</span>
        <span>0</span>
        <span>MAX ${fmt(chart.max)}</span>
      </div>
      <style jsx>{`
        .eq-chart {
          border-top: 1px solid rgba(255, 255, 255, 0.08);
          border-bottom: 1px solid rgba(255, 255, 255, 0.08);
          padding: 20px 0 10px 0;
        }
        .chart-empty {
          padding: 60px 20px;
          text-align: center;
          color: rgba(255, 255, 255, 0.3);
          font-size: 12px;
          letter-spacing: 0.2em;
          border-top: 1px solid rgba(255, 255, 255, 0.08);
          border-bottom: 1px solid rgba(255, 255, 255, 0.08);
        }
        .chart-scale {
          display: flex;
          justify-content: space-between;
          font-size: 9px;
          letter-spacing: 0.15em;
          color: rgba(255, 255, 255, 0.3);
          margin-top: 6px;
          font-family: "JetBrains Mono", monospace;
        }
      `}</style>
    </div>
  );
}
