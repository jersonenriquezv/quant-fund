"use client";

import { useState, useCallback } from "react";
import { usePolling } from "@/lib/hooks";
import { patchApi, postApi, deleteApi, fetchApi } from "@/lib/api";
import type { ManualTrade } from "@/lib/api";

function fmt(n: number | null | undefined, d: number = 2): string {
  if (n == null) return "--";
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

function fmtDate(iso: string | null): string {
  if (!iso) return "--";
  return new Date(iso).toLocaleDateString("en", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

// ── Edit Modal — loads all trade fields, user modifies, saves via PATCH ──
function EditModal({ trade, onClose, onSaved }: { trade: ManualTrade; onClose: () => void; onSaved: () => void }) {
  const [sl, setSl] = useState(String(trade.sl_price ?? ""));
  const [tp1, setTp1] = useState(String(trade.tp1_price ?? ""));
  const [tp2, setTp2] = useState(String(trade.tp2_price ?? ""));
  const [entry, setEntry] = useState(String(trade.entry_price ?? ""));
  const [thesis, setThesis] = useState(trade.thesis ?? "");
  const [notes, setNotes] = useState(trade.notes ?? "");
  const [mistakes, setMistakes] = useState(trade.mistakes ?? "");
  const [saving, setSaving] = useState(false);

  const handleSave = async () => {
    setSaving(true);
    try {
      const body: Record<string, unknown> = {};
      if (entry && parseFloat(entry) !== trade.entry_price) body.entry_price = parseFloat(entry);
      if (sl && parseFloat(sl) !== trade.sl_price) body.sl_price = parseFloat(sl);
      if (tp1) body.tp1_price = parseFloat(tp1);
      if (tp2) body.tp2_price = parseFloat(tp2);
      if (thesis !== (trade.thesis ?? "")) body.thesis = thesis || null;
      if (notes !== (trade.notes ?? "")) body.notes = notes || null;
      if (mistakes !== (trade.mistakes ?? "")) body.mistakes = mistakes || null;
      if (Object.keys(body).length > 0) {
        await patchApi(`/manual/trades/${trade.id}`, body);
      }
      onSaved();
      onClose();
    } catch (e) {
      console.error("Save failed:", e);
    }
    setSaving(false);
  };

  return (
    <div className="manual-modal-overlay" onClick={onClose}>
      <div className="manual-modal" onClick={(e) => e.stopPropagation()}>
        <div className="manual-modal-title">Edit — {trade.pair} {trade.direction.toUpperCase()}</div>
        <div className="manual-modal-form">
          <div className="manual-calc-row">
            <div style={{ flex: 1 }}>
              <label className="manual-field-label">Entry</label>
              <input className="manual-input" type="number" value={entry} onChange={(e) => setEntry(e.target.value)} />
            </div>
            <div style={{ flex: 1 }}>
              <label className="manual-field-label">SL</label>
              <input className="manual-input" type="number" value={sl} onChange={(e) => setSl(e.target.value)} />
            </div>
          </div>
          <div className="manual-calc-row">
            <div style={{ flex: 1 }}>
              <label className="manual-field-label">TP1</label>
              <input className="manual-input" type="number" value={tp1} onChange={(e) => setTp1(e.target.value)} />
            </div>
            <div style={{ flex: 1 }}>
              <label className="manual-field-label">TP2</label>
              <input className="manual-input" type="number" value={tp2} onChange={(e) => setTp2(e.target.value)} />
            </div>
          </div>
          <div>
            <label className="manual-field-label">Thesis</label>
            <textarea className="manual-input manual-textarea" value={thesis} onChange={(e) => setThesis(e.target.value)} rows={2} />
          </div>
          <div>
            <label className="manual-field-label">Notes / Mistakes</label>
            <textarea className="manual-input manual-textarea" value={mistakes || notes} onChange={(e) => { setMistakes(e.target.value); setNotes(e.target.value); }} rows={2} />
          </div>
          <div className="manual-modal-actions">
            <button className="manual-btn manual-btn-calc" onClick={handleSave} disabled={saving}>
              {saving ? "Saving..." : "Save Changes"}
            </button>
            <button className="manual-btn" onClick={onClose}>Cancel</button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Partial Close / TP Hit / SL Hit modal ──
function PartialCloseModal({
  trade, defaultPrice, defaultPct, defaultLabel, onClose, onSaved,
}: {
  trade: ManualTrade; defaultPrice: number | null; defaultPct: number;
  defaultLabel: string; onClose: () => void; onSaved: () => void;
}) {
  const [pct, setPct] = useState(String(defaultPct));
  const [price, setPrice] = useState(String(defaultPrice ?? ""));
  const [pcNotes, setPcNotes] = useState(defaultLabel ? `${defaultLabel} hit` : "");
  const [saving, setSaving] = useState(false);

  const handleSubmit = async () => {
    if (!price) return;
    setSaving(true);
    try {
      await postApi(`/manual/trades/${trade.id}/partial-close`, {
        close_price: parseFloat(price),
        percentage: parseFloat(pct),
        notes: pcNotes || undefined,
      });
      onSaved();
      onClose();
    } catch (e) {
      console.error("Partial close failed:", e);
    }
    setSaving(false);
  };

  return (
    <div className="manual-modal-overlay" onClick={onClose}>
      <div className="manual-modal" onClick={(e) => e.stopPropagation()}>
        <div className="manual-modal-title">{defaultLabel || "Partial Close"} — {trade.pair}</div>
        <div className="manual-modal-form">
          <div className="manual-calc-row">
            <div style={{ flex: 1 }}>
              <label className="manual-field-label">Close %</label>
              <input className="manual-input" type="number" value={pct} onChange={(e) => setPct(e.target.value)} />
            </div>
            <div style={{ flex: 1 }}>
              <label className="manual-field-label">Price</label>
              <input className="manual-input" type="number" value={price} onChange={(e) => setPrice(e.target.value)} />
            </div>
          </div>
          <div>
            <label className="manual-field-label">Notes</label>
            <input className="manual-input" value={pcNotes} onChange={(e) => setPcNotes(e.target.value)} />
          </div>
          <button className="manual-btn manual-btn-calc" onClick={handleSubmit} disabled={saving} style={{ marginTop: 8 }}>
            {saving ? "..." : `Close ${pct}%`}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Activate Modal — set activation time ──
function ActivateModal({ trade, onClose, onSaved }: { trade: ManualTrade; onClose: () => void; onSaved: () => void }) {
  const now = new Date();
  const [date, setDate] = useState(now.toISOString().slice(0, 10));
  const [time, setTime] = useState(now.toTimeString().slice(0, 5));
  const [saving, setSaving] = useState(false);

  const handleActivate = async () => {
    setSaving(true);
    try {
      const activated_at = new Date(`${date}T${time}:00`).toISOString();
      await patchApi(`/manual/trades/${trade.id}`, { status: "active", activated_at });
      onSaved();
      onClose();
    } catch (e) {
      console.error("Activate failed:", e);
    }
    setSaving(false);
  };

  return (
    <div className="manual-modal-overlay" onClick={onClose}>
      <div className="manual-modal" onClick={(e) => e.stopPropagation()}>
        <div className="manual-modal-title">Activate — {trade.pair}</div>
        <div className="manual-modal-form">
          <div className="manual-calc-row">
            <div style={{ flex: 1 }}>
              <label className="manual-field-label">Date</label>
              <input className="manual-input" type="date" value={date} onChange={(e) => setDate(e.target.value)} />
            </div>
            <div style={{ flex: 1 }}>
              <label className="manual-field-label">Time</label>
              <input className="manual-input" type="time" value={time} onChange={(e) => setTime(e.target.value)} />
            </div>
          </div>
          <button className="manual-btn manual-btn-create" onClick={handleActivate} disabled={saving} style={{ marginTop: 8 }}>
            {saving ? "..." : "Activate"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Trade Card ──
function TradeCard({ trade, onRefresh }: { trade: ManualTrade; onRefresh: () => void }) {
  const [showEdit, setShowEdit] = useState(false);
  const [showPartial, setShowPartial] = useState<{ price: number | null; pct: number; label: string } | null>(null);
  const [showActivate, setShowActivate] = useState(false);
  const [expanded, setExpanded] = useState(false);

  const { data: priceData } = usePolling<{ price: number }>(
    `/manual/price/${encodeURIComponent(trade.pair)}`, 10000
  );
  const currentPrice = priceData?.price ?? null;
  const isLong = trade.direction === "long";

  // Unrealized PnL
  let unrealizedPct = 0;
  if (currentPrice && trade.entry_price > 0 && trade.status === "active") {
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
    ? Math.abs(trade.entry_price - trade.sl_price) / trade.entry_price * 100 : 0;
  const tp1Dist = trade.tp1_price && trade.entry_price > 0
    ? Math.abs(trade.tp1_price - trade.entry_price) / trade.entry_price * 100 : 0;

  // TP1 progress
  let tp1Progress = 0;
  if (currentPrice && trade.tp1_price && trade.entry_price && trade.status === "active") {
    const totalDist = Math.abs(trade.tp1_price - trade.entry_price);
    if (totalDist > 0) {
      const d = isLong ? currentPrice - trade.entry_price : trade.entry_price - currentPrice;
      tp1Progress = Math.max(0, Math.min(100, (d / totalDist) * 100));
    }
  }

  // Partial close progress
  const closedPct = trade.partial_closes?.reduce((s, pc) => s + pc.close_pct, 0) ?? 0;

  const handleDelete = async () => {
    try { await deleteApi(`/manual/trades/${trade.id}`); onRefresh(); }
    catch (e) { console.error(e); }
  };

  const handleCancel = async () => {
    try { await patchApi(`/manual/trades/${trade.id}`, { status: "cancelled" }); onRefresh(); }
    catch (e) { console.error(e); }
  };

  return (
    <>
      <div
        className={`manual-trade-card ${trade.status === "planned" ? "trade-planned" : isProfit ? "trade-profit" : "trade-loss"}`}
        onClick={() => setExpanded(!expanded)}
      >
        {/* Header */}
        <div className="manual-trade-header">
          <div style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
            <span className={`badge ${isLong ? "badge-long" : "badge-short"}`}>
              {isLong ? "L" : "S"}
            </span>
            <span style={{ fontWeight: 600, whiteSpace: "nowrap" }}>{trade.pair}</span>
            {trade.status === "planned" && <span className="badge badge-planned">PENDING</span>}
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

        {/* Meta line */}
        <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 8 }}>
          {fmtDate(trade.activated_at || trade.created_at)} · E ${fmt(trade.entry_price)} · SL ${fmt(trade.sl_price)} · {trade.rr_ratio ? trade.rr_ratio.toFixed(1) : "--"}R · ${fmt(trade.risk_usd)} risk
        </div>

        {/* Levels grid */}
        <div className="manual-trade-levels">
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
            </span>
          </div>
          <div className="manual-trade-level">
            <span className="manual-trade-level-label">Size</span>
            <span>{fmt(trade.position_size, 4)}</span>
          </div>
          <div className="manual-trade-level">
            <span className="manual-trade-level-label">Current</span>
            <span style={{ color: trade.status === "active" ? (isProfit ? "var(--long)" : "var(--short)") : "var(--text-secondary)" }}>
              {currentPrice ? fmt(currentPrice) : "--"}
            </span>
          </div>
        </div>

        {/* TP1 progress / partial close progress */}
        {trade.status === "active" && (
          <div className="manual-tp-progress">
            <div className="manual-tp-progress-bar" style={{
              width: `${closedPct > 0 ? closedPct : tp1Progress}%`,
              background: closedPct > 0 ? "var(--accent)" : (isProfit ? "var(--long)" : "var(--short)"),
            }} />
          </div>
        )}
        {closedPct > 0 && (
          <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 2 }}>
            {closedPct}% closed ({trade.partial_closes?.length} partial{trade.partial_closes?.length !== 1 ? "s" : ""})
          </div>
        )}

        {/* Thesis preview */}
        {trade.thesis && (
          <div style={{ fontSize: 11, color: "var(--text-secondary)", marginTop: 6 }}>
            {trade.thesis.length > 80 && !expanded ? trade.thesis.slice(0, 80) + "..." : trade.thesis}
          </div>
        )}

        {/* Expanded detail + actions */}
        {expanded && (
          <div className="manual-card-actions" onClick={(e) => e.stopPropagation()}>
            {/* Planned actions */}
            {trade.status === "planned" && (
              <div className="manual-action-row">
                <button className="manual-btn manual-btn-calc" onClick={() => setShowActivate(true)}>Activate</button>
                <button className="manual-btn" onClick={() => setShowEdit(true)}>Edit</button>
                <button className="manual-btn manual-btn-close" onClick={handleCancel}>Cancel</button>
                <button className="manual-btn manual-btn-del" onClick={handleDelete}>Delete</button>
              </div>
            )}
            {/* Active actions */}
            {trade.status === "active" && (
              <div className="manual-action-row">
                {closedPct < 50 && trade.tp1_price && (
                  <button className="manual-btn manual-btn-create"
                    onClick={() => setShowPartial({ price: trade.tp1_price, pct: 50, label: "TP1" })}>
                    TP1 Hit
                  </button>
                )}
                {closedPct >= 50 && closedPct < 100 && (
                  <button className="manual-btn manual-btn-create"
                    onClick={() => setShowPartial({ price: trade.tp2_price || trade.tp1_price, pct: 100, label: "TP2" })}>
                    TP2 Hit
                  </button>
                )}
                <button className="manual-btn manual-btn-close"
                  onClick={() => setShowPartial({ price: trade.sl_price, pct: 100, label: "SL" })}>
                  Stopped
                </button>
                <button className="manual-btn"
                  onClick={() => setShowPartial({ price: currentPrice, pct: 100, label: "Manual Exit" })}>
                  Exit
                </button>
                <button className="manual-btn" onClick={() => setShowEdit(true)}>Edit</button>
              </div>
            )}
            {/* Closed actions */}
            {trade.status === "closed" && (
              <div className="manual-action-row">
                <button className="manual-btn" onClick={() => setShowEdit(true)}>Add Notes</button>
              </div>
            )}
          </div>
        )}

        {/* Footer */}
        <div className="manual-trade-footer">
          <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
            {trade.setup_type || "manual"} · {trade.leverage}x · {trade.timeframe || "--"}
          </span>
          <span style={{ fontSize: 10, color: "var(--text-muted)" }}>
            tap to {expanded ? "collapse" : "expand"}
          </span>
        </div>
      </div>

      {showEdit && <EditModal trade={trade} onClose={() => setShowEdit(false)} onSaved={onRefresh} />}
      {showPartial && (
        <PartialCloseModal
          trade={trade}
          defaultPrice={showPartial.price}
          defaultPct={showPartial.pct}
          defaultLabel={showPartial.label}
          onClose={() => setShowPartial(null)}
          onSaved={onRefresh}
        />
      )}
      {showActivate && <ActivateModal trade={trade} onClose={() => setShowActivate(false)} onSaved={onRefresh} />}
    </>
  );
}

// ── Main component ──
export function ActiveTrades() {
  const { data: active, loading } = usePolling<ManualTrade[]>(
    "/manual/trades?status=active&limit=20", 10000
  );
  const { data: planned } = usePolling<ManualTrade[]>(
    "/manual/trades?status=planned&limit=10", 15000
  );
  const [refreshKey, setRefreshKey] = useState(0);
  const refresh = useCallback(() => setRefreshKey((k) => k + 1), []);

  const activeTrades = active ?? [];
  const plannedTrades = planned ?? [];

  return (
    <div>
      <div className="card-title">
        Positions
        {activeTrades.length > 0 && (
          <span style={{ color: "var(--text-secondary)", marginLeft: 8, fontSize: 11 }}>
            {activeTrades.length} active
          </span>
        )}
        {plannedTrades.length > 0 && (
          <span style={{ color: "var(--warning)", marginLeft: 8, fontSize: 11 }}>
            {plannedTrades.length} pending
          </span>
        )}
      </div>

      {loading && activeTrades.length === 0 && plannedTrades.length === 0 ? (
        <div className="skeleton" style={{ height: 100, width: "100%" }} />
      ) : activeTrades.length === 0 && plannedTrades.length === 0 ? (
        <div style={{ color: "var(--text-muted)", textAlign: "center", padding: 24, fontSize: 13 }}>
          No active or pending trades
        </div>
      ) : (
        <div className="manual-active-grid">
          {activeTrades.map((t) => <TradeCard key={t.id} trade={t} onRefresh={refresh} />)}
          {plannedTrades.map((t) => <TradeCard key={t.id} trade={t} onRefresh={refresh} />)}
        </div>
      )}
    </div>
  );
}
