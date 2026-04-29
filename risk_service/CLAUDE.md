# Risk Service — CLAUDE.md

Operational rules for Claude when modifying `risk_service/`. Capital guardian — if a check fails, no trade.

## Purpose
9 sequential guardrails + dynamic position sizing. Decides approve/reject for every `TradeSetup` before it reaches execution.

## Source of truth (read before editing)
- **Detailed behavior:** `docs/context/04-risk.md` (Spanish, deep — flow diagram, every guardrail, state lifecycle, FAQ, changelog)
- **Active limits / thresholds:** `docs/SYSTEM_BASELINE.md` — RISK_PER_TRADE, MAX_LEVERAGE, drawdown caps, portfolio heat, etc.
- **Models:** `shared/models.py` — `TradeSetup`, `RiskApproval`

## Files
| File | Role |
|---|---|
| `service.py` | Facade `RiskService(capital, data_service)`. Composes Guardrails + PositionSizer + StateTracker. `check(setup, ai_confidence, dry_run)` is the entry point |
| `guardrails.py` | Pure functions, no state. Each returns `(passed: bool, reason: str)` |
| `position_sizer.py` | `position_size = (capital × risk_pct) / |entry - sl|`, leverage capped at `MAX_LEVERAGE` |
| `state_tracker.py` | In-memory state with Redis persistence. Daily/weekly PnL, cooldown, open positions, capital |

## Rules — modifying guardrails
1. **Guardrails must remain pure.** No I/O, no state mutation. Inputs in, `(bool, str)` out.
2. **Order matters — fail fast.** First NO is the final NO. Do not move state-touching checks before structural checks.
3. **Structural checks run in dry_run too.** State checks (cooldown, DD, max trades) skip in `dry_run=True`. Min risk distance, max SL pct, R:R must always run.
4. **Every new guardrail needs a setting in `config/settings.py`** with a sensible default and a row in SYSTEM_BASELINE.
5. **Both layers enforce critical bounds.** `MIN_RISK_DISTANCE_PCT` is checked in strategy layer (`_check_sl_distance`) AND here. Do not remove either side.
6. **Portfolio heat runs AFTER position sizing** — needs the computed size. Keep this ordering.

## Rules — modifying state tracker
1. **Every mutation must persist to Redis.** `_persist_state()` must run on every state change. Failures degrade silently — bot must NOT stop.
2. **Date reset uses `date()` objects, not `tm_yday`.** Year-boundary bug fixed 2026-03-04. Do not regress.
3. **`record_trade_closed` matches by `(pair, direction)`.** Long+short on same pair are independent positions.
4. **`record_trade_cancelled` removes pending without counting as a trade.** Do not collapse the three lifecycle methods (opened/closed/cancelled).
5. **Drawdown reconciliation on startup** uses worse of Redis vs Postgres. Never trust Redis alone after restart.
6. **Capital comes from exchange first, fallback to tracked.** `refresh_capital_from_exchange(force=True)` after every realized close. 5-min TTL cache otherwise.

## Rules — modifying sizing
1. **`MAX_MARGIN_PCT_OF_CAPITAL` is the hard cap.** If risk_pct exceeds it, recompute with capped risk. Bet sizing must never exceed this.
2. **Min order size check is post-sizing, not pre.** Reject after computing size, with a clear message naming the pair.
3. **Bet sizing only fires when AI is active** (`ai_confidence < 1.0`). With AI bypass everywhere, this path is currently inactive — do not delete it.

## Never
- Make a guardrail async or I/O-bound. They must be microsecond-fast.
- Allow a trade to skip portfolio heat. It is the global ceiling.
- Use raw dicts between guardrails and the facade. Stay on `TradeSetup` / `RiskApproval`.
- Set MAX_DAILY_DRAWDOWN < MAX_WEEKLY_DRAWDOWN. They are equal by design (10%/10%) — daily only catches catastrophic days.
- Hardcode capital. Always pull from `RiskStateTracker.get_capital()`.

## Verify after changes
```bash
python -m pytest tests/test_risk_service.py tests/test_guardrails.py tests/test_position_sizer.py tests/test_state_tracker.py -v --tb=short
```

## Telemetry
- Rejections persisted to `trade_rejections` table in PostgreSQL
- Risk events to `risk_events` table
- Every check emits structured log line via Loguru
