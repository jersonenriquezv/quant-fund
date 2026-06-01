// Registers a klinecharts custom overlay that draws a bot-detection zone
// (Order Block / FVG) as a colored rectangle spanning [origin bar -> as-of bar]
// across the zone's price band, with a small label. See chart-replay plan C2.

import { registerOverlay, type Chart } from "klinecharts";
import type { DetectionZone } from "@/lib/chartDatafeed";

const OVERLAY_NAME = "detectionZone";
const GROUP_ID = "detections";

// type+direction+state -> fill / border. Mitigated/filled zones are dimmed.
function zoneColors(z: DetectionZone): { fill: string; border: string } {
  const spent = z.type === "order_block" ? z.mitigated : z.fully_filled;
  const a = spent ? 0.05 : 0.14;
  const b = spent ? 0.3 : 0.7;
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

function label(z: DetectionZone): string {
  if (z.type === "order_block") {
    return `OB ${z.direction[0].toUpperCase()}${z.mitigated ? " ·mit" : ""}`;
  }
  return `FVG ${z.direction[0].toUpperCase()}${z.fully_filled ? " ·filled" : ""}`;
}

let registered = false;

export function ensureDetectionOverlayRegistered(): void {
  if (registered) return;
  registered = true;
  registerOverlay({
    name: OVERLAY_NAME,
    totalStep: 1, // no interactive drawing — created programmatically
    needDefaultPointFigure: false,
    needDefaultXAxisFigure: false,
    needDefaultYAxisFigure: false,
    createPointFigures: ({ coordinates, overlay }) => {
      if (coordinates.length < 2) return [];
      const z = overlay.extendData as DetectionZone;
      const { fill, border } = zoneColors(z);
      const x = Math.min(coordinates[0].x, coordinates[1].x);
      const yTop = Math.min(coordinates[0].y, coordinates[1].y);
      const w = Math.abs(coordinates[1].x - coordinates[0].x);
      const h = Math.abs(coordinates[1].y - coordinates[0].y);
      return [
        {
          type: "rect",
          attrs: { x, y: yTop, width: w, height: h },
          styles: {
            style: "stroke_fill",
            color: fill,
            borderColor: border,
            borderSize: 1,
          },
          ignoreEvent: true,
        },
        {
          type: "text",
          attrs: { x: x + 4, y: yTop + 2, text: label(z), baseline: "top" },
          styles: { color: border, size: 10 },
          ignoreEvent: true,
        },
      ];
    },
  });
}

// Clear and redraw all detection zones as-of `asOfMs`.
export function renderDetections(
  chart: Chart,
  zones: DetectionZone[],
  asOfMs: number,
): void {
  chart.removeOverlay({ groupId: GROUP_ID });
  for (const z of zones) {
    chart.createOverlay({
      name: OVERLAY_NAME,
      groupId: GROUP_ID,
      extendData: z,
      // point1 = zone origin @ high (top-left), point2 = as-of bar @ low (bottom-right)
      points: [
        { timestamp: z.timestamp, value: z.high },
        { timestamp: asOfMs, value: z.low },
      ],
    });
  }
}

export function clearDetections(chart: Chart): void {
  chart.removeOverlay({ groupId: GROUP_ID });
}
