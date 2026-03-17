"use client";

import { useState } from "react";
import { OrderBlockPanel } from "./OrderBlockPanel";
import { LiquidationHeatmap } from "./LiquidationHeatmap";
import { WhaleLog } from "./WhaleLog";
import { NewsPanel } from "./NewsPanel";
import type { WSMessage } from "@/lib/api";

const TABS = [
  { id: "obs", label: "Order Blocks", shortLabel: "OBs" },
  { id: "liq", label: "Liquidations", shortLabel: "Liq" },
  { id: "whales", label: "Whales", shortLabel: "Whales" },
  { id: "news", label: "News", shortLabel: "News" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export function MarketIntel({ ws }: { ws: WSMessage | null }) {
  const [active, setActive] = useState<TabId>("obs");

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
        <div className="card-title" style={{ marginBottom: 0 }}>Market Intel</div>
        <div className="liq-tabs">
          {TABS.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActive(tab.id)}
              className={`liq-tab${active === tab.id ? " liq-tab-active" : ""}`}
            >
              <span className="tab-label-full">{tab.label}</span>
              <span className="tab-label-short">{tab.shortLabel}</span>
            </button>
          ))}
        </div>
      </div>

      <div style={{ display: active === "obs" ? "block" : "none" }}>
        <OrderBlockPanel ws={ws} />
      </div>
      <div style={{ display: active === "liq" ? "block" : "none" }}>
        <LiquidationHeatmap />
      </div>
      <div style={{ display: active === "whales" ? "block" : "none" }}>
        <WhaleLog />
      </div>
      <div style={{ display: active === "news" ? "block" : "none" }}>
        <NewsPanel />
      </div>
    </div>
  );
}
