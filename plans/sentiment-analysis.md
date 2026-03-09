# News Sentiment Analysis — Implementation Plan

## What
Add crypto news sentiment as a new data layer. Use it as a pre-filter gate (reject longs during extreme fear) and as context for Claude's evaluation. No new AI model — just external API feeds + rules.

## Why
The bot currently has zero awareness of news. A major hack, exchange collapse, regulatory crackdown, or macro event (Fed rate surprise) can invalidate any technical setup instantly. Sentiment acts as a "macro circuit breaker" — if the entire market is in panic mode, don't open longs regardless of how clean the OB looks.

## Current State (Verified 2026-03-09)
- **Pre-filter** exists in `main.py:267-303` — checks funding extremes + CVD divergence before Claude
- **Claude receives** setup + funding + OI + CVD + liquidations + whale movements (no news)
- **MarketSnapshot** in `shared/models.py` has no sentiment field
- No news or sentiment data is fetched anywhere in the codebase
- `aiohttp` 3.13.3 is installed in venv (not in requirements.txt — add it)

## API Validation (2026-03-09)

### alternative.me Fear & Greed Index ← PRIMARY (pre-filter score)
- **Status: LIVE, TESTED, WORKING**
- **Endpoint:** `GET https://api.alternative.me/fng/?limit=1`
- **Response:** `{"data": [{"value": "8", "value_classification": "Extreme Fear", "timestamp": "..."}]}`
- **Scale:** 0 (Extreme Fear) to 100 (Extreme Greed)
- **Cost:** Free, no API key, reliable infrastructure (running since 2018)
- **Use:** Numeric score for pre-filter gate + Claude context

### cryptocurrency.cv (free-crypto-news) ← SECONDARY (headlines for Claude)
- **Status: PARTIALLY WORKING**
- **Working:** `GET https://cryptocurrency.cv/api/news?asset=BTC&limit=5` → 200 (requires `User-Agent` header)
- **NOT working:** `/api/ai/sentiment` → 404, `/api/market/fear-greed` → 404, `/api/breaking` → 404
- **Bare requests (no User-Agent):** 403 (Cloudflare)
- **Response structure:** `{"articles": [{"title", "source", "description", "pubDate", "category"}]}`
- **NOTE:** Articles do NOT have individual sentiment scores
- **Use:** Headlines only — fed to Claude as context

### WorldMonitor.app — NOT suitable (too geopolitical)
### CryptoPanic API — Backup option (not implemented in v1)

## Architecture Decision

```
                          Pipeline Flow

Market Data → Strategy → [PRE-FILTER] → Claude AI → Risk → Execution
                              ↑
                     ┌────────┴────────┐
                     │  Existing:      │
                     │  - Funding      │
                     │  - CVD          │
                     │                 │
                     │  NEW:           │
                     │  - Fear & Greed │
                     │  - News         │
                     └─────────────────┘
```

**Two data sources, two uses:**
1. **Fear & Greed** (alternative.me) → numeric score for pre-filter gate
   - F&G < 15 (Extreme Fear) → reject longs
   - F&G > 85 (Extreme Greed) → reject shorts
   - Thresholds configurable in settings
2. **Headlines** (cryptocurrency.cv) → context for Claude prompt
   - Top 3-5 headlines included in the prompt
   - Claude weighs them alongside funding, CVD, whales
   - Not a hard gate — Claude decides

## Steps

### 1. Data models
**File:** `shared/models.py` (MODIFY)
```python
@dataclass(frozen=True)
class NewsHeadline:
    title: str
    source: str
    timestamp: int       # Unix ms
    category: str        # "bitcoin", "defi", "macro", etc.

@dataclass(frozen=True)
class NewsSentiment:
    score: int                  # 0-100 (Fear & Greed Index)
    label: str                  # "Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed"
    headlines: list             # Recent headlines (for Claude)
    fetched_at: int             # Unix ms
```

Add to `MarketSnapshot`:
```python
news_sentiment: Optional[NewsSentiment] = None
```
- Done when: models defined, MarketSnapshot includes field

### 2. News client module
**File:** `data_service/news_client.py` (NEW)
- Class `NewsClient` with async methods:
  - `fetch_fear_greed() -> tuple[int, str] | None` — calls alternative.me
  - `fetch_headlines(asset: str, limit: int = 5) -> list[NewsHeadline]` — calls cryptocurrency.cv
  - `fetch_sentiment() -> NewsSentiment | None` — combines both into one model
- HTTP via `aiohttp` (installed, add to requirements.txt)
- 15s timeout per request, graceful failure (return None on error)
- Cache Fear & Greed in Redis for 30min (updates daily anyway)
- Cache headlines in Redis for 5min
- Redis keys: `qf:news:fear_greed`, `qf:news:headlines:{asset}`
- Must send `User-Agent` header for cryptocurrency.cv (Cloudflare)
- Done when: returns Fear & Greed score + headlines for BTC and ETH

### 3. Settings
**File:** `config/settings.py` (MODIFY)
```python
# News Sentiment
NEWS_SENTIMENT_ENABLED: bool = True
NEWS_FEAR_GREED_URL: str = "https://api.alternative.me/fng/"
NEWS_HEADLINES_URL: str = "https://cryptocurrency.cv/api/news"
NEWS_POLL_INTERVAL: int = 300                    # 5 minutes
NEWS_FEAR_GREED_CACHE_TTL: int = 1800            # 30 minutes
NEWS_HEADLINES_CACHE_TTL: int = 300              # 5 minutes
NEWS_EXTREME_FEAR_THRESHOLD: int = 15            # F&G < 15 → reject longs
NEWS_EXTREME_GREED_THRESHOLD: int = 85           # F&G > 85 → reject shorts
```
- Done when: configurable via settings

### 4. DataService integration
**File:** `data_service/service.py` (MODIFY)
- Initialize `NewsClient` in `DataService.__init__`
- Add `_news_sentiment_loop()` polling every `NEWS_POLL_INTERVAL`
- Store latest `NewsSentiment` in instance variable
- Include in `get_market_snapshot()` output
- Add to `start()` task list
- Done when: `MarketSnapshot` includes live sentiment data

### 5. Pre-filter gate
**File:** `main.py` (MODIFY — `_pre_filter_for_claude`)
```python
# Check 3: Fear & Greed extreme against direction
if snapshot.news_sentiment:
    fg = snapshot.news_sentiment.score
    if setup.direction == "long" and fg < settings.NEWS_EXTREME_FEAR_THRESHOLD:
        return f"Extreme Fear (F&G={fg}) — rejecting long"
    if setup.direction == "short" and fg > settings.NEWS_EXTREME_GREED_THRESHOLD:
        return f"Extreme Greed (F&G={fg}) — rejecting short"
```
- Done when: pre-filter rejects longs during extreme fear

### 6. Claude context
**File:** `ai_service/prompt_builder.py` (MODIFY)
- Add `_build_news_section(snapshot)` method
- Section format:
```
## NEWS SENTIMENT
Fear & Greed Index: 8/100 (Extreme Fear)
Recent headlines:
- "Bitcoin ETF sees $200M outflows" (CoinDesk, bitcoin)
- "Fed signals rate hold through Q2" (CNBC Crypto, macro)
- "Ethereum Pectra upgrade on track" (Decrypt, ethereum)
```
- Add to `build_evaluation_prompt()` sections list
- Only include if `snapshot.news_sentiment` exists
- Update system prompt to include NEWS SENTIMENT as factor 8
- Done when: Claude receives F&G + headlines in evaluation prompt

### 7. Tests
**File:** `tests/test_news_client.py` (NEW)
- Mock aiohttp responses for both APIs
- Test Fear & Greed parsing (valid, error, timeout)
- Test headlines parsing (valid, 403, empty)
- Test `NewsSentiment` assembly
- Test pre-filter integration with sentiment data
- Done when: tests pass

### 8. Dashboard display (OPTIONAL — can be separate PR)
**File:** `dashboard/web/src/components/` — add F&G indicator
- Simple pill: "F&G: 8 (Extreme Fear)" with color coding
- Red (0-25), Orange (25-50), Yellow (50-75), Green (75-100)
- Mobile: abbreviate to "F&G: 8"
- Done when: F&G visible on dashboard

## Files Changed

| File | Action |
|------|--------|
| `shared/models.py` | MODIFY (add NewsSentiment, NewsHeadline, update MarketSnapshot) |
| `data_service/news_client.py` | NEW |
| `config/settings.py` | MODIFY (add news settings) |
| `data_service/service.py` | MODIFY (init client, polling loop, snapshot) |
| `main.py` | MODIFY (pre-filter F&G gate) |
| `ai_service/prompt_builder.py` | MODIFY (add news section to Claude prompt) |
| `tests/test_news_client.py` | NEW |
| `requirements.txt` | MODIFY (add aiohttp) |

## Risks

1. **alternative.me goes down** — unlikely (running since 2018), but mitigate: graceful degradation (None = skip check).
2. **cryptocurrency.cv goes down / changes API** — community project. Mitigate: headlines are optional context for Claude, not a hard gate. Bot works fine without them.
3. **Cloudflare blocks cryptocurrency.cv** — already seen (403 without User-Agent). Mitigate: proper headers + graceful fallback.
4. **Sentiment lag** — F&G is reactive, not predictive. A crash happens, F&G drops AFTER. Mitigate: use only as "don't fight extreme sentiment" gate, not as entry signal.
5. **Over-filtering** — if thresholds too aggressive, blocks valid trades. Mitigate: start at 15/85 (extreme only), tune after 2 weeks.

## Out of Scope
- WorldMonitor.app integration — too geopolitical
- Custom NLP/sentiment model — unnecessary
- Social media sentiment (Twitter/X, Reddit) — different data source
- Individual article sentiment scores — API doesn't provide them
- Sentiment as trade ORIGINATOR — filter only, never generates setups
- CryptoPanic fallback — can add later if cryptocurrency.cv dies

## Sources
- [alternative.me Fear & Greed](https://api.alternative.me/fng/) — primary F&G score (TESTED, WORKING)
- [free-crypto-news / cryptocurrency.cv](https://github.com/nirholas/free-crypto-news) — headlines (TESTED, /api/news works)
- [CryptoPanic API](https://cryptopanic.com/developers/api/) — backup (not used in v1)
