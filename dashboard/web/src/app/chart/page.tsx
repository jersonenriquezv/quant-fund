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
  periodStartMs,
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
import {
  ensurePositionOverlayRegistered,
  createPosition,
  clearPosition,
  onPositionChange,
} from "@/lib/positionTool";
import {
  ensureDrawingOverlaysRegistered,
  startDrawing,
  cancelPendingDrawing,
  clearAllDrawings,
  restoreDrawings,
  deleteSelectedDrawing,
} from "@/lib/drawingTools";
import { isPositionSelected } from "@/lib/positionTool";
import ChartToolbar, { type ToolboxAction } from "@/components/ChartToolbar";

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
  const reconcilingRef = useRef<boolean>(false); // prevents overlapping reconciles
  const lastReconcileRef = useRef<number>(0); // debounce rapid visibility toggles

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
  const [detMode, setDetMode] = useState<"off" | "boxes" | "subtle">("off"); // cycles per click
  const showDetections = detMode !== "off";
  const [significantOnly, setSignificantOnly] = useState(true); // LuxAlgo de-noise: on by default
  const [detCount, setDetCount] = useState<number | null>(null);
  const [timelineReady, setTimelineReady] = useState(0); // bumps when timeline refetched
  const [hourTick, setHourTick] = useState(0); // wall-clock heartbeat to refresh live HTF detections
  const [positionRR, setPositionRR] = useState<number | null>(null); // A6 live R:R
  const [armDir, setArmDir] = useState<"long" | "short" | null>(null); // click-to-place mode
  const [activeTool, setActiveTool] = useState<ToolboxAction>("cursor"); // left toolbox
  const restoredSymbolRef = useRef<string | null>(null); // last symbol whose drawings were restored

  // Init chart once.
  useEffect(() => {
    if (!containerRef.current) return;
    ensureDetectionOverlayRegistered();
    ensurePositionOverlayRegistered();
    ensureDrawingOverlaysRegistered();
    onPositionChange(setPositionRR);
    const chart = init(containerRef.current);
    if (chart) {
      chart.setStyles(CHART_STYLES);
      chart.setOffsetRightDistance(140); // room for right-projected detection zones
      chart.createIndicator("VOL", false); // separate sub-pane below candles
      chartRef.current = chart;
      if (process.env.NODE_ENV !== "production") {
        (window as unknown as { __qfChart?: unknown }).__qfChart = chart; // dev/test handle
      }
      setChartReady(true);
    }
    return () => {
      onPositionChange(null);
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
      // Saved drawings are per symbol — restore on first load and on symbol
      // switch (a BTC trend line on the ETH chart is meaningless). Resolution
      // toggles keep them: points are (timestamp, value), valid across TFs.
      if (restoredSymbolRef.current !== symbol) {
        restoreDrawings(chartRef.current, symbol);
        restoredSymbolRef.current = symbol;
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load chart data.");
    } finally {
      setLoading(false);
    }
  }, [symbol, resolution, replay]);

  useEffect(() => { if (chartReady) load(); }, [chartReady, load]);

  // Reconcile closed bars against /history WITHOUT the heavy load() side effects
  // (no spinner, no viewport reset, no asOfIdx reset). The live poll only ever
  // appends/updates the tail and never re-pulls /history, so a backgrounded tab
  // (throttled setInterval) that skips a period leaves a permanent hole, and every
  // forming bar it pushed (volume 0, approx OHLC) is never replaced by the real
  // closed candle. Re-fetching /history fixes both. Silent + viewport-preserving.
  const reconcile = useCallback(async () => {
    const chart = chartRef.current;
    if (!chart || replay) return;
    if (reconcilingRef.current) return; // already in flight
    if (Date.now() - lastReconcileRef.current < 2000) return; // debounce toggles
    reconcilingRef.current = true;
    try {
      const fresh = await fetchHistory(symbol, resolution);
      if (!fresh.length) return;
      const cur = barsRef.current;
      const freshLast = fresh[fresh.length - 1].timestamp;
      // Keep the current forming bar only if its period is still open (newer than
      // the freshest closed bar); otherwise /history now carries it as a real bar.
      const last = cur[cur.length - 1];
      const merged = last && last.timestamp > freshLast ? [...fresh, last] : fresh;
      // Skip the repaint when nothing changed (same length + same tail timestamp).
      if (merged.length === cur.length &&
          merged[merged.length - 1]?.timestamp === last?.timestamp) {
        return;
      }
      const offset = chart.getOffsetRightDistance(); // preserve horizontal scroll
      barsRef.current = merged;
      setBarCount(merged.length);
      chart.applyNewData(merged);
      chart.setOffsetRightDistance(offset);
    } catch {
      /* transient — keep current data */
    } finally {
      reconcilingRef.current = false;
      lastReconcileRef.current = Date.now();
    }
  }, [symbol, resolution, replay]);

  // Re-fetch /history when the tab regains focus. Browsers throttle/pause the 2s
  // live poll while backgrounded; on return the data may have skipped periods.
  useEffect(() => {
    if (replay || !chartReady) return;
    const onVis = () => { if (document.visibilityState === "visible") reconcile(); };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, [replay, chartReady, reconcile]);

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
    // As-of: replay → the pointer bar; live → wall-clock NOW. An HTF bar starts
    // hours/days before "now", so using its start would hide zones born within
    // the current forming bar — up to 7 days stale on 1W (the bug 1W exposed).
    // The cache key bounds refetch cost: low TFs still refresh per new bar,
    // HTF refreshes at most hourly (min(period, 1h)), driven by hourTick.
    const pms = RESOLUTION_MS[resolution] ?? 5 * 60_000;
    const asOf = replay ? bars[bars.length - 1].timestamp : Date.now();
    const refreshMs = Math.min(pms, 3_600_000);
    const key = replay
      ? bars[bars.length - 1].timestamp
      : Math.floor(asOf / refreshMs) * refreshMs;
    if (key === timelineTs.current) return; // window unchanged — reuse cache
    const seq = ++detSeq.current;
    (async () => {
      try {
        const tl = await fetchDetectionTimeline(symbol, resolution, asOf);
        if (seq !== detSeq.current) return; // superseded
        timelineRef.current = tl.zones;
        timelineTs.current = key;
        setTimelineReady((n) => n + 1); // nudge the render effect
      } catch {
        /* keep previous timeline on transient error */
      }
    })();
  }, [chartReady, showDetections, symbol, resolution, replay, barCount, hourTick]);

  // Wall-clock heartbeat (live only) so HTF detection overlays refresh as zones
  // form within the current bar, instead of going stale until the bar closes.
  useEffect(() => {
    if (replay || !chartReady) return;
    const id = setInterval(() => setHourTick((n) => n + 1), 60_000);
    return () => clearInterval(id);
  }, [replay, chartReady]);

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
    // Match the timeline's as-of: live filters against NOW (not the HTF bar's
    // start), so zones active right now render even mid-bar.
    const asOfMs = replay ? bars[idx]?.timestamp : Date.now();
    if (!asOfMs) return;
    const price = bars[idx]?.close ?? 0;
    // "Focus" on (significantOnly): keep only impulsive, unmitigated zones nearest
    // to price. Off: show everything raw (incl. spent/dimmed) for inspection.
    let zones = zonesAsOf(timelineRef.current, asOfMs, significantOnly);
    if (significantOnly) zones = curateZones(zones, price);
    renderDetections(chart, zones, asOfMs, detMode === "subtle" ? "subtle" : "boxes");
    setDetCount(zones.length);
  }, [showDetections, detMode, significantOnly, asOfIdx, replay, barCount, timelineReady]);

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
          const barTs = periodStartMs(resolution, live.timestamp); // current HTF period (weekly = Monday-aligned)
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

        // Period skipped (≥1 closed bar missing between last and now) — e.g. the
        // poll was throttled while backgrounded. Don't blind-push past the hole;
        // re-fetch /history to backfill the missed closed bars, then let the next
        // tick append the current forming bar normally.
        if (last && formed.timestamp > last.timestamp + pms) {
          reconcile();
          return;
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
  }, [replay, chartReady, symbol, resolution, reconcile]);

  const toggleReplay = () => {
    setPlaying(false);
    setReplay((r) => !r);
  };
  const step = (d: number) =>
    setAsOfIdx((i) => Math.min(barCount - 1, Math.max(0, i + d)));

  // A6 — click-to-place (TradingView-style). Arming a direction switches the
  // cursor to a crosshair; the next click on the chart drops the entry at that
  // exact price/time (then the handles are dragged freely).
  const removePosition = () => {
    if (chartRef.current) clearPosition(chartRef.current);
  };

  // Left toolbox state machine. Picking any tool first cancels whatever was
  // armed (in-progress drawing or position placement) — one active tool at a
  // time, TradingView-style. Drawing tools auto-revert to cursor on placement.
  const selectTool = useCallback((tool: ToolboxAction) => {
    const chart = chartRef.current;
    if (!chart) return;
    cancelPendingDrawing(chart);
    setArmDir(null);
    if (tool === "cursor") {
      setActiveTool("cursor");
      return;
    }
    if (tool === "long" || tool === "short") {
      setActiveTool(tool);
      setArmDir(tool);
      return;
    }
    if (tool === "clear") {
      clearAllDrawings(chart, symbol);
      setActiveTool("cursor");
      return;
    }
    setActiveTool(tool);
    startDrawing(chart, symbol, tool, () => setActiveTool("cursor"));
  }, [symbol]);

  // Esc cancels the armed tool (drawing in progress or position placement).
  // Backspace/Delete removes whatever drawing (or the position) is selected.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        selectTool("cursor");
        return;
      }
      if (e.key === "Backspace" || e.key === "Delete") {
        const tag = (e.target as HTMLElement | null)?.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA") return; // typing, not deleting drawings
        const chart = chartRef.current;
        if (!chart) return;
        if (deleteSelectedDrawing(chart, symbol)) {
          e.preventDefault();
        } else if (isPositionSelected()) {
          clearPosition(chart);
          e.preventDefault();
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectTool, symbol]);

  // The position tool disarms itself after placement (armDir → null inside the
  // pointerdown effect) — mirror that back onto the toolbox highlight.
  useEffect(() => {
    if (!armDir) setActiveTool((t) => (t === "long" || t === "short" ? "cursor" : t));
  }, [armDir]);

  // While armed, the next click on the chart places the position there.
  useEffect(() => {
    if (!armDir) return;
    const el = containerRef.current;
    const chart = chartRef.current;
    if (!el || !chart) return;
    const onDown = (e: PointerEvent) => {
      const rect = el.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const pt = chart.convertFromPixel([{ x, y }], {});
      const point = Array.isArray(pt) ? pt[0] : pt;
      const entry = point?.value;
      const ts = point?.timestamp ?? barsRef.current[barsRef.current.length - 1]?.timestamp;
      if (entry && ts) createPosition(chart, { direction: armDir, entry, anchorTs: ts });
      setArmDir(null);
    };
    el.addEventListener("pointerdown", onDown, { once: true, capture: true });
    return () => el.removeEventListener("pointerdown", onDown, { capture: true } as EventListenerOptions);
  }, [armDir]);

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
            onClick={() => setDetMode((m) => (m === "off" ? "boxes" : m === "boxes" ? "subtle" : "off"))}
            title="Cycles: Off → Boxes (filled zones) → Lines (edges only, candles stay readable) → Off">
            Detections{detCount != null ? ` (${detCount})` : ""}{detMode === "boxes" ? " ▣" : detMode === "subtle" ? " ☰" : ""}
          </button>
          {showDetections && (
            <button className={`chart-toggle ${significantOnly ? "on" : ""}`}
              onClick={() => setSignificantOnly((v) => !v)}
              title="Focus: only impulsive, unmitigated zones nearest to price (per timeframe). Off = show all raw zones.">
              Focus
            </button>
          )}
          {positionRR != null && (
            <button className="chart-toggle chart-pos-clear" onClick={removePosition} title="Clear position">
              R:R {positionRR.toFixed(2)} ✕
            </button>
          )}
          {armDir && <span className="chart-arm-hint">click chart to place {armDir} entry…</span>}
          {activeTool !== "cursor" && !armDir && (
            <span className="chart-arm-hint">click chart to draw…</span>
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

      <div className="chart-body">
        <ChartToolbar active={activeTool} onSelect={selectTool} />
        <div
          ref={containerRef}
          className={`chart-canvas ${armDir ? "arming" : ""}`}
          onContextMenu={(e) => e.preventDefault()} // right-click deletes drawings instead
        />
      </div>
    </main>
  );
}
