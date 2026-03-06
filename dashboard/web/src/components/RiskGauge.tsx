"use client";

import { usePolling } from "@/lib/hooks";
import type { RiskState } from "@/lib/api";

function ArcGauge({ label, value, max, threshold }: { label: string; value: number; max: number; threshold: number }) {
  const pct = Math.min(value / max, 1);
  const radius = 36;
  const circumference = Math.PI * radius;
  const offset = circumference * (1 - pct);

  // Color: white below threshold, warning near, red at limit
  const ratio = value / max;
  let color: string;
  let glow: string;
  if (ratio >= 0.9) {
    color = "var(--short)";
    glow = "drop-shadow(0 0 4px rgba(239,68,68,0.4))";
  } else if (ratio >= threshold / max) {
    color = "var(--warning)";
    glow = "drop-shadow(0 0 3px rgba(245,158,11,0.3))";
  } else {
    color = "var(--text-primary)";
    glow = "drop-shadow(0 0 2px rgba(255,255,255,0.15))";
  }

  return (
    <div style={{ textAlign: "center" }}>
      <svg width="90" height="55" viewBox="0 0 90 55" style={{ filter: glow }}>
        <path
          d="M 9 50 A 36 36 0 0 1 81 50"
          fill="none"
          stroke="rgba(255,255,255,0.06)"
          strokeWidth="5"
          strokeLinecap="round"
        />
        <path
          d="M 9 50 A 36 36 0 0 1 81 50"
          fill="none"
          stroke={color}
          strokeWidth="5"
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          style={{ transition: "stroke-dashoffset 0.5s ease" }}
        />
        <text x="45" y="44" textAnchor="middle" fill="var(--text-primary)" fontSize="14" fontWeight="700" fontFamily="inherit">
          {(value * 100).toFixed(1)}%
        </text>
      </svg>
      <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: -4 }}>{label}</div>
    </div>
  );
}

export function RiskGauge() {
  const { data: risk } = usePolling<RiskState>("/risk", 5000);

  const dailyDD = risk?.daily_dd_pct ?? 0;
  const weeklyDD = risk?.weekly_dd_pct ?? 0;
  const openPos = risk?.open_positions ?? 0;
  const maxPos = risk?.max_positions ?? 3;

  return (
    <div>
      <div className="card-title">Risk State</div>
      <div style={{ display: "flex", justifyContent: "space-around", marginBottom: 12 }}>
        <ArcGauge label="Daily DD" value={Math.abs(dailyDD)} max={0.03} threshold={0.02} />
        <ArcGauge label="Weekly DD" value={Math.abs(weeklyDD)} max={0.05} threshold={0.04} />
      </div>
      <div style={{ textAlign: "center", fontSize: 13 }}>
        <span style={{ color: "var(--text-muted)" }}>Positions: </span>
        <span style={{ fontWeight: 700 }}>{openPos}</span>
        <span style={{ color: "var(--text-muted)" }}> / {maxPos}</span>
      </div>
    </div>
  );
}
