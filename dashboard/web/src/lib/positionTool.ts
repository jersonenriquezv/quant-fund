// A6 — Long/Short position practice tool (TradingView-style custom overlay).
//
// One interactive overlay with three price levels (entry / SL / TP). It behaves
// like TradingView's Long/Short Position tool:
//   - drag the ENTRY line  -> the whole position moves (SL & TP follow)
//   - drag the SL line     -> stop adjusts independently (R:R recomputes)
//   - drag the TP line      -> target adjusts independently (R:R recomputes)
// Lines span the full width and are grabbable anywhere (a visible round handle
// marks each). Direction is implied by geometry, so dragging a handle through
// the entry flips long<->short automatically (reward stays green, risk red).
//
// Placement is click-to-place (see page.tsx): the toolbar arms the tool, the
// next click on the chart drops the entry there via convertFromPixel.
//
// Pure practice: no persistence, no order placement (grill scope).

import { registerOverlay, type Chart, type Overlay } from "klinecharts";

const OVERLAY_NAME = "positionTool";
const GROUP_ID = "position";

// Point order is fixed: 0 = entry, 1 = stop-loss, 2 = take-profit.
const I_ENTRY = 0;
const I_SL = 1;
const I_TP = 2;

const RISK_FILL = "rgba(255,77,77,0.10)";
const RISK_LINE = "rgba(255,77,77,0.90)";
const REWARD_FILL = "rgba(178,253,2,0.10)";
const REWARD_LINE = "rgba(178,253,2,0.90)";
const ENTRY_LINE = "rgba(255,255,255,0.85)";

// Live R:R subscribers (toolbar readout). The overlay label is the primary
// display; this mirrors it into React on create + while dragging.
type RRListener = (rr: number | null) => void;
let rrListener: RRListener | null = null;
export function onPositionChange(cb: RRListener | null): void {
  rrListener = cb;
}

function priceOf(overlay: Overlay, i: number): number {
  return (overlay.points[i]?.value as number) ?? 0;
}

function fmt(price: number, decimals: number): string {
  return price.toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function computeRR(entry: number, sl: number, tp: number): number | null {
  const risk = Math.abs(entry - sl);
  if (risk <= 0) return null;
  return Math.abs(tp - entry) / risk;
}

function emitRR(overlay: Overlay): void {
  if (rrListener) {
    rrListener(computeRR(priceOf(overlay, I_ENTRY), priceOf(overlay, I_SL), priceOf(overlay, I_TP)));
  }
}

let registered = false;

export function ensurePositionOverlayRegistered(): void {
  if (registered) return;
  registered = true;
  registerOverlay({
    name: OVERLAY_NAME,
    totalStep: 1, // created programmatically (points supplied at once)
    needDefaultPointFigure: true, // draggable handles at each price point
    needDefaultXAxisFigure: false,
    needDefaultYAxisFigure: true, // price tag on the Y axis per handle
    // Visible, easy-to-grab round handles (default klinecharts dots are tiny and
    // hidden until selected). styleable point figure.
    styles: {
      point: {
        color: "#ffffff",
        borderColor: "rgba(0,0,0,0.6)",
        borderSize: 1,
        radius: 5,
        activeColor: "#ffffff",
        activeBorderColor: "rgba(255,255,255,0.4)",
        activeBorderSize: 3,
        activeRadius: 7,
      },
    },
    // TradingView drag semantics: moving the entry translates SL & TP by the same
    // delta (whole position moves); moving SL or TP adjusts only that level.
    // klinecharts applies performPoint to points[performPointIndex] itself, so we
    // only need to shift the OTHER points when the entry is the one being dragged.
    performEventPressedMove: ({ points, performPointIndex, performPoint }) => {
      if (performPointIndex !== I_ENTRY) return;
      const oldEntry = points[I_ENTRY]?.value ?? 0;
      const oldTs = points[I_ENTRY]?.timestamp ?? 0;
      const dv = (performPoint.value ?? 0) - oldEntry;
      const dt = (performPoint.timestamp ?? 0) - oldTs;
      points[I_SL] = {
        timestamp: (points[I_SL]?.timestamp ?? 0) + dt,
        value: (points[I_SL]?.value ?? 0) + dv,
      };
      points[I_TP] = {
        timestamp: (points[I_TP]?.timestamp ?? 0) + dt,
        value: (points[I_TP]?.value ?? 0) + dv,
      };
    },
    onPressedMoving: ({ overlay }) => {
      emitRR(overlay);
      return false;
    },
    onPressedMoveEnd: ({ overlay }) => {
      emitRR(overlay);
      return false;
    },
    createPointFigures: ({ overlay, coordinates, bounding, precision }) => {
      const cE = coordinates[I_ENTRY];
      const cS = coordinates[I_SL];
      const cT = coordinates[I_TP];
      if (!cE || !cS || !cT) return [];

      const entry = priceOf(overlay, I_ENTRY);
      const sl = priceOf(overlay, I_SL);
      const tp = priceOf(overlay, I_TP);
      const dp = precision?.price ?? 2;

      // Boxes/lines start at the anchor bar (where the position was placed) and
      // extend to the right edge — they do NOT span the whole chart. The handle
      // dots sit at that same left edge.
      const xRight = bounding.width;
      const xLeft = Math.min(cE.x, cS.x, cT.x);
      const w = Math.max(0, xRight - xLeft);

      const riskTop = Math.min(cE.y, cS.y);
      const riskH = Math.abs(cS.y - cE.y);
      const rewardTop = Math.min(cE.y, cT.y);
      const rewardH = Math.abs(cT.y - cE.y);

      const slPct = entry ? ((sl - entry) / entry) * 100 : 0;
      const tpPct = entry ? ((tp - entry) / entry) * 100 : 0;
      const rr = computeRR(entry, sl, tp);

      const lineFig = (y: number, color: string, key: string) => ({
        type: "line",
        attrs: { coordinates: [{ x: xLeft, y }, { x: xRight, y }] },
        styles: { color, size: key === "entry" ? 1 : 1, style: key === "entry" ? "dashed" : "solid" },
        // NOT ignoreEvent: pressing a line selects the overlay so its handles
        // become draggable (klinecharts hides handles until the overlay is hit).
      });
      const labelFig = (y: number, text: string, color: string, above: boolean) => ({
        type: "text",
        attrs: { x: xRight - 4, y: y + (above ? -2 : 2), text, align: "right", baseline: above ? "bottom" : "top" },
        styles: { color, size: 11, weight: "bold", backgroundColor: "rgba(0,0,0,0.4)", borderColor: "transparent", paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2 },
        ignoreEvent: true,
      });

      return [
        { type: "rect", attrs: { x: xLeft, y: rewardTop, width: w, height: rewardH }, styles: { style: "fill", color: REWARD_FILL }, ignoreEvent: true },
        { type: "rect", attrs: { x: xLeft, y: riskTop, width: w, height: riskH }, styles: { style: "fill", color: RISK_FILL }, ignoreEvent: true },
        lineFig(cT.y, REWARD_LINE, "tp"),
        lineFig(cS.y, RISK_LINE, "sl"),
        lineFig(cE.y, ENTRY_LINE, "entry"),
        labelFig(cT.y, `TP ${fmt(tp, dp)} (${tpPct >= 0 ? "+" : ""}${tpPct.toFixed(2)}%)`, REWARD_LINE, cT.y < cE.y),
        labelFig(cS.y, `SL ${fmt(sl, dp)} (${slPct >= 0 ? "+" : ""}${slPct.toFixed(2)}%)`, RISK_LINE, cS.y < cE.y),
        labelFig(cE.y, `Entry ${fmt(entry, dp)} · R:R ${rr != null ? rr.toFixed(2) : "—"}`, rr != null && rr >= 1 ? REWARD_LINE : RISK_LINE, true),
      ] as never;
    },
  });
}

export interface PositionSeed {
  direction: "long" | "short";
  entry: number;
  anchorTs: number; // bar timestamp the handles anchor to (x of the dots)
}

let lastOverlayId: string | null = null;
export function getPositionOverlayId(): string | null {
  return lastOverlayId;
}

// Create (or replace) the practice position. Default offsets: 1% risk, 2% reward
// (2R) — the user drags from there.
export function createPosition(chart: Chart, seed: PositionSeed): void {
  chart.removeOverlay({ groupId: GROUP_ID });
  const { direction, entry, anchorTs } = seed;
  const sl = direction === "long" ? entry * 0.99 : entry * 1.01;
  const tp = direction === "long" ? entry * 1.02 : entry * 0.98;
  const id = chart.createOverlay({
    name: OVERLAY_NAME,
    groupId: GROUP_ID,
    points: [
      { timestamp: anchorTs, value: entry },
      { timestamp: anchorTs, value: sl },
      { timestamp: anchorTs, value: tp },
    ],
  });
  lastOverlayId = typeof id === "string" ? id : null;
  if (process.env.NODE_ENV !== "production") {
    (globalThis as unknown as { __qfPosId?: string | null }).__qfPosId = lastOverlayId; // dev/test
  }
  if (rrListener) rrListener(computeRR(entry, sl, tp));
}

export function clearPosition(chart: Chart): void {
  chart.removeOverlay({ groupId: GROUP_ID });
  if (rrListener) rrListener(null);
}
