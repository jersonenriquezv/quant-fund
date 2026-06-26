# Event Blackout Calendar — Implementation Plan

## What
Block the bot from opening new trades during scheduled macro events (FOMC, CPI, NFP) when volatility is unpredictable and spreads widen. A JSON calendar + a single guardrail check.

## Why
Macro events like FOMC decisions cause 2-5% moves in seconds. During these windows:
- Spreads widen → limit orders skip, SL fills at worse prices
- Liquidation cascades trigger false signals (fake sweeps, fake BOS)
- SMC patterns break down — the move is news-driven, not structure-driven
- With $20 margin trades, a single bad fill during FOMC can wipe the trade's edge

The bot has zero awareness of scheduled events. A $100 account can't afford to be in the market during a Fed announcement.

## Current State
- **Risk Service** (`risk_service/guardrails.py`): 7 stateless checks, each returns `(passed, reason)`. Adding one more is trivial.
- **Settings** (`config/settings.py`): All thresholds centralized. Pattern already exists for enable/disable flags.
- **No event awareness whatsoever.** No calendar, no blackout, no scheduled pauses.
- **Existing positions stay open** — SL/TP orders live on the exchange. Blackout only blocks NEW entries.

## Steps

### 1. Create blackout calendar file
**File:** `config/blackout_calendar.json` (NEW)
```json
{
  "description": "Scheduled macro events — bot will not open trades during these windows",
  "events": [
    {
      "name": "FOMC Decision",
      "datetime_utc": "2026-03-19T18:00:00Z",
      "blackout_minutes_before": 30,
      "blackout_minutes_after": 30
    },
    {
      "name": "CPI Release",
      "datetime_utc": "2026-04-10T12:30:00Z",
      "blackout_minutes_before": 15,
      "blackout_minutes_after": 30
    }
  ]
}
```
- Manually maintained (user adds events from economic calendar)
- UTC timestamps. Before/after windows configurable per event.
- FOMC gets 30/30 (bigger impact). CPI/NFP get 15/30.
- Done when: file exists with at least one event

### 2. Add settings
**File:** `config/settings.py` (MODIFY)
```python
# Event Blackout Calendar
EVENT_BLACKOUT_ENABLED: bool = True
EVENT_BLACKOUT_FILE: str = "config/blackout_calendar.json"
```
- Done when: flag + path in settings

### 3. Add guardrail check
**File:** `risk_service/guardrails.py` (MODIFY)
```python
def check_event_blackout(self, current_time: int) -> tuple[bool, str]:
    """Check that no scheduled macro event blackout is active.

    Reads config/blackout_calendar.json, checks if current_time falls
    within any event's [datetime - before, datetime + after] window.
    """
```
- Load calendar once, cache in memory (reload on file change or every hour)
- Auto-prune past events (> 24h old) from cache
- File missing or invalid → pass (no blackout = don't block trades)
- Done when: check returns (False, reason) during blackout windows

### 4. Wire into RiskService
**File:** `risk_service/service.py` (MODIFY)
- Add `check_event_blackout(now)` to the checks list in `check()`
- Same pattern as existing guardrails — fail fast
- Done when: blackout rejects trades during event windows

### 5. Telegram notification
- When a trade is rejected due to blackout, the existing rejection flow sends a log WARNING
- Optionally: log the event name so the user knows WHY ("FOMC Decision in 15 min")
- No new notification code needed — existing guardrail rejection path handles it

### 6. Tests
**File:** `tests/test_guardrails.py` (MODIFY)
- Test: during blackout window → rejected
- Test: outside window → passed
- Test: no calendar file → passed (graceful)
- Test: expired events ignored
- Test: multiple events, one active → rejected
- Done when: tests pass

## Files Changed

| File | Action |
|------|--------|
| `config/blackout_calendar.json` | NEW — event list |
| `config/settings.py` | MODIFY — add 2 settings |
| `risk_service/guardrails.py` | MODIFY — add `check_event_blackout()` |
| `risk_service/service.py` | MODIFY — wire new check |
| `tests/test_guardrails.py` | MODIFY — add blackout tests |

## Risks

1. **Forgetting to update the calendar** — User must manually add events. Mitigate: keep it minimal (only FOMC 8x/year + CPI 12x/year = 20 entries/year). Future: scrape economic calendar API.
2. **Timezone bugs** — All times in UTC, bot already uses UTC internally. No conversion needed.
3. **Stale events pile up** — Auto-prune past events from cache. File itself can be cleaned quarterly.
4. **Over-blocking** — 30min before + 30min after = 1 hour per event. With FOMC 8x/year, that's 8 hours/year total. Negligible.

## Out of Scope
- Automatic economic calendar scraping (future, when scaling to $1K+)
- Closing existing positions before events (SL/TP on exchange handle this)
- Different blackout windows per pair (BTC and ETH react similarly to macro)
- Dashboard UI for managing events (JSON file is enough for one person)
- Fundamental analysis or interpretation of events (that's the human's job)

## Priority
**Low — implement when scaling to $1,000+.** At $108 capital with $20 trades, the absolute dollar impact of a bad FOMC fill is ~$0.50. The guardrail architecture is simple enough to build in 1 hour when needed.
