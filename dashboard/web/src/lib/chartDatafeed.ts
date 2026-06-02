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

// Resolution string -> period length in ms (for client-side forming-bar aggregation).
export const RESOLUTION_MS: Record<string, number> = {
  "5": 5 * 60_000,
  "15": 15 * 60_000,
  "60": 60 * 60_000,
  "240": 240 * 60_000,
  "D": 24 * 60 * 60_000,
};

// klinecharts period -> backend UDF resolution string.
export const RESOLUTIONS: { label: string; resolution: string }[] = [
  { label: "5m", resolution: "5" },
  { label: "15m", resolution: "15" },
  { label: "1h", resolution: "60" },
  { label: "4h", resolution: "240" },
  { label: "1D", resolution: "D" },
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

// The current FORMING candle (from Redis), for live intra-candle ticks. /history
// only returns closed bars, so this is what makes the chart actually move. Backend
// caches a 5m candle; higher TFs are aggregated client-side from its close.
export async function fetchLiveCandle(
  symbol: string,
  resolution: string,
): Promise<Kline | null> {
  const params = new URLSearchParams({ symbol, resolution });
  const d = await fetchApi<{
    candle: null | { timestamp: number; open: number; high: number; low: number; close: number; volume: number };
  }>(`/chart/live?${params.toString()}`);
  const c = d.candle;
  if (!c || c.timestamp == null || c.close == null) return null;
  return { timestamp: c.timestamp, open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume ?? 0 };
}

// --- bot-detection overlay (C2) ---------------------------------------

export interface DetectionZone {
  type: "order_block" | "fvg";
  direction: "bullish" | "bearish";
  timestamp: number; // ms (zone origin candle)
  high: number;
  low: number;
  source_tf?: string; // timeframe the zone was detected on (MTF overlay): "1D"/"4H"/...
  // OB-only
  mitigated?: boolean;
  entry_price?: number;
  impulse_score?: number;
  retest_count?: number;
  // FVG-only
  size_pct?: number;
  filled_pct?: number;
  fully_filled?: boolean;
}

export interface Detections {
  order_blocks: DetectionZone[];
  fvgs: DetectionZone[];
  as_of: number; // seconds
  bars: number;
}

// Zones the bot's detectors hold active as-of bar `toMs` (the replay pointer).
export async function fetchDetections(
  symbol: string,
  resolution: string,
  toMs: number,
): Promise<Detections> {
  const params = new URLSearchParams({
    symbol,
    resolution,
    to: String(Math.floor(toMs / 1000)),
  });
  const d = await fetchApi<Detections>(`/chart/detections?${params.toString()}`);
  // Normalize ms on each zone (backend already returns ms timestamps).
  return d;
}

// --- detection timeline (perf: one replay, client-side as-of filtering) ----

// A zone plus its lifecycle in bar timestamps (ms): when it was first detected,
// last active (expiry), and first marked spent (mitigated/filled).
export interface ZoneLifecycle extends DetectionZone {
  born_ts: number;
  expire_ts: number;
  spent_ts: number | null;
  significant: boolean; // FVG: displacement bar beat the adaptive threshold; OB: always true
  source_tf: string;    // TF this zone was detected on (MTF)
}

export interface DetectionTimeline {
  zones: ZoneLifecycle[];
  as_of: number;
  timeframes: string[]; // TFs included in this overlay (HTF bias + chart TF)
}

// One replay over the window ending at `toMs` → every zone's lifecycle. Fetch
// this once per symbol/resolution (and on each new live bar) and filter with
// zonesAsOf() while scrubbing — no per-bar server call (the replay is ~2.5s).
export async function fetchDetectionTimeline(
  symbol: string,
  resolution: string,
  toMs: number,
): Promise<DetectionTimeline> {
  const params = new URLSearchParams({
    symbol,
    resolution,
    to: String(Math.floor(toMs / 1000)),
  });
  return fetchApi<DetectionTimeline>(`/chart/detection_timeline?${params.toString()}`);
}

// Resolve the zones active as-of `asOfMs` from a cached timeline, deriving the
// spent flag (mitigated for OBs, fully_filled for FVGs) at that point in time.
// When significantOnly is set, drop low-significance zones (small FVGs) — the
// LuxAlgo-style adaptive de-noise filter, applied chart-side only.
export function zonesAsOf(
  timeline: ZoneLifecycle[],
  asOfMs: number,
  significantOnly = false,
): DetectionZone[] {
  return timeline
    .filter((z) => z.born_ts <= asOfMs && asOfMs <= z.expire_ts)
    .filter((z) => !significantOnly || z.significant)
    .map((z) => {
      const spent = z.spent_ts != null && z.spent_ts <= asOfMs;
      return z.type === "order_block"
        ? { ...z, mitigated: spent }
        : { ...z, fully_filled: spent };
    });
}
