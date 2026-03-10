## What
Overhaul Telegram notifications: stop whale noise, kill hourly status, add notifications that actually matter for a one-man operation.

## Why
Current notifications are noise, not signal:
- **Whale movements**: Neutral transfers (transfer_out, transfer_in) are being sent to Telegram. These are inter-wallet movements with no directional signal. The tiering in `_notify_new_movements()` already filters small neutrals, but ALL exchange deposits/withdrawals still fire — including medium-significance ones (~$200K) that don't move markets for BTC/ETH.
- **Hourly status**: Every hour is too frequent for a $108 account. It clutters the phone and provides no actionable info (prices + bias you can check on Grafana anytime).
- **Missing notifications**: Several events that SHOULD notify don't — drawdown warnings, breakeven SL moves, entry order fills/cancellations, daily P&L summary, bot crash/restart.

## Current State (verified from code)

**Whale alert tiering** (`data_service/service.py:422-448`):
- Tier 1: `exchange_deposit` / `exchange_withdrawal` → always notify
- Tier 2: Non-exchange transfers → notify only if `high` significance OR `amount_usd > $500K`
- Tier 3: Small non-exchange → log only
- Market makers → only on `high` significance
- High significance → sent immediately (bypass 2min batch)

**Problem**: Tier 1 has no minimum USD filter. Medium-significance exchange deposits (100-1000 ETH, $200K-$2M) still fire. With 30+ ETH wallets + BTC wallets, you're getting multiple alerts daily that are too small to impact BTC/ETH price.

**Hourly status** (`main.py:384-434`): Runs every 3600s, sends uptime/prices/positions/DD.

**Existing notifications that ARE useful**:
- AI decisions (approved/rejected) ✅
- Trade opened/closed ✅
- Emergency close ✅
- OB summary on 4H close ✅
- Health check down/recovered ✅

## Steps

### 1. Whale alerts: Only exchange movements + only high significance → `data_service/service.py` + `config/settings.py`
- Add setting `WHALE_NOTIFY_EXCHANGE_ONLY = True` (default)
- Add setting `WHALE_NOTIFY_MIN_USD = 1_000_000` ($1M minimum for notifications)
- Modify `_notify_new_movements()`:
  - When `WHALE_NOTIFY_EXCHANGE_ONLY=True`: skip `transfer_out`/`transfer_in` entirely (still collected for AI context + dashboard, just no Telegram)
  - When `WHALE_NOTIFY_MIN_USD > 0`: skip movements below this USD value
  - Always skip medium-significance exchange movements — only `high` gets notified
- Result: You only get notified when a known whale deposits/withdraws >$1M to/from an exchange. That's a real signal.
- Done when: Neutral whale alerts stop arriving. Only large exchange deposits/withdrawals notify.

### 2. Kill hourly status → `main.py`
- Remove `_hourly_status_loop()` and its `asyncio.create_task` in `main()`
- Remove `notify_hourly_status` from `alert_manager.py` and `notifier.py`
- The data is still available on Grafana (:3001) 24/7 — no information lost
- Done when: No more hourly Telegram messages.

### 3. Add daily summary (replace hourly) → `main.py` + `alert_manager.py`
- New `_daily_summary_loop()` that runs once at 00:00 UTC
- Message content:
  - Date + uptime
  - Trades executed today (count + net P&L)
  - Win/loss breakdown
  - Current balance + daily DD%
  - Open positions (if any)
  - Setups detected vs AI approved vs executed (funnel)
- Priority: INFO
- Done when: One summary per day at midnight UTC arrives on Telegram.

### 4. Add missing critical notifications → `alert_manager.py` + callers
- **Breakeven SL move**: When SL moves to entry (currently only logged in `monitor.py`). Priority: INFO. One line: "BTC LONG — SL moved to breakeven ($87,234)"
- **Trailing SL move**: When SL moves to TP1. Priority: INFO. Same format.
- **Entry order cancelled**: When limit order expires after 15min unfilled. Priority: INFO. "BTC LONG entry expired — not filled in 15min"
- **Drawdown warning**: When daily DD > 2% (66% of 3% limit). Priority: WARNING. "Daily DD at 2.1% — approaching 3% limit"
- **Bot restart**: Send "BOT STARTED" on startup with mode (demo/live) + capital. Priority: CRITICAL. Already know uptime from this.
- Done when: Each of these events triggers a Telegram message.

### 5. Add notification format improvement → `alert_manager.py`
- For whale alerts that DO pass the filter, add bullish/bearish emoji + signal interpretation:
  - Exchange deposit: 🔴 BEARISH — selling pressure
  - Exchange withdrawal: 🟢 BULLISH — accumulation
- This is what user asked for — whale alerts should tell you the direction, not just the raw data.
- Done when: Whale alerts clearly show BEARISH or BULLISH signal.

## Risks
- **Daily summary timing**: If bot restarts at 23:59, you miss the summary. Mitigation: persist last summary time to Redis. On restart, check if today's summary was already sent.
- **Breakeven/trailing SL notifications**: If multiple positions active, could get 3-6 SL move alerts in a burst. Acceptable — these are actionable.
- **$1M minimum too high**: For $108 capital, even $200K whale moves don't affect your P&L. At $1M you're filtering to truly market-moving events. Can be lowered via settings if needed.

## Out of Scope
- **Dashboard notification preferences panel** — overkill for now. Settings in `settings.py` is fine.
- **Telegram bot commands** (e.g., /status, /silence) — would be cool but not needed yet. Grafana handles status.
- **Push notification sound differentiation** — Telegram doesn't support this natively.
- **Slack/Discord integration** — Telegram is fine for a one-man operation.
