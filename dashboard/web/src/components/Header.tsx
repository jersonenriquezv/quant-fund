"use client";

import { useEffect, useState } from "react";
import { usePolling } from "@/lib/hooks";
import type { HealthData } from "@/lib/api";

export function Header({ children }: { children?: React.ReactNode }) {
  const { data: health } = usePolling<HealthData>("/health", 10000);
  const [clock, setClock] = useState("");

  useEffect(() => {
    const tick = () => {
      setClock(new Date().toISOString().replace("T", " ").slice(0, 19) + " UTC");
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);

  const isOk = health?.status === "ok";

  return (
    <div className="header card header-inner" style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "10px 20px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <span className={`status-dot ${isOk ? "ok" : "down"}`} />
        <span style={{ fontWeight: 700, fontSize: 15, letterSpacing: "0.05em" }}>
          QUANT FUND
        </span>
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <div className="demo-banner" style={{ padding: "3px 16px", borderRadius: 3, fontSize: 11 }}>
          DEMO MODE
        </div>
        {children}
      </div>

      <div style={{ color: "var(--text-muted)", fontSize: 13, fontVariantNumeric: "tabular-nums" }}>
        {clock}
      </div>
    </div>
  );
}
