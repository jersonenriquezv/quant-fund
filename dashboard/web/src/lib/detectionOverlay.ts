// A SINGLE klinecharts overlay that draws every bot-detection zone (Order Block
// / FVG) as a colored rectangle + label. Using one overlay with N*2 points (two
// per zone) instead of one overlay per zone matters for performance: klinecharts
// calls every overlay's createPointFigures on each repaint (every live tick AND
// every crosshair move), so N separate overlays = N callbacks per frame = jank
// with many MTF zones. One overlay = one callback returning all figures.

import { registerOverlay, type Chart } from "klinecharts";
import type { DetectionZone } from "@/lib/chartDatafeed";

const OVERLAY_NAME = "detectionZones";
const GROUP_ID = "detections";

// Zone render style — "boxes" (filled rects) or "subtle" (edge lines only).
export type DetectionStyle = "boxes" | "subtle";

// type+direction+state -> fill / border. Mitigated/filled zones are dimmed.
function zoneColors(z: DetectionZone): { fill: string; border: string } {
  const spent = z.type === "order_block" ? z.mitigated : z.fully_filled;
  const a = spent ? 0.04 : 0.1;
  const b = spent ? 0.35 : 0.85;
  if (z.type === "order_block") {
    return z.direction === "bullish"
      ? { fill: `rgba(178,253,2,${a})`, border: `rgba(178,253,2,${b})` }
      : { fill: `rgba(255,77,77,${a})`, border: `rgba(255,77,77,${b})` };
  }
  // FVG — teal / amber to read distinctly from OBs.
  return z.direction === "bullish"
    ? { fill: `rgba(45,212,191,${a})`, border: `rgba(45,212,191,${b})` }
    : { fill: `rgba(245,158,11,${a})`, border: `rgba(245,158,11,${b})` };
}

// HTF (bias) zones are drawn more prominently than the chart's own TF.
const HTF_LABELS = new Set(["1W", "1D", "4H"]);
function isHtf(z: DetectionZone): boolean {
  return z.source_tf != null && HTF_LABELS.has(z.source_tf);
}

// Short label: kind + direction (↑/↓) + source timeframe + spent marker.
function label(z: DetectionZone): string {
  const kind = z.type === "order_block" ? "OB" : "FVG";
  const arrow = z.direction === "bullish" ? "↑" : "↓";
  const tf = z.source_tf ? ` ${z.source_tf}` : "";
  const spent =
    z.type === "order_block"
      ? z.mitigated
        ? " mit"
        : ""
      : z.fully_filled
      ? " fill"
      : "";
  return `${kind}${arrow}${tf}${spent}`;
}

const TEXT_STYLE = {
  size: 10,
  weight: "bold",
  backgroundColor: "transparent",
  borderColor: "transparent",
  borderSize: 0,
  paddingLeft: 0,
  paddingRight: 0,
  paddingTop: 0,
  paddingBottom: 0,
} as const;

let registered = false;

export function ensureDetectionOverlayRegistered(): void {
  if (registered) return;
  registered = true;
  registerOverlay({
    name: OVERLAY_NAME,
    totalStep: 1, // no interactive drawing — created programmatically
    lock: true, // non-interactive: suppresses klinecharts' blue point handles
    needDefaultPointFigure: false,
    needDefaultXAxisFigure: false,
    needDefaultYAxisFigure: false,
    // coordinates carry 2 points per zone (origin, as-of); extendData is
    // {zones, mode}. Draw everything in a single pass.
    // mode "boxes": filled rect + border (original). mode "subtle": only thin
    // top/bottom edge lines, no fill — candles stay fully readable.
    createPointFigures: ({ coordinates, overlay }) => {
      const ext = (overlay.extendData as { zones: DetectionZone[]; mode: DetectionStyle }) ?? { zones: [], mode: "boxes" };
      const { zones, mode } = ext;
      const figures: unknown[] = [];
      for (let i = 0; i < zones.length; i++) {
        const c0 = coordinates[2 * i];
        const c1 = coordinates[2 * i + 1];
        if (!c0 || !c1) continue;
        const z = zones[i];
        const { fill, border } = zoneColors(z);
        const xLeft = Math.min(c0.x, c1.x);
        const xRight = Math.max(c0.x, c1.x);
        const yTop = Math.min(c0.y, c1.y);
        const yBottom = Math.max(c0.y, c1.y);
        const w = xRight - xLeft;
        const h = yBottom - yTop;
        if (mode === "subtle") {
          for (const y of [yTop, yBottom]) {
            figures.push({
              type: "line",
              attrs: { coordinates: [{ x: xLeft, y }, { x: xRight, y }] },
              styles: { color: border, size: isHtf(z) ? 2 : 1 },
              ignoreEvent: true,
            });
          }
        } else {
          figures.push({
            type: "rect",
            attrs: { x: xLeft, y: yTop, width: w, height: h },
            styles: {
              style: "stroke_fill",
              color: fill,
              borderColor: border,
              borderSize: isHtf(z) ? 2 : 1, // HTF bias zones drawn heavier
              borderRadius: 2,
            },
            ignoreEvent: true,
          });
        }
        figures.push({
          // Anchored to the as-of (right) edge so it stays visible when the zone
          // origin scrolls off-screen left. bg/border forced transparent —
          // klinecharts' default text style paints a blue chip otherwise.
          type: "text",
          attrs: { x: xRight - 3, y: yTop + 2, text: label(z), align: "right", baseline: "top" },
          styles: { ...TEXT_STYLE, color: border },
          ignoreEvent: true,
        });
      }
      return figures as never;
    },
  });
}

// Clear and (re)draw all detection zones as-of `asOfMs` as one overlay.
export function renderDetections(
  chart: Chart,
  zones: DetectionZone[],
  asOfMs: number,
  mode: DetectionStyle = "boxes",
): void {
  chart.removeOverlay({ groupId: GROUP_ID });
  if (!zones.length) return;
  const points: { timestamp: number; value: number }[] = [];
  for (const z of zones) {
    // point pair per zone: origin @ high (top-left), as-of bar @ low (bottom-right)
    points.push({ timestamp: z.timestamp, value: z.high });
    points.push({ timestamp: asOfMs, value: z.low });
  }
  chart.createOverlay({
    name: OVERLAY_NAME,
    groupId: GROUP_ID,
    lock: true,
    extendData: { zones, mode },
    points,
  });
}

export function clearDetections(chart: Chart): void {
  chart.removeOverlay({ groupId: GROUP_ID });
}
