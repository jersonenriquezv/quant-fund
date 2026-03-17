"use client";

import { Header } from "@/components/Header";
import { FearGreedPill } from "@/components/FearGreedPill";
import { HeroStats } from "@/components/HeroStats";
import { MarketOverview } from "@/components/MarketOverview";
import { RiskGauge } from "@/components/RiskGauge";
import { PositionCard } from "@/components/PositionCard";
import { PnLChart } from "@/components/PnLChart";
import { TradeLog } from "@/components/TradeLog";
import { RecentTrades } from "@/components/RecentTrades";
import { MarketIntel } from "@/components/MarketIntel";
import { HealthGrid } from "@/components/HealthGrid";
import { useWebSocket } from "@/lib/hooks";

export default function Dashboard() {
  const ws = useWebSocket();

  return (
    <div className="dashboard">
      <Header>
        <FearGreedPill />
      </Header>

      <div className="card area-hero">
        <HeroStats />
      </div>

      <div className="card area-market">
        <MarketOverview ws={ws} />
      </div>

      <div className="card area-positions">
        <PositionCard ws={ws} />
      </div>

      <div className="card area-equity">
        <PnLChart />
      </div>
      <div className="card area-risk">
        <RiskGauge />
      </div>

      <div className="card area-recent">
        <RecentTrades />
      </div>

      <div className="card area-trades">
        <TradeLog />
      </div>

      <div className="card area-intel">
        <MarketIntel ws={ws} />
      </div>

      <div className="card area-health">
        <HealthGrid />
      </div>
    </div>
  );
}
