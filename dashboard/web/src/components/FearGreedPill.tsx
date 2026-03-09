"use client";

import { usePolling } from "@/lib/hooks";
import type { SentimentData } from "@/lib/api";

function getColor(score: number): string {
  if (score <= 25) return "var(--short)";
  if (score <= 45) return "var(--warning)";
  if (score <= 55) return "var(--text-secondary)";
  if (score <= 75) return "#a3e635";
  return "var(--long)";
}

function getBg(score: number): string {
  if (score <= 25) return "rgba(239, 68, 68, 0.15)";
  if (score <= 45) return "rgba(245, 158, 11, 0.15)";
  if (score <= 55) return "rgba(255, 255, 255, 0.06)";
  if (score <= 75) return "rgba(163, 230, 53, 0.15)";
  return "rgba(16, 185, 129, 0.15)";
}

function getBorder(score: number): string {
  if (score <= 25) return "rgba(239, 68, 68, 0.3)";
  if (score <= 45) return "rgba(245, 158, 11, 0.3)";
  if (score <= 55) return "var(--border)";
  if (score <= 75) return "rgba(163, 230, 53, 0.3)";
  return "rgba(16, 185, 129, 0.3)";
}

export function FearGreedPill() {
  const { data } = usePolling<SentimentData>("/sentiment", 60000);

  if (!data?.score) return null;

  const score = data.score;
  const label = data.label ?? "";
  // Mobile: abbreviate label
  const shortLabel = label.replace("Extreme ", "Ext ");

  return (
    <span
      className="fg-pill"
      title={`Fear & Greed: ${score}/100 (${label})`}
      style={{
        padding: "3px 10px",
        borderRadius: 100,
        fontSize: 11,
        fontWeight: 700,
        letterSpacing: "0.05em",
        background: getBg(score),
        color: getColor(score),
        border: `1px solid ${getBorder(score)}`,
        whiteSpace: "nowrap",
      }}
    >
      <span className="fg-full">F&G: {score}</span>
      <span className="fg-short" style={{ display: "none" }}>F&G: {score}</span>
    </span>
  );
}
