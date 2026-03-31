"use client";

import { useState } from "react";
import { usePolling } from "@/lib/hooks";
import { patchApi, postApi, deleteApi } from "@/lib/api";
import type { ManualTrade } from "@/lib/api";

function fmt(n: number | null | undefined, d: number = 2): string {
  if (n == null) return "--";
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

function timeAgo(ts: string | null): string {
  if (!ts) return "--";
  const diff = Date.now() - new Date(ts).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

function EditModal({ trade, onClose }: { trade: ManualTrade; onClose: () => void }) {
  const [sl, setSl] = useState(String(trade.sl_price));
  const [tp1, setTp1] = useState(String(trade.tp1_price ?? ""));
  const [tp2, setTp2] = useState(String(trade.tp2_price ?? ""));
  const [thesis, setThesis] = useState(trade.thesis ?? "");
  const [notes, setNotes] = useState(trade.notes ?? "");
  const [saving, setSaving] = useState(false);

  const handleSave = async () => {
    setSaving(true);
    try {
      await patchApi(`/manual/trades/${trade.id}`, {
        sl_price: parseFloat(sl) || undefined,
        tp1_price: tp1 ? parseFloat(tp1) : undefined,
        tp2_price: tp2 ? parseFloat(tp2) : undefined,
        thesis: thesis || undefined,
        notes: notes || undefined,
      });
      onClose();
    } catch (e) {
      console.error("Save failed:", e);
    }
    setSaving(false);
  };

  return (
    <div className="manual-modal-overlay" onClick={onClose}>
      <div className="manual-modal" onClick={(e) => e.stopPropagation()}>
        <div className="manual-modal-title">Edit {trade.pair} {trade.direction.toUpperCase()}</div>
        <div className="manual-modal-form">
          <div className="manual-calc-row">
            <div style={{ flex: 1 }}>
              <label className="manual-stat-label">SL Price</label>
              <input className="manual-input" type="number" value={sl} onChange={(e) => setSl(e.target.value)} />
            </div>
            <div style={{ flex: 1 }}>
              <label className="manual-stat-label">TP1</label>
              <input className="manual-input" type="number" value={tp1} onChange={(e) => setTp1(e.target.value)} />
            </div>
            <div style={{ flex: 1 }}>
              <label className="manual-stat-label">TP2</label>
              <input className="manual-input" type="number" value={tp2} onChange={(e) => setTp2(e.target.value)} />
            </div>
          </div>
          <div>
            <label className="manual-stat-label">Thesis</label>
            <textarea className="manual-input manual-textarea" value={thesis} onChange={(e) => setThesis(e.target.value)} rows={2} />
          </div>
          <div>
            <label className="manual-stat-label">Notes</label>
            <textarea className="manual-input manual-textarea" value={notes} onChange={(e) => setNotes(e.target.value)} rows={2} />
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
            <button className="manual-btn manual-btn-calc" onClick={handleSave} disabled={saving}>
              {saving ? "Saving..." : "Save"}
            </button>
            <button className="manual-btn" onClick={onClose}>Cancel</button>
          </div>
        </div>
      </div>
    </div>
  );
}

function PartialCloseModal({ trade, currentPrice, onClose }: { trade: ManualTrade; currentPrice: number | null; onClose: () => void }) {
  const [pct, setPct] = useState("50");
  const [price, setPrice] = useState(String(currentPrice ?? ""));
  const [pcNotes, setPcNotes] = useState("");
  const [saving, setSaving] = useState(false);

  const handlePartialClose = async () => {
    if (!price) return;
    setSaving(true);
    try {
      await postApi(`/manual/trades/${trade.id}/partial-close`, {
        close_price: parseFloat(price),
        close_pct: parseFloat(pct),
        notes: pcNotes || undefined,
      });
      onClose();
    } catch (e) {
      console.error("Partial close failed:", e);
    }
    setSaving(false);
  };

  return (
    <div className="manual-modal-overlay" onClick={onClose}>
      <div className="manual-modal" onClick={(e) => e.stopPropagation()}>
        <div className="manual-modal-title">Partial Close {trade.pair}</div>
        <div className="manual-modal-form">
          <div className="manual-calc-row">
            <div style={{ flex: 1 }}>
              <label className="manual-stat-label">Close %</label>
              <input className="manual-input" type="number" value={pct} onChange={(e) => setPct(e.target.value)} />
            </div>
            <div style={{ flex: 1 }}>
              <label className="manual-stat-label">Price</label>
              <input className="manual-input" type="number" value={price} onChange={(e) => setPrice(e.target.value)} />
            </div>
          </div>
          <div>
            <label className="manual-stat-label">Notes</label>
            <input className="manual-input" value={pcNotes} onChange={(e) => setPcNotes(e.target.value)} />
          </div>
          <button className="manual-btn manual-btn-calc" onClick={handlePartialClose} disabled={saving} style={{ marginTop: 8 }}>
            {saving ? "..." : `Close ${pct}%`}
          </button>
        </div>
      </div>
    </div>
  );
}

function TradeCard({ trade }: { trade: ManualTrade }) {
  const [closing, setClosing] = useState(false);
  const [showEdit, setShowEdit] = useState(false);
  const [showPartial, setShowPartial] = useState(false);
  const [expanded, setExpanded] = useState(false);

  const { data: priceData } = usePolling<{ price: number }>(
    `/manual/price/${encodeURIComponent(trade.pair)}`, 10000
  );
  const currentPrice = priceData?.price ?? null;

  // Unrealized PnL
  let unrealizedPct = 0;
  const isLong = trade.direction === "long";
  if (currentPrice && trade.entry_price > 0) {
    unrealizedPct = isLong
      ? ((currentPrice - trade.entry_price) / trade.entry_price) * 100
      : ((trade.entry_price - currentPrice) / trade.entry_price) * 100;
  }
  const unrealizedUsd = trade.position_value_usd
    ? (unrealizedPct / 100) * trade.position_value_usd
    : null;
  const isProfit = unrealizedPct >= 0;

  // Distances
  const slDist = trade.entry_price > 0
    ? Math.abs(trade.entry_price - trade.sl_price) / trade.entry_price * 100
    : 0;
  const tp1Dist = trade.entry_price > 0 && trade.tp1_price
    ? Math.abs(trade.tp1_price - trade.entry_price) / trade.entry_price * 100
    : 0;
  const tp2Dist = trade.entry_price > 0 && trade.tp2_price
    ? Math.abs(trade.tp2_price - trade.entry_price) / trade.entry_price * 100
    : 0;

  // TP1 progress
  let tp1Progress = 0;
  if (currentPrice && trade.tp1_price && trade.entry_price) {
    const totalDist = Math.abs(trade.tp1_price - trade.entry_price);
    if (totalDist > 0) {
      const currentDist = isLong
        ? currentPrice - trade.entry_price
        : trade.entry_price - currentPrice;
      tp1Progress = Math.max(0, Math.min(100, (currentDist / totalDist) * 100));
    }
  }

  // R:R display
  const rr = trade.rr_ratio ? trade.rr_ratio.toFixed(1) : "--";

  const handleClose = async () => {
    if (!currentPrice || closing) return;
    setClosing(true);
    try {
      await patchApi(`/manual/trades/${trade.id}`, { status: "closed", close_price: currentPrice });
    } catch (e) { console.error("Close failed:", e); }
    setClosing(false);
  };

  const handleCancel = async () => {
    try {
      await patchApi(`/manual/trades/${trade.id}`, { status: "cancelled" });
    } catch (e) { console.error("Cancel failed:", e); }
  };

  const handleActivate = async () => {
    try {
      await patchApi(`/manual/trades/${trade.id}`, { status: "active" });
    } catch (e) { console.error("Activate failed:", e); }
  };

  const handleDelete = async () => {
    try {
      await deleteApi(`/manual/trades/${trade.id}`);
    } catch (e) { console.error("Delete failed:", e); }
  };

  return (
    <>
      <div className={`manual-trade-card ${trade.status === "planned" ? "" : isProfit ? "trade-profit" : "trade-loss"}`}>
        <div className="manual-trade-header">
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span className={`badge ${isLong ? "badge-long" : "badge-short"}`}>
              {trade.direction.toUpperCase()}
            </span>
            <span style={{ fontWeight: 600 }}>{trade.pair}</span>
            {trade.status === "planned" && <span className="badge badge-planned">PLANNED</span>}
          </div>
          {trade.status === "active" && (
            <span className={`manual-trade-pnl ${isProfit ? "pnl-positive" : "pnl-negative"}`}>
              {isProfit ? "+" : ""}{unrealizedPct.toFixed(2)}%
              {unrealizedUsd != null && (
                <span style={{ fontSize: 11, opacity: 0.7, marginLeft: 4 }}>
                  {unrealizedUsd >= 0 ? "+" : ""}${fmt(unrealizedUsd)}
                </span>
              )}
            </span>
          )}
        </div>

        <div className="manual-trade-levels">
          <div className="manual-trade-level">
            <span className="manual-trade-level-label">Entry</span>
            <span>{fmt(trade.entry_price)}</span>
          </div>
          <div className="manual-trade-level">
            <span className="manual-trade-level-label">Current</span>
            <span style={{ color: isProfit ? "var(--long)" : "var(--short)" }}>
              {currentPrice ? fmt(currentPrice) : "--"}
            </span>
          </div>
          <div className="manual-trade-level">
            <span className="manual-trade-level-label">SL</span>
            <span style={{ color: "var(--short)" }}>
              {fmt(trade.sl_price)} <span style={{ opacity: 0.5, fontSize: 10 }}>-{slDist.toFixed(1)}%</span>
            </span>
          </div>
          <div className="manual-trade-level">
            <span className="manual-trade-level-label">R:R</span>
            <span>{rr}</span>
          </div>
        </div>

        {/* TP levels row */}
        <div className="manual-trade-levels" style={{ marginTop: 4 }}>
          <div className="manual-trade-level">
            <span className="manual-trade-level-label">TP1</span>
            <span style={{ color: "var(--long)" }}>
              {trade.tp1_price ? fmt(trade.tp1_price) : "--"}
              {tp1Dist > 0 && <span style={{ opacity: 0.5, fontSize: 10 }}> +{tp1Dist.toFixed(1)}%</span>}
            </span>
          </div>
          <div className="manual-trade-level">
            <span className="manual-trade-level-label">TP2</span>
            <span style={{ color: "var(--long)" }}>
              {trade.tp2_price ? fmt(trade.tp2_price) : "--"}
              {tp2Dist > 0 && <span style={{ opacity: 0.5, fontSize: 10 }}> +{tp2Dist.toFixed(1)}%</span>}
            </span>
          </div>
          <div className="manual-trade-level">
            <span className="manual-trade-level-label">Size</span>
            <span>{fmt(trade.position_size, 4)}</span>
          </div>
          <div className="manual-trade-level">
            <span className="manual-trade-level-label">Risk</span>
            <span style={{ color: "var(--short)" }}>${fmt(trade.risk_usd)}</span>
          </div>
        </div>

        {/* TP1 progress bar */}
        {trade.status === "active" && (
          <div className="manual-tp-progress">
            <div
              className="manual-tp-progress-bar"
              style={{ width: `${tp1Progress}%`, background: isProfit ? "var(--long)" : "var(--short)" }}
            />
          </div>
        )}

        {/* Thesis/notes preview */}
        {trade.thesis && (
          <div style={{ fontSize: 11, color: "var(--text-secondary)", marginTop: 4, cursor: "pointer" }} onClick={() => setExpanded(!expanded)}>
            {expanded ? trade.thesis : trade.thesis.slice(0, 60) + (trade.thesis.length > 60 ? "..." : "")}
          </div>
        )}

        {/* Partial closes summary */}
        {trade.partial_closes && trade.partial_closes.length > 0 && (
          <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 4 }}>
            {trade.partial_closes.length} partial close(s) —{" "}
            {trade.partial_closes.reduce((s, pc) => s + pc.close_pct, 0)}% closed
          </div>
        )}

        {/* Footer with actions */}
        <div className="manual-trade-footer">
          <div style={{ display: "flex", gap: 8, fontSize: 11, color: "var(--text-muted)" }}>
            <span>{trade.setup_type || "manual"}</span>
            <span>{trade.leverage}x</span>
            <span>{timeAgo(trade.activated_at || trade.created_at)}</span>
          </div>
          <div style={{ display: "flex", gap: 4 }}>
            <button className="manual-btn" onClick={() => setShowEdit(true)} title="Edit">
              Edit
            </button>
            {trade.status === "active" && (
              <>
                <button className="manual-btn" onClick={() => setShowPartial(true)} title="Partial Close">
                  Partial
                </button>
                <button className="manual-btn manual-btn-close" onClick={handleClose} disabled={closing || !currentPrice}>
                  {closing ? "..." : "Close"}
                </button>
              </>
            )}
            {trade.status === "planned" && (
              <>
                <button className="manual-btn manual-btn-create" style={{ padding: "4px 8px", marginTop: 0, width: "auto" }} onClick={handleActivate}>
                  Activate
                </button>
                <button className="manual-btn manual-btn-close" onClick={handleCancel}>Cancel</button>
              </>
            )}
            <button className="manual-btn" onClick={handleDelete} style={{ color: "var(--text-muted)", fontSize: 10 }} title="Delete">
              Del
            </button>
          </div>
        </div>
      </div>

      {showEdit && <EditModal trade={trade} onClose={() => setShowEdit(false)} />}
      {showPartial && <PartialCloseModal trade={trade} currentPrice={currentPrice} onClose={() => setShowPartial(false)} />}
    </>
  );
}

export function ActiveTrades() {
  const { data: active, loading } = usePolling<ManualTrade[]>(
    "/manual/trades?status=active&limit=20", 10000
  );
  const { data: planned } = usePolling<ManualTrade[]>(
    "/manual/trades?status=planned&limit=10", 15000
  );

  const activeTrades = active ?? [];
  const plannedTrades = planned ?? [];
  const allTrades = [...activeTrades, ...plannedTrades];

  return (
    <div>
      <div className="card-title">
        Active Positions
        {activeTrades.length > 0 && (
          <span style={{ fontSize: 11, color: "var(--text-secondary)", marginLeft: 8 }}>
            {activeTrades.length} active
          </span>
        )}
        {plannedTrades.length > 0 && (
          <span style={{ fontSize: 11, color: "var(--warning)", marginLeft: 8 }}>
            {plannedTrades.length} planned
          </span>
        )}
      </div>
      {loading && allTrades.length === 0 ? (
        <div className="skeleton" style={{ height: 100, width: "100%" }} />
      ) : allTrades.length === 0 ? (
        <div style={{ color: "var(--text-muted)", textAlign: "center", padding: 24, fontSize: 13 }}>
          No active trades
        </div>
      ) : (
        <div className="manual-active-grid">
          {allTrades.map((t) => (
            <TradeCard key={t.id} trade={t} />
          ))}
        </div>
      )}
    </div>
  );
}
