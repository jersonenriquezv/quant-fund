"use client";

// Phase 1 — TradingView-style left toolbox for /chart.
// Vertical icon rail on desktop; horizontal scrollable row on mobile (≤639px,
// handled in globals.css). Pure UI: page.tsx owns the tool state machine.

import type { DrawingToolId } from "@/lib/drawingTools";

export type ToolboxAction = "cursor" | DrawingToolId | "long" | "short" | "clear";

interface ToolDef {
  id: ToolboxAction;
  label: string;
  icon: React.ReactNode;
  className?: string;
}

const S = { fill: "none", stroke: "currentColor", strokeWidth: 1.5, strokeLinecap: "round" as const };

const TOOLS: ToolDef[] = [
  {
    id: "cursor",
    label: "Cursor (Esc)",
    icon: (
      <svg width="16" height="16" viewBox="0 0 16 16">
        <path d="M4 2 L12 9 L8.5 9.5 L10.5 13.5 L8.8 14.3 L6.8 10.3 L4 12.5 Z" fill="currentColor" stroke="none" />
      </svg>
    ),
  },
  {
    id: "segment",
    label: "Trend line",
    icon: (
      <svg width="16" height="16" viewBox="0 0 16 16">
        <line x1="3" y1="13" x2="13" y2="3" {...S} />
        <circle cx="3" cy="13" r="1.5" fill="currentColor" stroke="none" />
        <circle cx="13" cy="3" r="1.5" fill="currentColor" stroke="none" />
      </svg>
    ),
  },
  {
    id: "rayLine",
    label: "Ray",
    icon: (
      <svg width="16" height="16" viewBox="0 0 16 16">
        <line x1="3" y1="13" x2="13" y2="3" {...S} />
        <circle cx="3" cy="13" r="1.5" fill="currentColor" stroke="none" />
        <path d="M13 3 L10.5 3.5 M13 3 L12.5 5.5" {...S} />
      </svg>
    ),
  },
  {
    id: "horizontalStraightLine",
    label: "Horizontal line",
    icon: (
      <svg width="16" height="16" viewBox="0 0 16 16">
        <line x1="2" y1="8" x2="14" y2="8" {...S} />
        <circle cx="8" cy="8" r="1.5" fill="currentColor" stroke="none" />
      </svg>
    ),
  },
  {
    id: "rectangleZone",
    label: "Rectangle",
    icon: (
      <svg width="16" height="16" viewBox="0 0 16 16">
        <rect x="3" y="4.5" width="10" height="7" rx="1" {...S} />
      </svg>
    ),
  },
  {
    id: "fibonacciLine",
    label: "Fib retracement",
    icon: (
      <svg width="16" height="16" viewBox="0 0 16 16">
        <line x1="2.5" y1="4" x2="13.5" y2="4" {...S} />
        <line x1="2.5" y1="8" x2="13.5" y2="8" {...S} opacity="0.6" />
        <line x1="2.5" y1="12" x2="13.5" y2="12" {...S} />
      </svg>
    ),
  },
  {
    id: "long",
    label: "Long position",
    className: "chart-tool-long",
    icon: (
      <svg width="16" height="16" viewBox="0 0 16 16">
        <path d="M3 13 L13 3 M13 3 L8.5 3 M13 3 L13 7.5" {...S} />
      </svg>
    ),
  },
  {
    id: "short",
    label: "Short position",
    className: "chart-tool-short",
    icon: (
      <svg width="16" height="16" viewBox="0 0 16 16">
        <path d="M3 3 L13 13 M13 13 L8.5 13 M13 13 L13 8.5" {...S} />
      </svg>
    ),
  },
  {
    id: "clear",
    label: "Clear drawings",
    className: "chart-tool-clear",
    icon: (
      <svg width="16" height="16" viewBox="0 0 16 16">
        <path d="M3.5 4.5 H12.5 M6.5 4.5 V3 H9.5 V4.5 M4.5 4.5 L5.2 13.5 H10.8 L11.5 4.5" {...S} />
      </svg>
    ),
  },
];

interface Props {
  active: ToolboxAction;
  onSelect: (tool: ToolboxAction) => void;
}

export default function ChartToolbar({ active, onSelect }: Props) {
  return (
    <div className="chart-toolbox" role="toolbar" aria-label="Drawing tools">
      {TOOLS.map((t) => (
        <button
          key={t.id}
          className={`chart-tool-btn ${t.className ?? ""} ${active === t.id ? "active" : ""}`}
          title={t.label}
          aria-label={t.label}
          onClick={() => onSelect(t.id)}
        >
          {t.icon}
        </button>
      ))}
    </div>
  );
}
