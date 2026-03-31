"use client";

import { Header } from "@/components/Header";
import { ManualStats } from "@/components/manual/ManualStats";
import { ActiveTrades } from "@/components/manual/ActiveTrades";
import { TradeHistory } from "@/components/manual/TradeHistory";
import { ManualAnalytics } from "@/components/manual/ManualAnalytics";
import { QuickCalculator } from "@/components/manual/QuickCalculator";

export default function ManualDashboard() {
  return (
    <div className="manual-dashboard">
      <Header />

      <div className="card manual-area-stats">
        <ManualStats />
      </div>

      <div className="card manual-area-calc">
        <QuickCalculator />
      </div>

      <div className="card manual-area-active">
        <ActiveTrades />
      </div>

      <div className="card manual-area-history">
        <TradeHistory />
      </div>

      <div className="card manual-area-analytics">
        <ManualAnalytics />
      </div>
    </div>
  );
}
