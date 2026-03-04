"use client";

import { usePolling } from "@/lib/hooks";
import type { OrderBlockData, WSMessage } from "@/lib/api";

function formatTime(ts: number): string {
  try {
    const d = new Date(ts);
    return d.toISOString().replace("T", " ").slice(5, 16);
  } catch {
    return "--";
  }
}

function fmt(n: number, decimals: number = 2): string {
  return n.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

export function OrderBlockPanel({ ws }: { ws: WSMessage | null }) {
  const { data: obs, loading } = usePolling<OrderBlockData[]>("/strategy/order-blocks", 10000);

  // Compute distance % from current price to each OB entry
  const enriched = obs?.map((ob) => {
    const wsPrice = ws?.prices?.[ob.pair]?.price;
    const price = wsPrice ?? null;
    const distancePct = price != null ? ((ob.entry_price - price) / price) * 100 : null;
    return { ...ob, distancePct };
  });

  // Sort by absolute distance (closest first)
  enriched?.sort((a, b) => {
    const da = a.distancePct != null ? Math.abs(a.distancePct) : Infinity;
    const db = b.distancePct != null ? Math.abs(b.distancePct) : Infinity;
    return da - db;
  });

  const isBtc = (pair: string) => pair.startsWith("BTC");

  return (
    <div>
      <div className="card-title">Active Order Blocks</div>
      <div className="scroll-y">
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Pair</th>
              <th>TF</th>
              <th>Direction</th>
              <th className="col-range">Range</th>
              <th style={{ textAlign: "right" }}>Entry</th>
              <th style={{ textAlign: "right" }}>Dist%</th>
              <th className="col-vol" style={{ textAlign: "right" }}>Vol Ratio</th>
            </tr>
          </thead>
          <tbody>
            {loading && !obs && (
              <tr><td colSpan={8}><div className="skeleton" style={{ height: 16, width: "100%" }} /></td></tr>
            )}
            {enriched?.map((ob, i) => {
              const approaching = ob.distancePct != null && Math.abs(ob.distancePct) < 0.5;
              const highVol = ob.volume_ratio >= 2.0;
              return (
                <tr key={`${ob.timestamp}-${ob.pair}-${ob.timeframe}-${i}`} className="animate-in">
                  <td style={{ color: "var(--text-muted)" }}>{formatTime(ob.timestamp)}</td>
                  <td style={{ fontWeight: 600 }}>{ob.pair}</td>
                  <td>{ob.timeframe}</td>
                  <td>
                    <span className={`badge ${ob.direction === "bullish" ? "badge-long" : "badge-short"}`}>
                      {ob.direction}
                    </span>
                  </td>
                  <td className="num col-range">
                    {fmt(ob.low, isBtc(ob.pair) ? 1 : 2)} - {fmt(ob.high, isBtc(ob.pair) ? 1 : 2)}
                  </td>
                  <td className="num" style={{ fontWeight: 600 }}>
                    ${fmt(ob.entry_price, isBtc(ob.pair) ? 1 : 2)}
                  </td>
                  <td className="num" style={{
                    fontWeight: 600,
                    color: approaching ? "var(--warning)" : "var(--text-secondary)",
                  }}>
                    {ob.distancePct != null
                      ? (ob.distancePct >= 0 ? "+" : "") + ob.distancePct.toFixed(2) + "%"
                      : "--"}
                  </td>
                  <td className="num col-vol" style={{
                    color: highVol ? "var(--warning)" : "var(--text-secondary)",
                  }}>
                    {ob.volume_ratio.toFixed(1)}x
                  </td>
                </tr>
              );
            })}
            {enriched?.length === 0 && (
              <tr><td colSpan={8} style={{ color: "var(--text-muted)", textAlign: "center", padding: 20 }}>No active order blocks</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
