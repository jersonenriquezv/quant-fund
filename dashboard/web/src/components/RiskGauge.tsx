"use client";

import { usePolling } from "@/lib/hooks";
import type { RiskState } from "@/lib/api";

function ArcGauge({ label, value, max, color }: { label: string; value: number; max: number; color: string }) {
  const pct = Math.min(value / max, 1);
  const radius = 36;
  const circumference = Math.PI * radius; // half circle
  const offset = circumference * (1 - pct);

  return (
    <div style={{ textAlign: "center" }}>
      <svg width="90" height="55" viewBox="0 0 90 55">
        {/* Background arc */}
        <path
          d="M 9 50 A 36 36 0 0 1 81 50"
          fill="none"
          stroke="var(--bg-secondary)"
          strokeWidth="5"
          strokeLinecap="round"
        />
        {/* Filled arc */}
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
        <ArcGauge label="Daily DD" value={Math.abs(dailyDD)} max={0.03} color={Math.abs(dailyDD) > 0.02 ? "var(--short)" : "var(--warning)"} />
        <ArcGauge label="Weekly DD" value={Math.abs(weeklyDD)} max={0.05} color={Math.abs(weeklyDD) > 0.04 ? "var(--short)" : "var(--warning)"} />
      </div>
      <div style={{ textAlign: "center", fontSize: 13 }}>
        <span style={{ color: "var(--text-muted)" }}>Positions: </span>
        <span style={{ fontWeight: 700 }}>{openPos}</span>
        <span style={{ color: "var(--text-muted)" }}> / {maxPos}</span>
      </div>
    </div>
  );
}
