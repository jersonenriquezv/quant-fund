"use client";

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

function ConfidenceBar({ confidence }: { confidence: number | null }) {
  const val = confidence ?? 0;
  const pct = Math.max(0, Math.min(100, val * 100));
  const approved = val >= 0.60;

  // Gradient from red (0) through yellow (0.5) to green (1)
  let color: string;
  if (val < 0.4) color = "var(--short)";
  else if (val < 0.6) color = "var(--warning)";
  else color = "var(--long)";

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <div className="conf-bar" style={{ flex: 1 }}>
        <div className="conf-bar-fill" style={{ width: `${pct}%`, background: color }} />
      </div>
      <span style={{ fontSize: 11, fontWeight: 600, color, fontVariantNumeric: "tabular-nums", minWidth: 36, textAlign: "right" }}>
        {(val * 100).toFixed(0)}%
      </span>
      <span style={{ fontSize: 10, color: approved ? "var(--long)" : "var(--short)" }}>
        {approved ? "PASS" : "FAIL"}
      </span>
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
          <div className="skeleton" style={{ height: 16, width: "100%", marginBottom: 8 }} />
        )}
        {decisions?.map((d) => (
          <div key={d.id} className="animate-in" style={{
            padding: "8px 0",
            borderBottom: "1px solid var(--border)",
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
              <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
                {formatTime(d.created_at)}
              </span>
              {d.trade_id && (
                <span style={{ fontSize: 10, color: "var(--accent)" }}>
                  #{d.trade_id}
                </span>
              )}
            </div>
            <ConfidenceBar confidence={d.confidence} />
            {d.reasoning && (
              <div style={{ fontSize: 11, color: "var(--text-secondary)", marginTop: 4, lineHeight: 1.4, maxHeight: 40, overflow: "hidden" }}>
                {d.reasoning}
              </div>
            )}
            {d.warnings && d.warnings.length > 0 && (
              <div style={{ fontSize: 10, color: "var(--warning)", marginTop: 2 }}>
                {d.warnings.join(" | ")}
              </div>
            )}
          </div>
        ))}
        {decisions?.length === 0 && (
          <div style={{ color: "var(--text-muted)", fontSize: 12, textAlign: "center", padding: 20 }}>
            No AI decisions yet
          </div>
        )}
      </div>
    </div>
  );
}
