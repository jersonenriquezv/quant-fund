"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import Link from "next/link";
import { init, dispose, type Chart } from "klinecharts";
import {
  fetchHistory,
  fetchDetections,
  RESOLUTIONS,
  SYMBOLS,
  type Kline,
  type DetectionZone,
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
  const detTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const detSeq = useRef<number>(0); // drops out-of-order detection responses
  const lastDetAsOf = useRef<number | null>(null); // skip requery when as-of bar unchanged

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
  const [detCount, setDetCount] = useState<number | null>(null);

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

  // Detection overlay. The backend replay is ~2.5s/call, so we never requery
  // per-bar while playing — zones freeze during playback and refresh once on
  // pause/settle. A sequence token drops out-of-order (stale) responses, and we
  // skip the call entirely when the as-of bar hasn't changed.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    if (!showDetections) {
      clearDetections(chart);
      setDetCount(null);
      lastDetAsOf.current = null;
      return;
    }
    if (playing) return; // keep the last-rendered zones; don't thrash mid-play
    const bars = barsRef.current;
    if (!bars.length) return;
    const idx = replay ? asOfIdx : bars.length - 1;
    const asOfMs = bars[idx]?.timestamp;
    if (!asOfMs || asOfMs === lastDetAsOf.current) return;

    if (detTimer.current) clearTimeout(detTimer.current);
    detTimer.current = setTimeout(async () => {
      const seq = ++detSeq.current;
      try {
        const d = await fetchDetections(symbol, resolution, asOfMs);
        if (seq !== detSeq.current) return; // a newer request superseded this one
        lastDetAsOf.current = asOfMs;
        const zones: DetectionZone[] = [...d.order_blocks, ...d.fvgs];
        renderDetections(chart, zones, asOfMs);
        setDetCount(zones.length);
      } catch {
        /* leave previous zones on transient error */
      }
    }, 500);
    return () => { if (detTimer.current) clearTimeout(detTimer.current); };
  }, [showDetections, asOfIdx, replay, symbol, resolution, barCount, playing]);

  // A3 — live candle wiring. In live (non-replay) mode, poll the latest bars and
  // update the forming candle so the chart ticks in real time. Detections gate
  // on the as-of timestamp, so they only refetch when a new bar actually forms.
  useEffect(() => {
    if (replay || !chartReady) return;
    const id = setInterval(async () => {
      const chart = chartRef.current;
      if (!chart) return;
      try {
        const bars = await fetchHistory(symbol, resolution);
        if (!bars.length) return;
        barsRef.current = bars;
        setBarCount(bars.length);
        chart.updateData(bars[bars.length - 1]); // tick the last/forming bar
      } catch {
        /* transient — keep current data */
      }
    }, 3000);
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
