function getApiBase(): string {
  if (typeof window !== "undefined") {
    return `http://${window.location.hostname}:8000`;
  }
  return process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
}

export async function fetchApi<T>(path: string): Promise<T> {
  const res = await fetch(`${getApiBase()}/api${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export function wsUrl(): string {
  const base = getApiBase().replace(/^http/, "ws");
  return `${base}/api/ws`;
}

// Types matching the API models
export interface HealthData {
  status: string;
  postgres: boolean;
  redis: boolean;
}

export interface MarketData {
  pair: string;
  price: number | null;
  change_pct: number | null;
  funding_rate: number | null;
  next_funding_rate: number | null;
  next_funding_time: number | null;
  oi_usd: number | null;
  oi_base: number | null;
}

export interface TradeRecord {
  id: number;
  pair: string | null;
  direction: string | null;
  setup_type: string | null;
  entry_price: number | null;
  sl_price: number | null;
  tp1_price: number | null;
  tp2_price: number | null;
  tp3_price: number | null;
  actual_entry: number | null;
  actual_exit: number | null;
  exit_reason: string | null;
  position_size: number | null;
  pnl_usd: number | null;
  pnl_pct: number | null;
  ai_confidence: number | null;
  opened_at: string | null;
  closed_at: string | null;
  status: string | null;
}

export interface AIDecision {
  id: number;
  trade_id: number | null;
  confidence: number | null;
  reasoning: string | null;
  adjustments: Record<string, unknown> | null;
  warnings: string[] | null;
  created_at: string | null;
}

export interface RiskState {
  daily_dd_pct: number | null;
  weekly_dd_pct: number | null;
  open_positions: number;
  max_positions: number;
  cooldown_until: number | null;
  recent_events: RiskEvent[];
}

export interface RiskEvent {
  id: number;
  event_type: string | null;
  details: Record<string, unknown> | null;
  created_at: string | null;
}

export interface StatsData {
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  win_rate: number;
  total_pnl_usd: number;
  avg_pnl_pct: number;
  best_trade_pct: number;
  worst_trade_pct: number;
  profit_factor: number;
  avg_rr: number;
}

export interface PositionData {
  pair: string;
  direction: string;
  setup_type: string;
  phase: string;
  entry_price: number;
  actual_entry_price: number | null;
  sl_price: number;
  tp1_price: number;
  tp2_price: number;
  tp3_price: number;
  filled_size: number;
  leverage: number;
  ai_confidence: number;
  pnl_pct: number;
  created_at: number;
  filled_at: number | null;
}

export interface WhaleMovement {
  timestamp: number;
  wallet: string;
  label: string;
  action: string;
  amount: number;
  exchange: string;
  significance: string;
  chain: string;
}

export interface WSMessage {
  prices: Record<string, { price: number; open: number; high: number; low: number; timestamp: number }>;
  positions: PositionData[];
}
