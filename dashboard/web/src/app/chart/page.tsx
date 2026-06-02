"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import Link from "next/link";
import { init, dispose, type Chart } from "klinecharts";
import {
  fetchHistory,
  fetchLiveCandle,
  fetchDetectionTimeline,
  zonesAsOf,
  curateZones,
  RESOLUTIONS,
  RESOLUTION_MS,
  SYMBOLS,
  type Kline,
  type ZoneLifecycle,
} from "@/lib/chartDatafeed";
import {
  ensureDetectionOverlayRegistered,
  renderDetections,
  clearDetections,
} from "@/lib/detectionOverlay";

const CHART_STYLES = {
  grid: {
    horizontal: { color: "rgba(255,255,255,0.05)" },
    vertical: { color: "rgba(255,255,255,0.05)" },
  },
  candle: {
    bar: {
      upColor: "#b2fd02",
      downColor: "#ff4d4d",
      upBorderColor: "#b2fd02",
      downBorderColor: "#ff4d4d",
      upWickColor: "#b2fd02",
      downWickColor: "#ff4d4d",
    },
    priceMark: { last: { upColor: "#b2fd02", downColor: "#ff4d4d" } },
  },
  xAxis: { axisLine: { color: "rgba(255,255,255,0.1)" }, tickText: { color: "#9ca3af" } },
  yAxis: { axisLine: { color: "rgba(255,255,255,0.1)" }, tickText: { color: "#9ca3af" } },
  crosshair: {
    horizontal: { line: { color: "rgba(255,255,255,0.25)" }, text: { backgroundColor: "#2a2a2a" } },
    vertical: { line: { color: "rgba(255,255,255,0.25)" }, text: { backgroundColor: "#2a2a2a" } },
  },
};

const SPEEDS = [1, 2, 4, 8];
const REPLAY_TAIL = 150; // bars revealed by playing forward from the entry point

function fmtBar(ts: number | undefined): string {
  if (!ts) return "—";
  return new Date(ts).toLocaleString("en-US", {
    month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

export default function ChartPage() {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<Chart | null>(null);
  const barsRef = useRef<Kline[]>([]);
  const prevIdxRef = useRef<number>(-1);
  const detSeq = useRef<number>(0); // drops out-of-order timeline responses
  const timelineRef = useRef<ZoneLifecycle[]>([]); // cached zone lifecycles
  const timelineTs = useRef<number | null>(null); // last bar the timeline was built for

  const [chartReady, setChartReady] = useState(false);
  const [symbol, setSymbol] = useState(SYMBOLS[0]);
  const [resolution, setResolution] = useState(RESOLUTIONS[2].resolution); // 1h
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [barCount, setBarCount] = useState(0);
  const [replay, setReplay] = useState(false);
  const [asOfIdx, setAsOfIdx] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(2);
  const [showDetections, setShowDetections] = useState(false);
  const [significantOnly, setSignificantOnly] = useState(true); // LuxAlgo de-noise: on by default
  const [detCount, setDetCount] = useState<number | null>(null);
  const [timelineReady, setTimelineReady] = useState(0); // bumps when timeline refetched

  // Init chart once.
  useEffect(() => {
    if (!containerRef.current) return;
    ensureDetectionOverlayRegistered();
    const chart = init(containerRef.current);
    if (chart) {
      chart.setStyles(CHART_STYLES);
      chart.createIndicator("VOL", false); // separate sub-pane below candles
      chartRef.current = chart;
      setChartReady(true);
    }
    return () => {
      if (containerRef.current) dispose(containerRef.current);
      chartRef.current = null;
    };
  }, []);

  // Load history on symbol / resolution change.
  const load = useCallback(async () => {
    if (!chartRef.current) return;
    setLoading(true);
    setError(null);
    setPlaying(false);
    try {
      const bars = await fetchHistory(symbol, resolution);
      barsRef.current = bars;
      setBarCount(bars.length);
      prevIdxRef.current = -1;
      if (!bars.length) {
        setError("No data for this symbol / timeframe.");
        chartRef.current.applyNewData([]);
        return;
      }
      // Entering fresh: live mode shows everything; replay rewinds to the tail.
      const startIdx = replay ? Math.max(0, bars.length - 1 - REPLAY_TAIL) : bars.length - 1;
      setAsOfIdx(startIdx);
      chartRef.current.applyNewData(replay ? bars.slice(0, startIdx + 1) : bars);
      prevIdxRef.current = startIdx;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load chart data.");
    } finally {
      setLoading(false);
    }
  }, [symbol, resolution, replay]);

  useEffect(() => { if (chartReady) load(); }, [chartReady, load]);

  // Reflect asOfIdx onto the chart (replay only). Append-by-one when playing.
  useEffect(() => {
    const chart = chartRef.current;
    const bars = barsRef.current;
    if (!chart || !replay || !bars.length) return;
    if (asOfIdx === prevIdxRef.current + 1) {
      chart.updateData(bars[asOfIdx]); // smooth single-bar advance
    } else {
      chart.applyNewData(bars.slice(0, asOfIdx + 1));
    }
    prevIdxRef.current = asOfIdx;
  }, [asOfIdx, replay]);

  // Play loop.
  useEffect(() => {
    if (!playing) return;
    const ms = 1000 / speed;
    const id = setInterval(() => {
      setAsOfIdx((i) => {
        if (i >= barsRef.current.length - 1) return i;
        return i + 1;
      });
    }, ms);
    return () => clearInterval(id);
  }, [playing, speed]);

  // Stop at end.
  useEffect(() => {
    if (playing && asOfIdx >= barCount - 1 && barCount > 0) setPlaying(false);
  }, [asOfIdx, barCount, playing]);

  // Detection timeline — fetch ONCE per symbol/resolution (and on each new live
  // bar), not per scrub. The replay is ~2.5s, so doing it once and filtering the
  // cached lifecycles client-side keeps scrub/playback instant and off the server.
  useEffect(() => {
    if (!chartReady || !showDetections) return;
    const bars = barsRef.current;
    if (!bars.length) return;
    const lastTs = bars[bars.length - 1].timestamp;
    if (lastTs === timelineTs.current) return; // window unchanged — reuse cache
    const seq = ++detSeq.current;
    (async () => {
      try {
        const tl = await fetchDetectionTimeline(symbol, resolution, lastTs);
        if (seq !== detSeq.current) return; // superseded
        timelineRef.current = tl.zones;
        timelineTs.current = lastTs;
        setTimelineReady((n) => n + 1); // nudge the render effect
      } catch {
        /* keep previous timeline on transient error */
      }
    })();
  }, [chartReady, showDetections, symbol, resolution, barCount]);

  // Render zones active as-of the current bar from the cached timeline. Pure
  // client-side filter → instant, runs every bar during playback with no fetch.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    if (!showDetections) {
      clearDetections(chart);
      setDetCount(null);
      return;
    }
    const bars = barsRef.current;
    if (!bars.length) return;
    const idx = replay ? asOfIdx : bars.length - 1;
    const asOfMs = bars[idx]?.timestamp;
    if (!asOfMs) return;
    const price = bars[idx]?.close ?? 0;
    // "Focus" on (significantOnly): keep only impulsive, unmitigated zones nearest
    // to price. Off: show everything raw (incl. spent/dimmed) for inspection.
    let zones = zonesAsOf(timelineRef.current, asOfMs, significantOnly);
    if (significantOnly) zones = curateZones(zones, price);
    renderDetections(chart, zones, asOfMs);
    setDetCount(zones.length);
  }, [showDetections, significantOnly, asOfIdx, replay, barCount, timelineReady]);

  // A3 — live candle wiring. In live (non-replay) mode, poll the FORMING candle
  // (Redis, via /chart/live) every 2s and tick it onto the chart. /history only
  // returns closed bars, so this is what makes the candle actually move intra-bar.
  // The backend caches a 5m forming candle; higher TFs aggregate it client-side
  // (open carried from the prior bar's close — perps are continuous).
  useEffect(() => {
    if (replay || !chartReady) return;
    const pms = RESOLUTION_MS[resolution] ?? 5 * 60_000;
    const id = setInterval(async () => {
      const chart = chartRef.current;
      if (!chart) return;
      try {
        const live = await fetchLiveCandle(symbol, resolution);
        if (!live) return;
        const bars = barsRef.current;
        const last = bars[bars.length - 1];
        const price = live.close;

        let formed: Kline;
        if (resolution === "5") {
          formed = live; // Redis candle IS this 5m bar — use its O/H/L/C directly
        } else {
          const barTs = Math.floor(live.timestamp / pms) * pms; // current HTF period
          if (last && last.timestamp === barTs) {
            formed = {
              ...last,
              high: Math.max(last.high, live.high, price),
              low: Math.min(last.low, live.low, price),
              close: price,
            };
          } else {
            const open = last ? last.close : live.open; // continuous open
            formed = { timestamp: barTs, open, high: Math.max(open, price), low: Math.min(open, price), close: price, volume: 0 };
          }
        }

        const isNewBar = !last || formed.timestamp > last.timestamp;
        // Skip the repaint when nothing actually moved (idle market) — avoids a
        // pointless full canvas redraw every 2s.
        if (!isNewBar && last &&
            last.close === formed.close && last.high === formed.high && last.low === formed.low) {
          return;
        }
        if (last && last.timestamp === formed.timestamp) {
          bars[bars.length - 1] = formed; // same bar — update in place
        } else if (isNewBar) {
          bars.push(formed); // new period — append (also retriggers detection refetch)
          setBarCount(bars.length);
        }
        chart.updateData(formed);
      } catch {
        /* transient — keep current data */
      }
    }, 2000);
    return () => clearInterval(id);
  }, [replay, chartReady, symbol, resolution]);

  const toggleReplay = () => {
    setPlaying(false);
    setReplay((r) => !r);
  };
  const step = (d: number) =>
    setAsOfIdx((i) => Math.min(barCount - 1, Math.max(0, i + d)));

  const asOfTs = barsRef.current[asOfIdx]?.timestamp;

  return (
    <main className="chart-page">
      <header className="chart-header">
        <Link href="/" className="chart-back">← Dashboard</Link>
        <div className="chart-controls">
          <div className="chart-seg">
            {SYMBOLS.map((s) => (
              <button key={s} className={`chart-seg-btn ${s === symbol ? "active" : ""}`}
                onClick={() => setSymbol(s)}>{s.replace("/USDT", "")}</button>
            ))}
          </div>
          <div className="chart-seg">
            {RESOLUTIONS.map((r) => (
              <button key={r.resolution} className={`chart-seg-btn ${r.resolution === resolution ? "active" : ""}`}
                onClick={() => setResolution(r.resolution)}>{r.label}</button>
            ))}
          </div>
          <button className={`chart-toggle ${replay ? "on" : ""}`} onClick={toggleReplay}>
            Replay
          </button>
          <button className={`chart-toggle ${showDetections ? "on" : ""}`}
            onClick={() => setShowDetections((v) => !v)}>
            Detections{detCount != null ? ` (${detCount})` : ""}
          </button>
          {showDetections && (
            <button className={`chart-toggle ${significantOnly ? "on" : ""}`}
              onClick={() => setSignificantOnly((v) => !v)}
              title="Focus: only impulsive, unmitigated zones nearest to price (per timeframe). Off = show all raw zones.">
              Focus
            </button>
          )}
        </div>
        <div className="chart-status">{loading ? "loading…" : error ?? ""}</div>
      </header>

      {replay && (
        <div className="chart-replay-bar">
          <button className="chart-replay-btn" onClick={() => step(-1)} disabled={asOfIdx <= 0}>⏮</button>
          <button className="chart-replay-btn play" onClick={() => setPlaying((p) => !p)}
            disabled={asOfIdx >= barCount - 1}>{playing ? "⏸" : "▶"}</button>
          <button className="chart-replay-btn" onClick={() => step(1)} disabled={asOfIdx >= barCount - 1}>⏭</button>
          <input className="chart-replay-slider" type="range" min={0} max={Math.max(0, barCount - 1)}
            value={asOfIdx} onChange={(e) => { setPlaying(false); setAsOfIdx(Number(e.target.value)); }} />
          <div className="chart-seg chart-speed">
            {SPEEDS.map((s) => (
              <button key={s} className={`chart-seg-btn ${s === speed ? "active" : ""}`}
                onClick={() => setSpeed(s)}>{s}×</button>
            ))}
          </div>
          <span className="chart-asof">{fmtBar(asOfTs)} · {asOfIdx + 1}/{barCount}</span>
        </div>
      )}

      <div ref={containerRef} className="chart-canvas" />
    </main>
  );
}
