"use client";

import { usePolling } from "@/lib/hooks";
import type { MarketData, WSMessage } from "@/lib/api";

function fmt(n: number | null | undefined, decimals: number = 2): string {
  if (n == null) return "--";
  return n.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function fmtCompact(n: number | null | undefined): string {
  if (n == null) return "--";
  if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(2) + "M";
  return fmt(n);
}

function fmtPct(n: number | null | undefined): string {
  if (n == null) return "--";
  const sign = n >= 0 ? "+" : "";
  return sign + n.toFixed(4) + "%";
}

export function PricePanel({ pair, ws }: { pair: string; ws: WSMessage | null }) {
  const { data: market } = usePolling<MarketData>(`/market/${encodeURIComponent(pair)}`, 5000);

  // Prefer WebSocket price if available
  const wsPrice = ws?.prices?.[pair]?.price;
  const price = wsPrice ?? market?.price;

  const changePct = market?.change_pct;
  const isPositive = (changePct ?? 0) >= 0;

  return (
    <div>
      <div className="card-title">{pair}</div>

      <div style={{ fontSize: 28, fontWeight: 700, marginBottom: 4, fontVariantNumeric: "tabular-nums" }}>
        ${price != null ? fmt(price, pair.startsWith("BTC") ? 1 : 2) : "--"}
      </div>

      <div style={{ fontSize: 13, color: isPositive ? "var(--long)" : "var(--short)", marginBottom: 16 }}>
        {changePct != null ? (isPositive ? "+" : "") + changePct.toFixed(2) + "%" : "--"}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px 16px", fontSize: 12 }}>
        <div>
          <span style={{ color: "var(--text-muted)" }}>Funding</span>
          <div className="num" style={{ color: market?.funding_rate != null ? (market.funding_rate >= 0 ? "var(--long)" : "var(--short)") : "var(--text-secondary)" }}>
            {fmtPct(market?.funding_rate != null ? market.funding_rate * 100 : null)}
          </div>
        </div>
        <div>
          <span style={{ color: "var(--text-muted)" }}>OI</span>
          <div className="num">${fmtCompact(market?.oi_usd)}</div>
        </div>
      </div>
    </div>
  );
}
