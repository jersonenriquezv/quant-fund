"use client";

import { usePolling } from "@/lib/hooks";
import type { HeadlinesData } from "@/lib/api";

function timeAgo(ms: number): string {
  const diff = Date.now() - ms;
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "now";
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

function sentimentColor(s: string | null): string {
  if (s === "bullish") return "var(--long)";
  if (s === "bearish") return "var(--short)";
  return "var(--text-muted)";
}

export function NewsPanel() {
  const { data, loading } = usePolling<HeadlinesData>("/headlines", 300000);

  const headlines = data?.headlines ?? [];

  return (
    <>
      <div className="card-title">Recent News</div>
      {loading && !data ? (
        <div style={{ color: "var(--text-muted)", fontSize: 12 }}>Loading...</div>
      ) : headlines.length === 0 ? (
        <div style={{ color: "var(--text-muted)", fontSize: 12 }}>No headlines</div>
      ) : (
        <div className="scroll-y news-list">
          {headlines.map((h, i) => (
            <a
              key={i}
              className="news-item"
              href={h.url || undefined}
              target="_blank"
              rel="noopener noreferrer"
            >
              <div className="news-header">
                <span className="news-badge" style={{ color: sentimentColor(h.sentiment) }}>
                  {h.category}
                </span>
                <span className="news-source">{h.source}</span>
                <span className="news-time">{timeAgo(h.timestamp)}</span>
              </div>
              <div className="news-title">{h.title}</div>
            </a>
          ))}
        </div>
      )}
    </>
  );
}
