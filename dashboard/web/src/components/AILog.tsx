"use client";

import { useState } from "react";
import { usePolling } from "@/lib/hooks";
import type { AIDecision } from "@/lib/api";

function formatTime(ts: string | null): string {
  if (!ts) return "--";
  try {
    const d = new Date(ts);
    return d.toISOString().replace("T", " ").slice(5, 16);
  } catch {
    return "--";
  }
}

function ConfidenceRing({ confidence }: { confidence: number | null }) {
  const val = confidence ?? 0;
  const pct = Math.max(0, Math.min(1, val));
  const radius = 16;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference * (1 - pct);

  let color: string;
  if (val < 0.4) color = "var(--short)";
  else if (val < 0.6) color = "var(--warning)";
  else color = "var(--long)";

  return (
    <div style={{ position: "relative", width: 40, height: 40, flexShrink: 0 }}>
      <svg width="40" height="40" viewBox="0 0 40 40">
        <circle cx="20" cy="20" r={radius} fill="none" stroke="rgba(255,255,255,0.06)" strokeWidth="3" />
        <circle
          cx="20" cy="20" r={radius} fill="none"
          stroke={color} strokeWidth="3" strokeLinecap="round"
          strokeDasharray={circumference} strokeDashoffset={offset}
          transform="rotate(-90 20 20)"
          style={{ transition: "stroke-dashoffset 0.3s ease" }}
        />
      </svg>
      <div style={{
        position: "absolute", inset: 0,
        display: "flex", alignItems: "center", justifyContent: "center",
        fontSize: 10, fontWeight: 700, color, fontVariantNumeric: "tabular-nums",
      }}>
        {(val * 100).toFixed(0)}
      </div>
    </div>
  );
}

function AICard({ d }: { d: AIDecision }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="ai-card animate-in">
      <div style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
        <ConfidenceRing confidence={d.confidence} />
        <div style={{ flex: 1, minWidth: 0 }}>
          {/* Header row */}
          <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap", marginBottom: 4 }}>
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
              {formatTime(d.created_at)}
            </span>
            {d.pair && (
              <span style={{ fontSize: 11, fontWeight: 600, color: "var(--text-primary)" }}>
                {d.pair}
              </span>
            )}
            {d.direction && (
              <span className={`badge ${d.direction === "long" ? "badge-long" : "badge-short"}`}
                style={{ fontSize: 9, padding: "1px 6px" }}>
                {d.direction.toUpperCase()}
              </span>
            )}
            {d.setup_type && (
              <span className="badge badge-neutral" style={{ fontSize: 9, padding: "1px 6px" }}>
                {d.setup_type}
              </span>
            )}
            {d.approved !== null && (
              <span style={{
                fontSize: 9, padding: "1px 6px", borderRadius: 100, fontWeight: 600,
                background: d.approved ? "rgba(16,185,129,0.12)" : "rgba(239,68,68,0.12)",
                color: d.approved ? "var(--long)" : "var(--short)",
              }}>
                {d.approved ? "APPROVED" : "REJECTED"}
              </span>
            )}
            {d.trade_id && (
              <span style={{ fontSize: 10, color: "var(--accent)" }}>
                #{d.trade_id}
              </span>
            )}
          </div>

          {/* Reasoning */}
          {d.reasoning && (
            <div
              className={`ai-reasoning ${expanded ? "" : "collapsed"}`}
              onClick={() => setExpanded(!expanded)}
              style={{ marginBottom: 4 }}
            >
              {d.reasoning}
            </div>
          )}

          {/* Warnings as pills */}
          {d.warnings && d.warnings.length > 0 && (
            <div>
              {d.warnings.map((w, i) => (
                <span key={i} className="warning-pill">{w}</span>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export function AILog() {
  const { data: decisions, loading } = usePolling<AIDecision[]>("/ai/decisions?limit=15", 10000);

  return (
    <div>
      <div className="card-title">AI Decisions</div>
      <div className="scroll-y">
        {loading && !decisions && (
          <div className="skeleton" style={{ height: 60, width: "100%", marginBottom: 8 }} />
        )}
        {decisions?.map((d) => (
          <AICard key={d.id} d={d} />
        ))}
        {decisions?.length === 0 && (
          <div style={{
            color: "var(--text-muted)", fontSize: 12, textAlign: "center",
            padding: "24px 12px", lineHeight: 1.6,
          }}>
            No AI evaluations yet — decisions appear when the bot detects a setup
          </div>
        )}
      </div>
    </div>
  );
}
