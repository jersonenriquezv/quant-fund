// Phase 1 — TradingView-style drawing toolbox.
//
// Wraps klinecharts' built-in drawing overlays (segment, rayLine,
// horizontalStraightLine, fibonacciLine) plus a custom 2-point rectangle, and
// adds what the built-ins lack:
//   - per-symbol persistence (localStorage) so drawings survive reloads and
//     symbol switches, TradingView-style
//   - right-click on a drawing removes it (browser context menu suppressed by
//     the canvas container)
//   - cancel of an in-progress drawing (Esc / picking another tool)
//
// Drawings are practice/annotation only — nothing here touches bot data.

import { registerOverlay, type Chart, type OverlayEvent } from "klinecharts";

export type DrawingToolId =
  | "segment"
  | "rayLine"
  | "horizontalStraightLine"
  | "rectangleZone"
  | "fibonacciLine";

const GROUP_ID = "drawing";
const STORE_PREFIX = "qf-chart-drawings:";
const ACCENT = "#4da3ff";

// Shared look for every drawing overlay (klinecharts merges per-overlay styles
// over the chart defaults). Handles mirror the position tool's, slightly smaller.
const DRAWING_STYLES = {
  line: { color: ACCENT, size: 1 },
  text: { color: ACCENT, size: 11, backgroundColor: "transparent", borderColor: "transparent" },
  point: {
    color: ACCENT,
    borderColor: "rgba(0,0,0,0.6)",
    borderSize: 1,
    radius: 4,
    activeColor: ACCENT,
    activeBorderColor: "rgba(77,163,255,0.4)",
    activeBorderSize: 2,
    activeRadius: 6,
  },
};

let registered = false;

export function ensureDrawingOverlaysRegistered(): void {
  if (registered) return;
  registered = true;
  // klinecharts has a `rect` FIGURE but no built-in rectangle OVERLAY — this is
  // the standard 2-point box (click corner, click opposite corner).
  registerOverlay({
    name: "rectangleZone",
    totalStep: 3,
    needDefaultPointFigure: true,
    needDefaultXAxisFigure: false,
    needDefaultYAxisFigure: false,
    createPointFigures: ({ coordinates }) => {
      if (coordinates.length < 2) return [];
      const [a, b] = coordinates;
      return [
        {
          type: "rect",
          attrs: {
            x: Math.min(a.x, b.x),
            y: Math.min(a.y, b.y),
            width: Math.abs(b.x - a.x),
            height: Math.abs(b.y - a.y),
          },
          styles: {
            style: "stroke_fill",
            color: "rgba(77,163,255,0.10)",
            borderColor: "rgba(77,163,255,0.9)",
            borderSize: 1,
          },
        },
      ] as never;
    },
  });
}

// ---------------------------------------------------------------------------
// Persistence — drawings are stored per symbol as {name, points} and recreated
// on restore. Overlay ids are session-scoped, so we keep our own id→name map.
// ---------------------------------------------------------------------------

interface StoredDrawing {
  name: string;
  points: Array<{ timestamp?: number; value?: number }>;
}

const live = new Map<string, string>(); // overlayId -> overlay name
let pendingId: string | null = null; // drawing in progress (not all points placed)

function storeKey(symbol: string): string {
  return STORE_PREFIX + symbol;
}

function saveDrawings(chart: Chart, symbol: string): void {
  const out: StoredDrawing[] = [];
  for (const [id, name] of live) {
    const o = chart.getOverlayById(id);
    if (!o) continue;
    out.push({
      name,
      points: o.points.map((p) => ({ timestamp: p.timestamp, value: p.value })),
    });
  }
  try {
    localStorage.setItem(storeKey(symbol), JSON.stringify(out));
  } catch {
    /* storage full / private mode — drawings just won't persist */
  }
}

// OverlayEvent carries the overlay but NOT the chart instance — keep the last
// chart seen (single-chart page) and operate through it in the callbacks.
let lastChart: Chart | null = null;

function attachCallbacks(symbol: string) {
  return {
    onPressedMoveEnd: () => {
      if (lastChart) saveDrawings(lastChart, symbol);
      return false;
    },
    onRightClick: (e: OverlayEvent) => {
      if (lastChart) removeDrawing(lastChart, symbol, e.overlay.id);
      return true; // swallow so klinecharts doesn't also select it
    },
  };
}

/** Arm a drawing tool: klinecharts enters interactive placement (user clicks
 *  the points). `onDone` fires when the final point lands. */
export function startDrawing(
  chart: Chart,
  symbol: string,
  name: DrawingToolId,
  onDone: () => void,
): void {
  lastChart = chart;
  cancelPendingDrawing(chart);
  const id = chart.createOverlay({
    name,
    groupId: GROUP_ID,
    styles: DRAWING_STYLES,
    ...attachCallbacks(symbol),
    onDrawEnd: () => {
      pendingId = null;
      saveDrawings(chart, symbol);
      onDone();
      return false;
    },
  });
  if (typeof id === "string") {
    live.set(id, name);
    pendingId = id;
  }
}

/** Abort an in-progress drawing (Esc or switching tools mid-placement). */
export function cancelPendingDrawing(chart: Chart): void {
  if (!pendingId) return;
  const id = pendingId;
  pendingId = null;
  live.delete(id);
  chart.removeOverlay({ id });
}

export function removeDrawing(chart: Chart, symbol: string, id: string): void {
  if (pendingId === id) pendingId = null;
  live.delete(id);
  chart.removeOverlay({ id });
  saveDrawings(chart, symbol);
}

export function clearAllDrawings(chart: Chart, symbol: string): void {
  pendingId = null;
  live.clear();
  chart.removeOverlay({ groupId: GROUP_ID });
  try {
    localStorage.removeItem(storeKey(symbol));
  } catch {
    /* ignore */
  }
}

/** Wipe current drawings and recreate the stored set for `symbol`. Call on
 *  chart init and on every symbol switch (BTC drawings make no sense on ETH). */
export function restoreDrawings(chart: Chart, symbol: string): void {
  lastChart = chart;
  pendingId = null;
  live.clear();
  chart.removeOverlay({ groupId: GROUP_ID });
  let stored: StoredDrawing[] = [];
  try {
    stored = JSON.parse(localStorage.getItem(storeKey(symbol)) ?? "[]");
  } catch {
    return;
  }
  if (!Array.isArray(stored)) return;
  for (const d of stored) {
    if (!d || typeof d.name !== "string" || !Array.isArray(d.points) || !d.points.length) continue;
    const id = chart.createOverlay({
      name: d.name,
      groupId: GROUP_ID,
      points: d.points,
      styles: DRAWING_STYLES,
      ...attachCallbacks(symbol),
    });
    if (typeof id === "string") live.set(id, d.name);
  }
}
