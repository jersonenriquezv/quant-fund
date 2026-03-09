# Liquidation Heatmap — Implementation Plan

## What
Add an estimated liquidation level chart to the dashboard, similar to Coinglass's heatmap but using DIY estimation from existing OI + candle data. MVP is a vertical bar chart (Y=price, X=estimated liquidation USD) that refreshes every 30s.

## Why
Visualize where liquidation clusters sit relative to current price. Helps anticipate price magnets and validate SMC setups (sweeps target these clusters). Currently the dashboard is pure text — this adds the first real chart with trading insight.

## Approach: DIY Estimation (Option B)

Coinglass has proprietary leverage distribution data we don't have. Instead, we assume industry-average leverage distribution and project liquidation prices from OI + recent candle closes.

**Algorithm:**
1. Take last 200 candles (5m) — covers ~17 hours
2. Define leverage tiers: `[5x, 10x, 25x, 50x, 100x]` with weights `[0.30, 0.30, 0.20, 0.15, 0.05]`
3. For each candle close price, for each leverage tier:
   - `liq_long = close * (1 - 1/leverage * (1 - maintenance_margin))`
   - `liq_short = close * (1 + 1/leverage * (1 - maintenance_margin))`
   - Allocate `oi_usd * weight / num_candles` to each liquidation price
4. Bucket into price bins ($50 for BTC, $2 for ETH)
5. Sum estimated USD per bin

**MVP (v1):** Vertical bar chart — snapshot in time, no time axis
**Future (v2):** Store snapshots every 5min in Redis/PG, render X=time heatmap

## Limitations vs Coinglass

| Aspect | Coinglass | This |
|--------|-----------|------|
| Leverage distribution | Real exchange data | Assumed (industry averages) |
| Multi-exchange | Binance + OKX + Bybit | OKX only |
| Entry price model | Order book depth | Candle close as proxy |
| Granularity | 1-min, $1 bins | 5-min, $50 BTC / $2 ETH bins |
| Time dimension | Full time-series heatmap | v1: snapshot only |
| Cost | $29+/month | Free |

Label as "Estimated Liquidation Levels" — be honest about the approximation.

## Steps

### 1. Backend — Liquidation estimator
**File:** `data_service/liquidation_estimator.py` (NEW)
- Function `estimate_liquidation_levels(candles, oi_usd, pair) -> list[dict]`
- Each dict: `{price: float, liq_long_usd: float, liq_short_usd: float}`
- Leverage tiers and weights as constants at top of file
- BTC bin size: $50, ETH bin size: $2
- Done when: function returns correct bins given test candles + OI

### 2. Backend — API endpoint
**File:** `dashboard/api/routes/liquidation.py` (NEW)
**File:** `dashboard/api/main.py` (MODIFY — register route)
- `GET /api/liquidation/heatmap/{pair}`
- Reads candles from PostgreSQL via `queries.get_candles(pair, "5m", 200)`
- Reads OI from Redis key `qf:oi:{pair}`
- Returns: `{pair, current_price, bins[], bsl_levels[], ssl_levels[]}`
- Follow pattern from `dashboard/api/routes/market.py`
- Done when: endpoint returns valid JSON with bins

### 3. Backend — Response model
**File:** `dashboard/api/models.py` (MODIFY)
```python
class LiqHeatmapBin(BaseModel):
    price: float
    liq_long_usd: float
    liq_short_usd: float

class LiqHeatmapResponse(BaseModel):
    pair: str
    current_price: float
    bins: list[LiqHeatmapBin]
    bsl_levels: list[float]
    ssl_levels: list[float]
```
- Done when: model validates correctly

### 4. Frontend — API type + fetch
**File:** `dashboard/web/src/lib/api.ts` (MODIFY)
- Add `LiqHeatmapData` interface matching backend response
- Add `fetchLiquidationHeatmap(pair)` function
- Done when: TypeScript compiles, function returns typed data

### 5. Frontend — LiquidationHeatmap component
**File:** `dashboard/web/src/components/LiquidationHeatmap.tsx` (NEW)
- `<canvas>` element via React refs (no new dependencies)
- Y-axis: price bins
- X-axis: estimated USD (longs left/red, shorts right/green)
- Horizontal line for current price
- Optional: BSL/SSL level markers
- Pair selector tabs (BTC / ETH)
- `usePolling` hook for 30s refresh (same pattern as other components)
- `devicePixelRatio` scaling for retina displays
- Done when: chart renders with mock data, updates on polling

### 6. Frontend — Dashboard integration
**File:** `dashboard/web/src/app/page.tsx` (MODIFY)
- Add `<LiquidationHeatmap />` between OrderBlockPanel and WhaleLog
**File:** `dashboard/web/src/app/globals.css` (MODIFY)
- `.liq-heatmap { grid-column: 1 / -1; }` (full width)
- Mobile (<=639px): canvas scales to container, height 200px, font-size 10px
- Desktop: height 300px
- Done when: component renders in dashboard, mobile responsive at 375px

## Files Changed

| File | Action |
|------|--------|
| `data_service/liquidation_estimator.py` | NEW |
| `dashboard/api/routes/liquidation.py` | NEW |
| `dashboard/api/models.py` | MODIFY |
| `dashboard/api/main.py` | MODIFY |
| `dashboard/web/src/lib/api.ts` | MODIFY |
| `dashboard/web/src/components/LiquidationHeatmap.tsx` | NEW |
| `dashboard/web/src/app/page.tsx` | MODIFY |
| `dashboard/web/src/app/globals.css` | MODIFY |

## Risks

1. **Inaccurate estimates** — Leverage distribution is guessed. Mitigate: label as "Estimated", use conservative weights from published research
2. **OI unavailable** — If Redis has no OI data, return empty response with message, don't crash
3. **Canvas on mobile** — Must test at 375px. Use `width: 100%` CSS + `devicePixelRatio` for sharp rendering
4. **Performance** — 200 candles x 5 tiers = 1000 calculations, negligible. Canvas rendering ~200 bins also lightweight

## Out of Scope
- Time-series heatmap (X=time) — requires storing snapshots, future v2
- Coinglass API integration ($29/mo) — not worth it at $108 capital
- Multi-exchange aggregation — OKX only
- Real leverage distribution data — not available via public API
