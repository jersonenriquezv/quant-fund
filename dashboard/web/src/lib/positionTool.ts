// A6 — Long/Short position practice tool (custom klinecharts overlay).
//
// One interactive overlay with three draggable price handles: entry, stop-loss,
// take-profit. It draws a red risk box (entry->SL) and a green reward box
// (entry->TP) extending to the right edge, with price/percent labels and a live
// R:R readout. Pure practice: no persistence, no order placement (grill scope).
//
// Direction is implied by geometry, so dragging a handle through the entry flips
// long<->short automatically (reward side stays green, risk side stays red). The
// initial Long/Short button only seeds the default SL/TP offsets.

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
// display; this just mirrors it into React on drag end.
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

let registered = false;

export function ensurePositionOverlayRegistered(): void {
  if (registered) return;
  registered = true;
  registerOverlay({
    name: OVERLAY_NAME,
    totalStep: 1, // created programmatically; handles are draggable afterward
    needDefaultPointFigure: true, // show the draggable price handles
    needDefaultXAxisFigure: false,
    needDefaultYAxisFigure: true, // price tag on the Y axis for each handle
    // Notify React of the new R:R after a drag settles (cheap; not per-frame).
    onPressedMoveEnd: ({ overlay }) => {
      if (rrListener) {
        rrListener(
          computeRR(priceOf(overlay, I_ENTRY), priceOf(overlay, I_SL), priceOf(overlay, I_TP)),
        );
      }
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

      // Box spans from the entry handle's bar to the right edge of the pane.
      const xLeft = cE.x;
      const xRight = bounding.width;
      const w = Math.max(0, xRight - xLeft);

      const riskTop = Math.min(cE.y, cS.y);
      const riskH = Math.abs(cS.y - cE.y);
      const rewardTop = Math.min(cE.y, cT.y);
      const rewardH = Math.abs(cT.y - cE.y);

      const slPct = entry ? ((sl - entry) / entry) * 100 : 0;
      const tpPct = entry ? ((tp - entry) / entry) * 100 : 0;
      const rr = computeRR(entry, sl, tp);

      const figures: unknown[] = [
        {
          type: "rect",
          attrs: { x: xLeft, y: rewardTop, width: w, height: rewardH },
          styles: { style: "stroke_fill", color: REWARD_FILL, borderColor: REWARD_LINE, borderSize: 1 },
          ignoreEvent: true,
        },
        {
          type: "rect",
          attrs: { x: xLeft, y: riskTop, width: w, height: riskH },
          styles: { style: "stroke_fill", color: RISK_FILL, borderColor: RISK_LINE, borderSize: 1 },
          ignoreEvent: true,
        },
        // Entry line spanning the box. All labels are RIGHT-anchored so they stay
        // on-screen even when the box's left edge scrolls off (mirrors the
        // detection overlay). One chip per line + entry carries the live R:R.
        {
          type: "line",
          attrs: { coordinates: [{ x: xLeft, y: cE.y }, { x: xRight, y: cE.y }] },
          styles: { color: ENTRY_LINE, size: 1, style: "dashed" },
          ignoreEvent: true,
        },
        {
          type: "text",
          attrs: { x: xRight - 4, y: cT.y + (cT.y < cE.y ? -2 : 2), text: `TP ${fmt(tp, dp)} (${tpPct >= 0 ? "+" : ""}${tpPct.toFixed(2)}%)`, align: "right", baseline: cT.y < cE.y ? "bottom" : "top" },
          styles: { color: REWARD_LINE, size: 11, weight: "bold", backgroundColor: "rgba(0,0,0,0.35)", borderColor: "transparent", paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2 },
          ignoreEvent: true,
        },
        {
          type: "text",
          attrs: { x: xRight - 4, y: cS.y + (cS.y < cE.y ? -2 : 2), text: `SL ${fmt(sl, dp)} (${slPct >= 0 ? "+" : ""}${slPct.toFixed(2)}%)`, align: "right", baseline: cS.y < cE.y ? "bottom" : "top" },
          styles: { color: RISK_LINE, size: 11, weight: "bold", backgroundColor: "rgba(0,0,0,0.35)", borderColor: "transparent", paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2 },
          ignoreEvent: true,
        },
        {
          type: "text",
          attrs: { x: xRight - 4, y: cE.y - 2, text: `Entry ${fmt(entry, dp)} · R:R ${rr != null ? rr.toFixed(2) : "—"}`, align: "right", baseline: "bottom" },
          styles: { color: rr != null && rr >= 1 ? REWARD_LINE : RISK_LINE, size: 12, weight: "bold", backgroundColor: "rgba(0,0,0,0.45)", borderColor: "transparent", paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2 },
          ignoreEvent: true,
        },
      ];
      return figures as never;
    },
  });
}

export interface PositionSeed {
  direction: "long" | "short";
  entry: number;
  anchorTs: number; // bar timestamp for the box's left edge
}

// Create (or replace) the practice position. Default SL/TP offsets: 1% risk,
// 2% reward (2R) — the user drags from there.
let lastOverlayId: string | null = null;
export function getPositionOverlayId(): string | null {
  return lastOverlayId;
}

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
