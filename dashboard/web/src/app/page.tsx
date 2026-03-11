"use client";

import { Header } from "@/components/Header";
import { ProfileSelector } from "@/components/ProfileSelector";
import { FearGreedPill } from "@/components/FearGreedPill";
import { PricePanel } from "@/components/PricePanel";
import { RiskGauge } from "@/components/RiskGauge";
import { PositionCard } from "@/components/PositionCard";
import { PnLChart } from "@/components/PnLChart";
import { TradeLog } from "@/components/TradeLog";
import { AILog } from "@/components/AILog";
import { OrderBlockPanel } from "@/components/OrderBlockPanel";
import { WhaleLog } from "@/components/WhaleLog";
import { NewsPanel } from "@/components/NewsPanel";
import { HealthGrid } from "@/components/HealthGrid";
import { useWebSocket } from "@/lib/hooks";

export default function Dashboard() {
  const ws = useWebSocket();

  return (
    <div className="dashboard">
      <Header>
        <FearGreedPill />
        <ProfileSelector />
      </Header>

      <div className="card price-panel">
        <PricePanel pair="BTC/USDT" ws={ws} />
      </div>
      <div className="card price-panel">
        <PricePanel pair="ETH/USDT" ws={ws} />
      </div>
      <div className="card risk-gauge">
        <RiskGauge />
      </div>

      <div className="card positions">
        <PositionCard ws={ws} />
      </div>
      <div className="card equity">
        <PnLChart />
      </div>

      <div className="card trade-log">
        <TradeLog />
      </div>
      <div className="card ai-log">
        <AILog />
      </div>

      <div className="card ob-panel">
        <OrderBlockPanel ws={ws} />
      </div>

      <div className="card news-panel">
        <NewsPanel />
      </div>

      <div className="card whale-log">
        <WhaleLog />
      </div>

      <div className="card health-bar">
        <HealthGrid />
      </div>
    </div>
  );
}
