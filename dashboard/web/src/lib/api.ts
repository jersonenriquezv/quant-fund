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
  sandbox: boolean;
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
  pair: string | null;
  direction: string | null;
  setup_type: string | null;
  approved: boolean | null;
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
  tp_price: number;
  tp1_price?: number;
  tp2_price?: number;
  tp3_price?: number;
  filled_size: number;
  leverage: number;
  ai_confidence: number;
  pnl_pct: number;
  breakeven_hit: boolean;
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

export interface OrderBlockData {
  timestamp: number;
  pair: string;
  timeframe: string;
  direction: string;
  high: number;
  low: number;
  body_high: number;
  body_low: number;
  entry_price: number;
  volume_ratio: number;
}

export interface HTFBiasResponse {
  bias: Record<string, string>;
}

export interface WSMessage {
  prices: Record<string, { price: number; open: number; high: number; low: number; timestamp: number }>;
  positions: PositionData[];
}

export interface SentimentData {
  score: number | null;
  label: string | null;
}

export interface HeadlineData {
  title: string;
  source: string;
  timestamp: number;
  category: string;
  url: string;
  sentiment: string | null;
}

export interface HeadlinesData {
  headlines: HeadlineData[];
}

export interface LiqHeatmapBin {
  price: number;
  liq_long_usd: number;
  liq_short_usd: number;
}

export interface LiqHeatmapData {
  pair: string;
  current_price: number;
  bins: LiqHeatmapBin[];
}

export async function postApi<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${getApiBase()}/api${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function patchApi<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${getApiBase()}/api${path}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export async function deleteApi(path: string): Promise<void> {
  const res = await fetch(`${getApiBase()}/api${path}`, {
    method: "DELETE",
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
}

// Manual trading types
export interface ManualTrade {
  id: number;
  pair: string;
  direction: string;
  status: string;
  entry_price: number;
  sl_price: number;
  tp1_price: number | null;
  tp2_price: number | null;
  close_price: number | null;
  pnl_usd: number | null;
  pnl_percent: number | null;
  r_multiple: number | null;
  result: string | null;
  position_size: number;
  position_value_usd: number | null;
  leverage: number;
  risk_usd: number;
  risk_percent: number;
  rr_ratio: number | null;
  sl_distance_pct: number | null;
  thesis: string | null;
  notes: string | null;
  mistakes: string | null;
  setup_type: string | null;
  timeframe: string | null;
  tags: string[] | null;
  created_at: string;
  activated_at: string | null;
  closed_at: string | null;
  partial_closes: ManualPartialClose[];
}

export interface ManualPartialClose {
  id: number;
  close_price: number;
  close_pct: number;
  pnl_usd: number | null;
  notes: string | null;
  closed_at: string;
}

export interface ManualAnalyticsData {
  total_trades: number;
  wins: number;
  losses: number;
  winning: number; // alias
  losing: number;  // alias
  breakeven: number;
  cancelled: number;
  win_rate: number; // Already percentage (e.g. 50.0)
  total_pnl_usd: number;
  avg_r_multiple: number | null;
  avg_rr_planned: number | null;
  profit_factor: number | null;
  current_streak: { count: number; type: string };
  best_trade: { id: number; pair: string; r_multiple: number } | null;
  worst_trade: { id: number; pair: string; r_multiple: number } | null;
  tp1_hit_rate: number | null; // Already percentage
  tp2_hit_rate: number | null; // Already percentage
  breakeven_rate: number | null;
  trades_by_pair: Record<string, { count: number; pnl_usd: number; win_rate: number; avg_r: number }>;
  trades_by_setup: Record<string, { count: number; pnl_usd: number; win_rate: number; avg_r: number }>;
  trades_by_direction: Record<string, { count: number; pnl_usd: number; win_rate: number; avg_r: number }>;
}

export interface ManualBalance {
  pair: string;
  balance: number;
  updated_at: string;
}

export interface CalcResult {
  position_size: number;
  position_value_usd: number;
  margin_required: number;
  risk_usd: number;
  risk_percent: number;
  sl_distance_pct: number;
  rr_ratio: number;
  rr_ratio_tp2: number | null;
  tp_plan: {
    tp1_price: number;
    tp1_rr: number;
    tp2_price: number;
    tp2_rr: number;
  };
  warnings: string[];
}
