"use client";

import { useRef, useEffect, useState, useCallback } from "react";
import { usePolling } from "@/lib/hooks";
import type { LiqHeatmapData } from "@/lib/api";

const PAIRS = ["BTC/USDT", "ETH/USDT"] as const;

function formatUsd(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  return n.toFixed(0);
}

function formatPrice(n: number, isBtc: boolean): string {
  return n.toLocaleString("en-US", {
    minimumFractionDigits: isBtc ? 0 : 0,
    maximumFractionDigits: isBtc ? 0 : 0,
  });
}

export function LiquidationHeatmap() {
  const [pair, setPair] = useState<string>(PAIRS[0]);
  const { data, loading } = usePolling<LiqHeatmapData>(
    `/liquidation/heatmap/${pair}`,
    30000,
  );

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    const container = containerRef.current;
    if (!canvas || !container || !data || data.bins.length === 0) return;

    const dpr = window.devicePixelRatio || 1;
    const rect = container.getBoundingClientRect();
    const w = rect.width;
    const h = rect.height;

    canvas.width = w * dpr;
    canvas.height = h * dpr;
    canvas.style.width = `${w}px`;
    canvas.style.height = `${h}px`;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.scale(dpr, dpr);

    // Clear
    ctx.clearRect(0, 0, w, h);

    const isBtc = pair.startsWith("BTC");
    const bins = data.bins;
    const currentPrice = data.current_price;

    // Find max USD for scaling
    const maxUsd = Math.max(
      ...bins.map((b) => Math.max(b.liq_long_usd, b.liq_short_usd)),
      1,
    );

    // Layout constants
    const fontSize = w < 400 ? 9 : 11;
    const labelWidth = isBtc ? 56 : 44;
    const topPad = 4;
    const bottomPad = 4;
    const chartLeft = labelWidth;
    const chartRight = w - labelWidth;
    const chartW = chartRight - chartLeft;
    const midX = chartLeft + chartW / 2;

    // Price range
    const priceMin = bins[0].price;
    const priceMax = bins[bins.length - 1].price;
    const priceRange = priceMax - priceMin || 1;

    // Map price to Y (inverted: high price at top)
    const priceToY = (p: number): number => {
      const pct = (p - priceMin) / priceRange;
      return topPad + (1 - pct) * (h - topPad - bottomPad);
    };

    // Bar height based on bin density
    const barH = Math.max(1, ((h - topPad - bottomPad) / bins.length) * 0.85);

    // Draw center line
    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(midX, 0);
    ctx.lineTo(midX, h);
    ctx.stroke();

    // Draw bars
    for (const bin of bins) {
      const y = priceToY(bin.price) - barH / 2;

      // Long liquidation (left side, red)
      if (bin.liq_long_usd > 0) {
        const barW = (bin.liq_long_usd / maxUsd) * (chartW / 2);
        ctx.fillStyle = "rgba(239, 68, 68, 0.6)";
        ctx.fillRect(midX - barW, y, barW, barH);
      }

      // Short liquidation (right side, green)
      if (bin.liq_short_usd > 0) {
        const barW = (bin.liq_short_usd / maxUsd) * (chartW / 2);
        ctx.fillStyle = "rgba(16, 185, 129, 0.6)";
        ctx.fillRect(midX, y, barW, barH);
      }
    }

    // Draw current price line
    if (currentPrice >= priceMin && currentPrice <= priceMax) {
      const priceY = priceToY(currentPrice);
      ctx.strokeStyle = "rgba(59, 130, 246, 0.8)";
      ctx.lineWidth = 1.5;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(chartLeft, priceY);
      ctx.lineTo(chartRight, priceY);
      ctx.stroke();
      ctx.setLineDash([]);

      // Price label
      ctx.fillStyle = "rgba(59, 130, 246, 0.9)";
      ctx.font = `bold ${fontSize}px "JetBrains Mono", monospace`;
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      ctx.fillText(
        `$${formatPrice(currentPrice, isBtc)}`,
        chartLeft - 4,
        priceY,
      );
    }

    // Y-axis price labels (sample ~8 evenly spaced)
    const labelCount = Math.min(8, bins.length);
    const step = Math.max(1, Math.floor(bins.length / labelCount));
    ctx.fillStyle = "rgba(255,255,255,0.35)";
    ctx.font = `${fontSize}px "JetBrains Mono", monospace`;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";

    for (let i = 0; i < bins.length; i += step) {
      const bin = bins[i];
      const y = priceToY(bin.price);
      ctx.fillText(`$${formatPrice(bin.price, isBtc)}`, chartLeft - 4, y);
    }

    // Legend labels at top
    ctx.font = `bold ${fontSize}px "JetBrains Mono", monospace`;
    ctx.textBaseline = "top";

    ctx.fillStyle = "rgba(239, 68, 68, 0.7)";
    ctx.textAlign = "right";
    ctx.fillText("LONG LIQ", midX - 6, 2);

    ctx.fillStyle = "rgba(16, 185, 129, 0.7)";
    ctx.textAlign = "left";
    ctx.fillText("SHORT LIQ", midX + 6, 2);

    // Max USD label
    ctx.fillStyle = "rgba(255,255,255,0.25)";
    ctx.font = `${fontSize - 1}px "JetBrains Mono", monospace`;
    ctx.textAlign = "right";
    ctx.fillText(formatUsd(maxUsd), w - 2, h - fontSize - 2);
  }, [data, pair]);

  // Redraw on data change or resize
  useEffect(() => {
    draw();
    const handleResize = () => draw();
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, [draw]);

  const hasBins = data && data.bins.length > 0;

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
        <div className="card-title" style={{ marginBottom: 0 }}>
          Estimated Liquidation Levels
        </div>
        <div className="liq-tabs">
          {PAIRS.map((p) => (
            <button
              key={p}
              onClick={() => setPair(p)}
              className={`liq-tab${pair === p ? " liq-tab-active" : ""}`}
            >
              {p.split("/")[0]}
            </button>
          ))}
        </div>
      </div>

      <div ref={containerRef} className="liq-canvas-container">
        {loading && !data && (
          <div className="skeleton" style={{ width: "100%", height: "100%" }} />
        )}
        {!loading && !hasBins && (
          <div style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            height: "100%",
            color: "var(--text-muted)",
            fontSize: 12,
          }}>
            No OI data available
          </div>
        )}
        <canvas
          ref={canvasRef}
          style={{
            display: hasBins ? "block" : "none",
            width: "100%",
            height: "100%",
          }}
        />
      </div>
    </div>
  );
}
