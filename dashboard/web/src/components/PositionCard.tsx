"use client";

import { useState } from "react";
import type { WSMessage, PositionData } from "@/lib/api";
import { postApi } from "@/lib/api";

function fmt(n: number | null | undefined, d: number = 2): string {
  if (n == null) return "--";
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

function timeAgo(ts: number): string {
  const now = Math.floor(Date.now() / 1000);
  const diff = now - ts;
  if (diff < 60) return `${diff}s`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  const h = Math.floor(diff / 3600);
  const m = Math.floor((diff % 3600) / 60);
  return `${h}h ${m}m`;
}


function CancelButton({ pair }: { pair: string }) {
  const [confirming, setConfirming] = useState(false);
  const [cancelling, setCancelling] = useState(false);

  const doCancel = async () => {
    setCancelling(true);
    try {
      await postApi(`/trades/${encodeURIComponent(pair)}/cancel`, {});
    } catch {
      // Will disappear on next WS update if successful
    }
  };

  if (cancelling) {
    return (
      <button className="btn-cancel" disabled>Cancelling...</button>
    );
  }

  if (confirming) {
    return (
      <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
        <span style={{ fontSize: 11, color: "var(--text-secondary)" }}>Sure?</span>
        <button className="btn-confirm" onClick={doCancel}>Yes</button>
        <button className="btn-nevermind" onClick={() => setConfirming(false)}>No</button>
      </div>
    );
  }

  return (
    <button className="btn-cancel" onClick={() => setConfirming(true)}>Cancel</button>
  );
}

/* ── Pending entry: compact one-liner ── */
function PendingRow({ pos }: { pos: PositionData }) {
  const isLong = pos.direction === "long";

  return (
    <div className="pending-row animate-in">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", flex: 1, minWidth: 0 }}>
          <span style={{ fontWeight: 700, fontSize: 13 }}>{pos.pair}</span>
          <span className={`badge ${isLong ? "badge-long" : "badge-short"}`}>{pos.direction}</span>
          <span className="badge badge-neutral" style={{ fontSize: 9 }}>{pos.setup_type}</span>
          <span className="pending-label">PENDING</span>
        </div>
        <span style={{ fontSize: 11, color: "var(--text-muted)", fontVariantNumeric: "tabular-nums", marginRight: 8 }}>
          {timeAgo(pos.created_at)}
        </span>
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 6 }}>
        <div style={{ display: "flex", gap: 16, fontSize: 12 }}>
          <span>
            <span style={{ color: "var(--text-muted)", fontSize: 10 }}>ENTRY </span>
            <span className="num">{fmt(pos.entry_price)}</span>
          </span>
          <span>
            <span style={{ color: "var(--text-muted)", fontSize: 10 }}>SL </span>
            <span className="num" style={{ color: "var(--short)" }}>{fmt(pos.sl_price)}</span>
          </span>
          <span>
            <span style={{ color: "var(--text-muted)", fontSize: 10 }}>TP </span>
            <span className="num" style={{ color: "var(--long)" }}>{fmt(pos.tp_price ?? pos.tp2_price)}</span>
          </span>
          <span className="pending-lev">
            <span style={{ color: "var(--text-muted)", fontSize: 10 }}>LEV </span>
            <span className="num">{pos.leverage}x</span>
          </span>
        </div>
        <CancelButton pair={pos.pair} />
      </div>
    </div>
  );
}

/* ── Active position: full card ── */
function ActivePosition({ pos }: { pos: PositionData }) {
  const isLong = pos.direction === "long";
  const pnlPct = pos.pnl_pct * 100;
  const pnlClass = pnlPct >= 0 ? "pnl-positive" : "pnl-negative";
  const entryPrice = pos.actual_entry_price ?? pos.entry_price;
  const pnlUsd = pos.filled_size > 0 && entryPrice > 0
    ? entryPrice * pos.filled_size * pos.pnl_pct
    : null;

  return (
    <div className={`position-card animate-in${pnlPct >= 0 ? " position-card-winning" : ""}`} style={{
      borderColor: isLong ? "rgba(16,185,129,0.15)" : "rgba(239,68,68,0.15)",
    }}>
      {/* Row 1: Pair + direction + setup + time */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
          <span style={{ fontWeight: 700 }}>{pos.pair}</span>
          <span className={`badge ${isLong ? "badge-long" : "badge-short"}`}>{pos.direction}</span>
          <span className="badge badge-neutral" style={{ fontSize: 9 }}>{pos.setup_type}</span>
          {pos.breakeven_hit && (
            <span className="badge badge-neutral" style={{ fontSize: 9, background: "rgba(16,185,129,0.12)", color: "var(--long)" }}>BE</span>
          )}
        </div>
        <span style={{ fontSize: 11, color: "var(--text-muted)", fontVariantNumeric: "tabular-nums" }}>
          {timeAgo(pos.filled_at ?? pos.created_at)}
        </span>
      </div>

      {/* Row 2: P&L */}
      <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 10 }}>
        <span className={pnlClass} style={{ fontWeight: 700, fontSize: 20, fontVariantNumeric: "tabular-nums" }}>
          {pnlPct >= 0 ? "+" : ""}{pnlPct.toFixed(2)}%
        </span>
        {pnlUsd != null && (
          <span className={pnlClass} style={{ fontSize: 12, fontVariantNumeric: "tabular-nums" }}>
            {pnlUsd >= 0 ? "+" : ""}${fmt(Math.abs(pnlUsd))}
          </span>
        )}
      </div>

      {/* Row 3: grid — Entry, SL, TP, Leverage */}
      <div className="position-grid" style={{ marginBottom: 10 }}>
        <div>
          <div style={{ color: "var(--text-muted)", fontSize: 10 }}>ENTRY</div>
          <div className="num">{fmt(entryPrice)}</div>
        </div>
        <div>
          <div style={{ color: "var(--text-muted)", fontSize: 10 }}>SL</div>
          <div className="num" style={{ color: "var(--short)" }}>{fmt(pos.sl_price)}</div>
        </div>
        <div>
          <div style={{ color: "var(--text-muted)", fontSize: 10 }}>TP</div>
          <div className="num" style={{ color: "var(--long)" }}>{fmt(pos.tp_price ?? pos.tp2_price)}</div>
        </div>
        <div>
          <div style={{ color: "var(--text-muted)", fontSize: 10 }}>LEV</div>
          <div className="num">{pos.leverage}x</div>
        </div>
      </div>

      {/* Row 4: Cancel */}
      <div className="position-footer" style={{ display: "flex", justifyContent: "flex-end", alignItems: "center" }}>
        <CancelButton pair={pos.pair} />
      </div>
    </div>
  );
}

export function PositionCard({ ws }: { ws: WSMessage | null }) {
  const positions = ws?.positions ?? [];
  const pending = positions.filter(p => p.phase === "pending_entry");
  const active = positions.filter(p => p.phase !== "pending_entry" && p.phase !== "closed");

  return (
    <div>
      <div className="card-title">Open Positions</div>
      {positions.length === 0 ? (
        <div style={{ color: "var(--text-muted)", fontSize: 13, padding: "20px 0", textAlign: "center" }}>
          No open positions
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {active.map((p, i) => <ActivePosition key={`active-${p.pair}-${i}`} pos={p} />)}
          {pending.length > 0 && active.length > 0 && (
            <div style={{ fontSize: 10, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.1em", marginTop: 4 }}>
              Pending Orders
            </div>
          )}
          {pending.map((p, i) => <PendingRow key={`pending-${p.pair}-${i}`} pos={p} />)}
        </div>
      )}
    </div>
  );
}
