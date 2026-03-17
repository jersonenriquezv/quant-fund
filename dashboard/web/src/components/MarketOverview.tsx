"use client";

import { usePolling } from "@/lib/hooks";
import type { MarketData, HTFBiasResponse, WSMessage } from "@/lib/api";

function fmt(n: number | null | undefined, d: number = 2): string {
  if (n == null) return "--";
  return n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

function fmtCompact(n: number | null | undefined): string {
  if (n == null) return "--";
  const abs = Math.abs(n);
  if (abs >= 1e12) return (n / 1e12).toFixed(1) + "T";
  if (abs >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (abs >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (abs >= 1e3) return (n / 1e3).toFixed(0) + "K";
  return fmt(n);
}

function priceDecimals(pair: string): number {
  if (pair.startsWith("BTC")) return 1;
  if (pair.startsWith("ETH")) return 2;
  if (pair.startsWith("DOGE")) return 5;
  return 3;
}

function symbol(pair: string): string {
  return pair.split("/")[0];
}

function PairRow({
  pair,
  ws,
  bias,
}: {
  pair: string;
  ws: WSMessage | null;
  bias: string | null;
}) {
  const { data: market } = usePolling<MarketData>(`/market/${encodeURIComponent(pair)}`, 5000);

  const wsPrice = ws?.prices?.[pair]?.price;
  const price = wsPrice ?? market?.price;
  const changePct = market?.change_pct;
  const isPositive = (changePct ?? 0) >= 0;
  const decimals = priceDecimals(pair);
  const fundingRate = market?.funding_rate;
  const fundingPositive = (fundingRate ?? 0) >= 0;

  return (
    <tr className="animate-in">
      <td>
        <div className="market-pair-cell">
          <span className="market-symbol">{symbol(pair)}</span>
          <span className="market-quote">USDT</span>
          {bias && bias !== "undefined" && (
            <span className={`badge market-bias ${bias === "bullish" ? "badge-long" : bias === "bearish" ? "badge-short" : "badge-neutral"}`}>
              {bias}
            </span>
          )}
        </div>
      </td>
      <td className="num market-price">
        ${price != null ? fmt(price, decimals) : "--"}
      </td>
      <td className={`num ${isPositive ? "pnl-positive" : "pnl-negative"}`}>
        {changePct != null ? (isPositive ? "+" : "") + changePct.toFixed(2) + "%" : "--"}
      </td>
      <td className={`num market-funding ${fundingRate != null ? (fundingPositive ? "pnl-positive" : "pnl-negative") : ""}`}>
        {fundingRate != null ? (fundingRate >= 0 ? "+" : "") + (fundingRate * 100).toFixed(4) + "%" : "--"}
      </td>
      <td className="num market-oi">
        {market?.oi_usd != null ? "$" + fmtCompact(market.oi_usd) : "--"}
      </td>
    </tr>
  );
}

const PAIRS = [
  "BTC/USDT", "ETH/USDT", "SOL/USDT", "DOGE/USDT",
  "XRP/USDT", "LINK/USDT", "AVAX/USDT",
];

export function MarketOverview({ ws }: { ws: WSMessage | null }) {
  const { data: biasData } = usePolling<HTFBiasResponse>("/strategy/htf-bias", 10000);

  return (
    <div>
      <div className="card-title">Markets</div>
      <table className="market-table">
        <thead>
          <tr>
            <th>Pair</th>
            <th style={{ textAlign: "right" }}>Price</th>
            <th style={{ textAlign: "right" }}>24h</th>
            <th className="market-funding" style={{ textAlign: "right" }}>Funding</th>
            <th className="market-oi" style={{ textAlign: "right" }}>Open Interest</th>
          </tr>
        </thead>
        <tbody>
          {PAIRS.map((pair) => {
            const rawBias = biasData?.bias?.[pair];
            const bias = rawBias && rawBias !== "undefined" ? rawBias : null;
            return (
              <PairRow
                key={pair}
                pair={pair}
                ws={ws}
                bias={bias}
              />
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
