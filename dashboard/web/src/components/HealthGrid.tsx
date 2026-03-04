"use client";

import { usePolling } from "@/lib/hooks";
import type { HealthData } from "@/lib/api";

interface HealthItem {
  label: string;
  ok: boolean;
}

export function HealthGrid() {
  const { data: health } = usePolling<HealthData>("/health", 10000);

  const items: HealthItem[] = [
    { label: "Redis", ok: health?.redis ?? false },
    { label: "PostgreSQL", ok: health?.postgres ?? false },
    { label: "API", ok: health?.status !== undefined },
  ];

  return (
    <div className="health-inner" style={{ display: "flex", alignItems: "center", gap: 20, padding: "4px 0" }}>
      <span style={{ fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.1em" }}>
        System Health
      </span>
      {items.map((item) => (
        <div key={item.label} style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12 }}>
          <span className={`status-dot ${item.ok ? "ok" : "down"}`} />
          <span style={{ color: item.ok ? "var(--text-secondary)" : "var(--short)" }}>
            {item.label}
          </span>
        </div>
      ))}
    </div>
  );
}
