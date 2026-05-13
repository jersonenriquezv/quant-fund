# Optimal trading hours — BTC/ETH mean reversion
**Date:** 2026-05-13
**Data:** 90 days of 1H candles from OKX SWAP (BTC/USDT + ETH/USDT)
**Method:** mr_score = wick_ratio × range_pct × pct_range_bars × (1 + volume_zscore)
**Why this metric:** mean reversion fades wicks at structure. Best hours have (a) wide ranges (room for trade), (b) high wick ratios (rejections happen), (c) range-bound bars (not trending), (d) decent volume.

## Top 5 hours per pair (UTC)

| Rank | BTC | ETH | Common |
|------|-----|-----|--------|
| 1 | **14:00** UTC | **14:00** UTC | ✅ |
| 2 | **15:00** UTC | **15:00** UTC | ✅ |
| 3 | 16:00 UTC | 02:00 UTC | — |
| 4 | 08:00 UTC | **13:00** UTC | ✅ |
| 5 | **13:00** UTC | 09:00 UTC | — |

**Hours that win on BOTH pairs:** 13, 14, 15 UTC. Strong consensus.

## Worst 5 hours per pair (UTC) — avoid

| Rank | BTC | ETH |
|------|-----|-----|
| Worst | 22:00 | 18:00 |
|  | 06:00 | 20:00 |
|  | 07:00 | 21:00 |
|  | 00:00 | 22:00 |
|  | 04:00 | 23:00 |

**Hours that lose on BOTH:** 22:00 UTC consistently terrible. ETH dead 18-23 UTC (post-US-close drift).

## Translation to Eastern Time (your timezone, EDT in May)

UTC -4h:

| UTC | EDT (your time) | What's happening | Verdict |
|---|---|---|---|
| 13:00 | **09:00 AM** | London still active + NY pre-market | ✅ TOP |
| 14:00 | **10:00 AM** | NY equity open + London overlap | ✅ TOP |
| 15:00 | **11:00 AM** | NY morning fully open | ✅ TOP |
| 16:00 | **12:00 PM** | NY lunch start, BTC still active | ⚠️ BTC ok, ETH softer |
| 08:00 | 04:00 AM | London open | ⚠️ Strong but you won't be awake |
| 02:00 | 10:00 PM previous day | Asian session ETH | ⚠️ ETH only, late |
| 18-23:00 | 02:00-07:00 PM | US afternoon → close drift | ❌ AVOID, especially ETH |

## Practical operating window

**Primary:** **9:00 AM — 12:00 PM EDT** (UTC 13-16). Your sweet spot for both pairs.
**Secondary (ETH only):** late night around 10 PM EDT if you're awake.
**Avoid:** afternoon/evening EDT (2-7 PM). ETH is dead, BTC mediocre.

## Statistical caveats

- Sample = 90 days × 1 candle/hour = 90 observations per hour bucket. Large enough for hourly aggregates, not large enough for "Tuesday at 14:00 vs Friday at 14:00" splits.
- `mr_score` is a heuristic, not a backtest. Composite favors range-bound action. A trader trying to FOLLOW trends would invert this list.
- Hours 13-16 also have the most NEWS releases (Fed minutes, CPI, NFP). Those events break mean reversion — mr_score is averaged over event and non-event days. Recommend layering: skip operating ±30 min around scheduled major releases.

## How this fits taxonomy v3

**Option A — Add as Rule 15 (binding):**
> Rule 15 — Operate primarily in 9:00 AM — 12:00 PM EDT (UTC 13-16). Outside that window, only trade if confluence is unusually strong (4 of 4 confluences instead of 3).

**Option B — Informational guideline (recommended):**
- Don't add as binding rule yet (Rule 13 says no rule changes until N=30)
- Treat as "default trading window." Outside it, pause and verify confluences harder.
- After N=30, if data shows hour-of-day correlation with PnL, formalize as Rule 15.

**Option C — Reject:**
- 90 days is short. Crypto regime can shift. Hours that work in a Q2 chop may fail in a Q3 trend.
- Stick with rules 1-14 only. Skip hour filtering entirely.

## Recommendation

**Option B.** Use the window as guideline starting now. Don't elevate to binding rule until N=30 personal trades show your own PnL correlates with hour-of-day. The data above describes the MARKET; your edge needs to be verified on YOUR execution within those hours.

Bias toward 9-12 EDT for the next 30 trades. If you trade outside that window, write in journal `outside_optimal_hours: true` so we can analyze the impact later.
