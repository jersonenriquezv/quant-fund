// Datafeed adapter: maps the backend /api/chart/* endpoints (written to the
// TradingView UDF shape) into the kline arrays klinecharts expects.
// Backend: dashboard/api/routes/chart.py. Scope: BTC/ETH, 5m/15m/1h/4h.

import { fetchApi } from "@/lib/api";

export interface Kline {
  timestamp: number; // ms
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

// klinecharts period -> backend UDF resolution string.
export const RESOLUTIONS: { label: string; resolution: string }[] = [
  { label: "5m", resolution: "5" },
  { label: "15m", resolution: "15" },
  { label: "1h", resolution: "60" },
  { label: "4h", resolution: "240" },
];

export const SYMBOLS = ["BTC/USDT", "ETH/USDT"];

interface UdfHistory {
  s: "ok" | "no_data";
  t?: number[];
  o?: number[];
  h?: number[];
  l?: number[];
  c?: number[];
  v?: number[];
}

// Fetch the most recent window of bars for a symbol/resolution.
// from=0 lets the backend return the newest bars (it caps + reverses).
export async function fetchHistory(
  symbol: string,
  resolution: string,
  toMs?: number,
): Promise<Kline[]> {
  const to = Math.floor((toMs ?? Date.now()) / 1000);
  const params = new URLSearchParams({
    symbol,
    resolution,
    from: "0",
    to: String(to),
  });
  const data = await fetchApi<UdfHistory>(`/chart/history?${params.toString()}`);
  if (data.s !== "ok" || !data.t) return [];
  return data.t.map((tSec, i) => ({
    timestamp: tSec * 1000, // UDF seconds -> klinecharts ms
    open: data.o![i],
    high: data.h![i],
    low: data.l![i],
    close: data.c![i],
    volume: data.v![i],
  }));
}
