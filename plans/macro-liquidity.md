# Macro Liquidity Context — Implementation Plan

## What
Add global liquidity indicators (M2, Fed balance sheet, reverse repo, DXY, Treasury yields) as a macro regime layer. Provides Claude with big-picture context: is liquidity expanding or contracting? Not a pre-filter gate — data updates too slowly (weekly/daily) for intraday gating. Claude weighs it alongside funding, CVD, whales.

## Why
Crypto correlates ~0.85 with global M2 on 3-6 month windows. When central banks expand balance sheets, risk assets rally. When they contract, crypto bleeds. The bot currently has micro context (price action, funding, whales) but zero macro awareness. A regime score tells Claude "the tide is rising" or "the tide is going out" — useful for confidence calibration.

## Current State
- **Fear & Greed + news headlines**: LIVE (`data_service/news_client.py`)
- **Macro data**: Zero. No FRED client, no M2, no DXY.
- **Planner philosophy**: "Global liquidity (M2, Fed) drives everything. Crypto reacts first" (documented, not implemented)

## Data Sources

### FRED API (Federal Reserve Economic Data)
- **Base URL:** `https://api.stlouisfed.org/fred/series/observations`
- **Auth:** Free API key (register at fred.stlouisfed.org)
- **Rate limit:** 120 requests/minute
- **Format:** JSON
- **Series we need:**

| Series ID | Name | Frequency | Use |
|-----------|------|-----------|-----|
| `WM2NS` | M2 Money Supply | Weekly (Thursday) | Liquidity expansion/contraction |
| `WALCL` | Fed Total Assets (Balance Sheet) | Weekly (Wednesday) | QE/QT proxy |
| `RRPONTSYD` | Reverse Repo (ON RRP) | Daily | Net liquidity = Fed BS - RRP |
| `DGS2` | 2-Year Treasury Yield | Daily | Rate expectations |
| `DGS10` | 10-Year Treasury Yield | Daily | Long-term rate sentiment |
| `T10Y2Y` | 10Y-2Y Spread (Yield Curve) | Daily | Recession signal |
| `DTWEXBGS` | Trade-Weighted Dollar Index | Daily | USD strength (inverse crypto) |

### Update Schedule
- Weekly series (M2, Fed BS): Poll once daily, cache 24h
- Daily series (RRP, yields, DXY): Poll every 6h, cache 6h
- All data has 1-2 day publication lag — acceptable for regime context

## Architecture

```
FRED API (7 series)
       ↓
MacroClient (fetch + cache in Redis)
       ↓
MacroSnapshot (frozen dataclass)
       ↓
┌──────┴──────┐
│             │
Claude        Dashboard
(regime       (panel with
 context)      components)
```

**NOT a pre-filter gate.** Data is too slow (daily/weekly) to gate 5m/15m trades. Claude uses it for confidence calibration only.

## Steps

### 1. FRED API key setup
**File:** `config/.env` (MODIFY)
```
FRED_API_KEY=your_key_here
```
- Register at https://fred.stlouisfed.org/docs/api/api_key.html (free, instant)
- Done when: key in .env

### 2. Data models
**File:** `shared/models.py` (MODIFY)
```python
@dataclass(frozen=True)
class MacroIndicator:
    series_id: str          # "WM2NS", "WALCL", etc.
    name: str               # Human-readable name
    value: float            # Latest value
    previous_value: float   # Previous observation (for trend)
    change_pct: float       # % change from previous
    date: str               # Observation date "YYYY-MM-DD"
    frequency: str          # "weekly" or "daily"

@dataclass(frozen=True)
class MacroSnapshot:
    regime_score: int               # -100 to +100 (composite)
    regime_label: str               # "Risk-On", "Neutral", "Risk-Off"
    m2: Optional[MacroIndicator] = None
    fed_balance_sheet: Optional[MacroIndicator] = None
    reverse_repo: Optional[MacroIndicator] = None
    net_liquidity: Optional[float] = None      # Fed BS - RRP (trillions USD)
    net_liquidity_change: Optional[float] = None  # vs previous
    dxy: Optional[MacroIndicator] = None
    yield_2y: Optional[MacroIndicator] = None
    yield_10y: Optional[MacroIndicator] = None
    yield_curve: Optional[MacroIndicator] = None  # 10Y-2Y spread
    fetched_at: int = 0             # Unix ms
```

Add to `MarketSnapshot`:
```python
macro: Optional[MacroSnapshot] = None
```
- Done when: models defined, MarketSnapshot includes field

### 3. Settings
**File:** `config/settings.py` (MODIFY)
```python
# Macro Liquidity
MACRO_ENABLED: bool = True
FRED_API_KEY: str = field(default_factory=lambda: os.getenv("FRED_API_KEY", ""))
FRED_BASE_URL: str = "https://api.stlouisfed.org/fred/series/observations"
MACRO_POLL_INTERVAL: int = 21600       # 6 hours
MACRO_CACHE_TTL_DAILY: int = 21600     # 6 hours for daily series
MACRO_CACHE_TTL_WEEKLY: int = 86400    # 24 hours for weekly series
```
- Done when: configurable via settings

### 4. Macro client module
**File:** `data_service/macro_client.py` (NEW)

```python
class MacroClient:
    """Fetches global liquidity indicators from FRED API."""

    SERIES = {
        "WM2NS":     {"name": "M2 Money Supply",     "freq": "weekly"},
        "WALCL":     {"name": "Fed Balance Sheet",    "freq": "weekly"},
        "RRPONTSYD": {"name": "Reverse Repo (RRP)",   "freq": "daily"},
        "DGS2":      {"name": "2Y Treasury Yield",    "freq": "daily"},
        "DGS10":     {"name": "10Y Treasury Yield",   "freq": "daily"},
        "T10Y2Y":    {"name": "Yield Curve (10Y-2Y)", "freq": "daily"},
        "DTWEXBGS":  {"name": "Dollar Index (TWD)",   "freq": "daily"},
    }
```

Methods:
- `async fetch_series(series_id) -> MacroIndicator | None` — Fetch last 2 observations from FRED, compute change_pct
- `async fetch_all() -> MacroSnapshot` — Fetch all 7 series, compute regime score
- `_compute_regime_score(indicators) -> tuple[int, str]` — Weighted composite

**Regime score logic:**
```
Score components (each -20 to +20, total -100 to +100):
  M2 trend:        growing = +20, flat = 0, shrinking = -20
  Fed BS trend:    growing = +20 (QE), shrinking = -20 (QT)
  Net liquidity:   (Fed BS - RRP) rising = +20, falling = -20
  DXY trend:       falling = +20 (weak USD = bullish crypto), rising = -20
  Yield curve:     steepening = +10, inverting further = -10

Labels:
  > +40:  "Risk-On"
  +10 to +40: "Mildly Bullish"
  -10 to +10: "Neutral"
  -40 to -10: "Mildly Bearish"
  < -40: "Risk-Off"
```

**Redis caching:**
- Key: `qf:macro:{series_id}` — JSON of MacroIndicator
- Key: `qf:macro:snapshot` — JSON of full MacroSnapshot
- TTL based on frequency (6h daily, 24h weekly)
- On startup, try Redis first before hitting FRED

**Error handling:**
- FRED down → return cached data from Redis (stale but usable)
- No API key → log warning, return None, bot continues
- Individual series fails → skip it, compute regime from available data

- Done when: fetches all 7 series, computes regime score, caches in Redis

### 5. DataService integration
**File:** `data_service/service.py` (MODIFY)
- Initialize `MacroClient` in `__init__`
- Add `_macro_poll_loop()` task (every `MACRO_POLL_INTERVAL`)
- Store latest `MacroSnapshot` in instance variable
- Include in `get_market_snapshot()` output
- Add to `start()` task list
- Done when: `MarketSnapshot` includes live macro data

### 6. Claude context
**File:** `ai_service/prompt_builder.py` (MODIFY)
- Add `_build_macro_section(snapshot)` method
- Section format:
```
## MACRO REGIME
Score: +35/100 (Mildly Bullish)

Global Liquidity:
- M2 Money Supply: $21.8T (+0.3% WoW) — EXPANDING
- Fed Balance Sheet: $6.9T (-0.1% WoW) — slight QT
- Net Liquidity (Fed BS - RRP): $6.5T (+1.2% WoW) — EXPANDING
- Reverse Repo: $0.4T (-8.5% WoW) — draining into markets (bullish)

Dollar & Rates:
- Dollar Index: 103.2 (-0.5% WoW) — weakening (bullish crypto)
- 2Y Yield: 4.15% (-5bp) | 10Y Yield: 4.32% (-3bp)
- Yield Curve (10Y-2Y): +17bp — normal (no recession signal)

Interpretation: Net liquidity expanding as RRP drains. Weak dollar supports risk assets.
```
- Add to `build_evaluation_prompt()` sections list
- Update system prompt: add macro regime as evaluation factor
- Only include if `snapshot.macro` exists
- Done when: Claude receives macro context in evaluation prompt

### 7. Dashboard API
**File:** `dashboard/api/routes/macro.py` (NEW)
```
GET /api/macro → MacroSnapshot JSON
```
- Read from Redis (`qf:macro:snapshot`)
- Fallback: return `{"regime_score": null}` if no data
- Done when: endpoint returns macro data

### 8. Dashboard component
**File:** `dashboard/web/src/components/MacroPanel.tsx` (NEW)
- Regime score bar (-100 to +100) with color gradient (red → yellow → green)
- Label: "Risk-Off / Mildly Bearish / Neutral / Mildly Bullish / Risk-On"
- Individual indicators as compact rows:
  - M2: value + trend arrow (↑↓→)
  - Net Liquidity: value + trend arrow
  - DXY: value + trend arrow
  - Yield Curve: value + status
- Mobile: collapse to regime score pill only (expand on tap)
- Done when: macro panel visible on dashboard

### 9. Tests
**File:** `tests/test_macro_client.py` (NEW)
- Mock FRED API responses for all 7 series
- Test MacroIndicator parsing (valid, missing data, stale)
- Test regime score computation (all bullish, all bearish, mixed)
- Test Redis caching (hit, miss, expired)
- Test graceful degradation (partial data, no API key)
- Done when: tests pass

## Files Changed

| File | Action |
|------|--------|
| `config/.env` | MODIFY (add FRED_API_KEY) |
| `shared/models.py` | MODIFY (add MacroIndicator, MacroSnapshot, update MarketSnapshot) |
| `config/settings.py` | MODIFY (add macro settings) |
| `data_service/macro_client.py` | NEW |
| `data_service/service.py` | MODIFY (init client, polling loop, snapshot) |
| `ai_service/prompt_builder.py` | MODIFY (add macro section to Claude prompt) |
| `dashboard/api/routes/macro.py` | NEW |
| `dashboard/web/src/components/MacroPanel.tsx` | NEW |
| `tests/test_macro_client.py` | NEW |
| `requirements.txt` | No change (requests already included) |

## Risks

1. **FRED API down** — Rare (99.9% uptime), but mitigate: Redis cache + graceful None.
2. **Data publication lag** — M2 has 1-2 week lag. Mitigate: regime changes slowly, lag is acceptable for macro context.
3. **Regime score oversimplification** — Compressing 7 indicators into one number loses nuance. Mitigate: Claude receives individual components too, not just the score.
4. **Over-reliance on macro** — Claude might reject valid setups because "macro is bearish". Mitigate: macro is context only, never a hard gate. Claude still sees the full technical setup.
5. **FRED API key revocation** — Free keys have no expiry, but terms of use require attribution. Mitigate: keep key in .env, easy to replace.

## Out of Scope
- Real-time macro data (Bloomberg Terminal, Refinitiv) — cost prohibitive
- GDP, CPI, unemployment — too slow (monthly/quarterly) and already priced in
- Fed meeting minutes / FOMC analysis — requires NLP, out of scope
- Macro as pre-filter gate — data too slow for intraday gating
- Macro as trade originator — context only, never generates setups
- Non-US central banks (ECB, BOJ) — USD dominates crypto correlation
