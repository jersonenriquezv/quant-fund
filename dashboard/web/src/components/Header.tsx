"use client";

import { useEffect, useState } from "react";
import { usePolling } from "@/lib/hooks";
import type { HealthData } from "@/lib/api";

export function Header({ children }: { children?: React.ReactNode }) {
  const { data: health } = usePolling<HealthData>("/health", 10000);
  const [clock, setClock] = useState("");

  useEffect(() => {
    const tick = () => {
      setClock(new Date().toISOString().replace("T", " ").slice(11, 19) + " UTC");
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);

  const isOk = health?.status === "ok";
  const isLive = health ? !health.sandbox : false;

  // Determine active page from pathname
  const isManual = typeof window !== "undefined" && window.location.pathname.startsWith("/manual");

  return (
    <div className="header card header-inner" style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "10px 20px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span className={`status-dot ${isOk ? "ok" : "down"}`} />
        <span style={{ fontWeight: 700, fontSize: 14, letterSpacing: "0.05em" }}>
          QF
        </span>
        <span className={`mode-badge ${isLive ? "mode-badge-live" : "mode-badge-demo"}`}>
          {isLive ? "LIVE" : "DEMO"}
        </span>
        <nav className="header-nav">
          <a href="/" className={`header-nav-link ${!isManual ? "header-nav-active" : ""}`}>Bot</a>
          <a href="/manual" className={`header-nav-link ${isManual ? "header-nav-active" : ""}`}>Manual</a>
        </nav>
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        {children}
      </div>

      <div style={{ color: "var(--text-muted)", fontSize: 11, fontVariantNumeric: "tabular-nums" }}>
        {clock}
      </div>
    </div>
  );
}
