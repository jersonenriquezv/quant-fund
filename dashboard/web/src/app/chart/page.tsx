"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import Link from "next/link";
import { init, dispose, type Chart } from "klinecharts";
import { fetchHistory, RESOLUTIONS, SYMBOLS } from "@/lib/chartDatafeed";

// Apple-dark theme tokens to match the rest of the dashboard.
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
    priceMark: {
      last: { upColor: "#b2fd02", downColor: "#ff4d4d" },
    },
  },
  xAxis: { axisLine: { color: "rgba(255,255,255,0.1)" }, tickText: { color: "#9ca3af" } },
  yAxis: { axisLine: { color: "rgba(255,255,255,0.1)" }, tickText: { color: "#9ca3af" } },
  crosshair: {
    horizontal: { line: { color: "rgba(255,255,255,0.25)" }, text: { backgroundColor: "#2a2a2a" } },
    vertical: { line: { color: "rgba(255,255,255,0.25)" }, text: { backgroundColor: "#2a2a2a" } },
  },
};

export default function ChartPage() {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<Chart | null>(null);
  const [symbol, setSymbol] = useState(SYMBOLS[0]);
  const [resolution, setResolution] = useState(RESOLUTIONS[2].resolution); // 1h
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Init chart once.
  useEffect(() => {
    if (!containerRef.current) return;
    const chart = init(containerRef.current);
    if (chart) {
      chart.setStyles(CHART_STYLES);
      chart.createIndicator("VOL", false, { id: "candle_pane" });
      chartRef.current = chart;
    }
    return () => {
      dispose(containerRef.current!);
      chartRef.current = null;
    };
  }, []);

  // Load data on symbol / resolution change.
  const load = useCallback(async () => {
    if (!chartRef.current) return;
    setLoading(true);
    setError(null);
    try {
      const bars = await fetchHistory(symbol, resolution);
      if (!bars.length) {
        setError("No data for this symbol / timeframe.");
        chartRef.current.applyNewData([]);
      } else {
        chartRef.current.applyNewData(bars);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load chart data.");
    } finally {
      setLoading(false);
    }
  }, [symbol, resolution]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <main className="chart-page">
      <header className="chart-header">
        <Link href="/" className="chart-back">← Dashboard</Link>
        <div className="chart-controls">
          <div className="chart-seg">
            {SYMBOLS.map((s) => (
              <button
                key={s}
                className={`chart-seg-btn ${s === symbol ? "active" : ""}`}
                onClick={() => setSymbol(s)}
              >
                {s.replace("/USDT", "")}
              </button>
            ))}
          </div>
          <div className="chart-seg">
            {RESOLUTIONS.map((r) => (
              <button
                key={r.resolution}
                className={`chart-seg-btn ${r.resolution === resolution ? "active" : ""}`}
                onClick={() => setResolution(r.resolution)}
              >
                {r.label}
              </button>
            ))}
          </div>
        </div>
        <div className="chart-status">{loading ? "loading…" : error ? error : ""}</div>
      </header>
      <div ref={containerRef} className="chart-canvas" />
    </main>
  );
}
