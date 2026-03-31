"use client";

import { useState } from "react";
import { usePolling } from "@/lib/hooks";
import { patchApi, fetchApi } from "@/lib/api";
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

function TradeCard({ trade }: { trade: ManualTrade }) {
  const [closing, setClosing] = useState(false);
  const [price, setPrice] = useState<number | null>(null);

  // Fetch current price on mount
  const { data: priceData } = usePolling<{ price: number }>(
    `/manual/price/${encodeURIComponent(trade.pair)}`, 10000
  );
  const currentPrice = priceData?.price ?? price;

  // Unrealized PnL calculation
  let unrealizedPct = 0;
  if (currentPrice && trade.entry_price > 0) {
    if (trade.direction === "long") {
      unrealizedPct = ((currentPrice - trade.entry_price) / trade.entry_price) * 100;
    } else {
      unrealizedPct = ((trade.entry_price - currentPrice) / trade.entry_price) * 100;
    }
  }
  const unrealizedUsd = trade.position_value_usd
    ? (unrealizedPct / 100) * trade.position_value_usd
    : null;
  const isProfit = unrealizedPct >= 0;
  const isLong = trade.direction === "long";

  // SL/TP distances
  const slDist = trade.entry_price > 0
    ? Math.abs(trade.entry_price - trade.sl_price) / trade.entry_price * 100
    : 0;

  // Progress toward TP1 (0 = at entry, 100 = at TP1)
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

  const handleClose = async () => {
    if (!currentPrice || closing) return;
    setClosing(true);
    try {
      await patchApi(`/manual/trades/${trade.id}`, {
        status: "closed",
        close_price: currentPrice,
      });
    } catch (e) {
      console.error("Close failed:", e);
    }
    setClosing(false);
  };

  return (
    <div className={`manual-trade-card ${isProfit ? "trade-profit" : "trade-loss"}`}>
      <div className="manual-trade-header">
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span className={`badge ${isLong ? "badge-long" : "badge-short"}`}>
            {trade.direction.toUpperCase()}
          </span>
          <span style={{ fontWeight: 600 }}>{trade.pair}</span>
          {trade.status === "planned" && (
            <span className="badge badge-planned">PLANNED</span>
          )}
        </div>
        <span className={`manual-trade-pnl ${isProfit ? "pnl-positive" : "pnl-negative"}`}>
          {isProfit ? "+" : ""}{unrealizedPct.toFixed(2)}%
          {unrealizedUsd != null && (
            <span style={{ fontSize: 11, opacity: 0.7, marginLeft: 4 }}>
              {unrealizedUsd >= 0 ? "+" : ""}${fmt(unrealizedUsd)}
            </span>
          )}
        </span>
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
            {fmt(trade.sl_price)} <span style={{ opacity: 0.5 }}>({slDist.toFixed(1)}%)</span>
          </span>
        </div>
        <div className="manual-trade-level">
          <span className="manual-trade-level-label">TP1</span>
          <span style={{ color: "var(--long)" }}>
            {trade.tp1_price ? fmt(trade.tp1_price) : "--"}
          </span>
        </div>
      </div>

      {/* TP1 progress bar */}
      <div className="manual-tp-progress">
        <div
          className="manual-tp-progress-bar"
          style={{
            width: `${tp1Progress}%`,
            background: isProfit ? "var(--long)" : "var(--short)",
          }}
        />
      </div>

      <div className="manual-trade-footer">
        <div style={{ display: "flex", gap: 8, fontSize: 11, color: "var(--text-muted)" }}>
          <span>{trade.setup_type || "manual"}</span>
          <span>{trade.leverage}x</span>
          <span>{timeAgo(trade.activated_at || trade.created_at)}</span>
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          <button
            className="manual-btn manual-btn-close"
            onClick={handleClose}
            disabled={closing || !currentPrice}
          >
            {closing ? "..." : "Close"}
          </button>
        </div>
      </div>
    </div>
  );
}

export function ActiveTrades() {
  const { data: active, loading } = usePolling<ManualTrade[]>(
    "/manual/trades?status=active&limit=20", 10000
  );
  const { data: planned } = usePolling<ManualTrade[]>(
    "/manual/trades?status=planned&limit=10", 15000
  );

  const trades = [...(active ?? []), ...(planned ?? [])];

  return (
    <div>
      <div className="card-title">Active Positions</div>
      {loading && trades.length === 0 ? (
        <div className="skeleton" style={{ height: 100, width: "100%" }} />
      ) : trades.length === 0 ? (
        <div style={{ color: "var(--text-muted)", textAlign: "center", padding: 24, fontSize: 13 }}>
          No active trades
        </div>
      ) : (
        <div className="manual-active-grid">
          {trades.map((t) => (
            <TradeCard key={t.id} trade={t} />
          ))}
        </div>
      )}
    </div>
  );
}
