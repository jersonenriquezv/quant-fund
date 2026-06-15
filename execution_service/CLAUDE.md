# Execution Service — CLAUDE.md

Operational rules for Claude when modifying `execution_service/`. The arm that places orders on OKX. Mistakes here cost real money.

## Purpose
Receive `RiskApproval` → place entry + attached SL/TP on OKX → manage position lifecycle (breakeven, trailing, exits) → persist trade outcome.

## Source of truth (read before editing)
- **Detailed behavior:** `docs/context/05-execution.md` (Spanish, deep — full flow, state machines, OKX algo handling, split entry, HTF campaigns, PnL calc)
- **Active config:** `docs/SYSTEM_BASELINE.md` — timeouts, slippage cap, fee rate, trailing params
- **Models:** `shared/models.py` (`TradeSetup`, `RiskApproval`) + `execution_service/models.py` (`ManagedPosition`, `PositionCampaign`, `CampaignAdd`)

## Files
| File | Role |
|---|---|
| `service.py` | Facade `ExecutionService`. `execute(setup, approval, ai_confidence)`, `start()`, `stop()`, `health()`. Position adoption, ORDER PLACED Telegram |
| `executor.py` | ccxt wrapper. Order placement, contracts conversion (`_to_contracts`, `contracts_to_base`), algo order fallbacks, deterministic `clOrdId` |
| `monitor.py` | Async loop (5s poll). SL/TP discovery, manual fallback, breakeven, trailing, slippage guard, sl_too_close, emergency retry |
| `campaign_monitor.py` | HTF position trades. Pyramid adds, trailing SL on 4H swings, 7d timeout |
| `shadow_monitor.py` | Shadow position tracker. No exchange orders — simulates TP/SL/timeout from price ticks. ML outcome resolution |
| `dual_thrust_shadow.py` | Dual Thrust order-free shadow tracker. On each confirmed ETH 4h candle, replays the validated brain + harness fill model (verbatim port — stop-and-reverse, ATR stop) on fresh OKX REST 4h bars. No orders, no risk/execution path. Gated by `DUAL_THRUST_SHADOW_ENABLED`. See `docs/plans/dual-thrust-phase1b-shadow-wiring.md` |
| `position_guardian.py` | Cross-cutting safety checks |
| `models.py` | `ManagedPosition`, `PositionCampaign`, `CampaignAdd` dataclasses |

## Rules — modifying order placement
1. **Validate price ordering before any order.** Long: `sl < entry < tp2`. Short: `sl > entry > tp2`. Reject otherwise — never trust upstream.
2. **SL+TP attached to entry.** Pass `stopLoss`/`takeProfit` to `place_limit_order`. OKX creates them atomically on fill. Manual placement is fallback only.
3. **Notify Risk on PLACE, not on fill.** Otherwise heat is undercounted while pending fills.
4. **Cancelled entries do NOT count as trades.** Use `on_trade_cancelled`, not `on_trade_closed`.
5. **New SL is placed BEFORE old SL is cancelled.** Zero-window-without-protection rule. Applies to breakeven, trailing, and adjustments.
6. **`clOrdId` is deterministic** (md5 of pair+side+price+contracts). Do not regenerate per attempt — it dedupes against OKX on network retries.
7. **Contracts conversion is mandatory.** Base currency in, contracts out for OKX. `ctVal` per pair is in `executor.py`. Never send base-currency amounts to OKX SWAP.
8. **Split entry guards min order size.** If half-size < `MIN_ORDER_SIZES[pair]`, fall back to single entry. Never silently truncate.

## Rules — modifying the monitor
1. **Per-position try/catch in poll loop.** One position's error must NOT block others. The wrapping try/catch is non-negotiable.
2. **SL vanished fallback uses 12 polls (~60s).** If SL not found:
   - Position gone → SL triggered, close + mark OB failed
   - Position exists → re-place SL
   - Network error (`fetch_position` returns None) → skip, retry
   - `POSITION_EMPTY` (`{}`) means API succeeded but no position — close
3. **Periodic SL verification** (`SL_VERIFY_INTERVAL_SECONDS`, 60s) catches OKX silent-failure bug. Do not remove.
4. **Post-fill SL distance check.** If `abs(fill - sl) / fill < MIN_RISK_DISTANCE_PCT` after slippage → close as `sl_too_close`. Fees would eat the trade.
5. **Slippage guard.** If `abs(actual_entry - entry) / entry > MAX_SLIPPAGE_PCT` (0.3%) → close as `excessive_slippage`. Skipped in sandbox.
6. **`_on_sl_hit` callback is wrapped.** If marking OB failed raises, `_close_position` must still run. Bug from past — do not regress.
7. **TP failure does NOT trigger emergency close.** SL alone is sufficient protection. Old behavior was wrong.

## Rules — PnL & persistence
1. **`_calculate_pnl()` runs on EVERY exit path.** TP, SL, breakeven, trailing, emergency, slippage, sl_too_close, timeout, manual_close. No exit may persist without going through it.
2. **PnL is net of fees AND funding.** `TRADING_FEE_RATE` (0.05% per side) deducted; funding deducted for positions crossing 8h windows.
3. **`pnl_pct` is capital-based, not notional-based.** DD guardrails measure real account impact. Falls back to notional only if risk service unavailable.
4. **`setup_id` propagates** TradeSetup → ManagedPosition → `trades.setup_id` → `_ml_resolve_close()` resolves the `ml_setups` row outcome.
5. **`manual_close` maps to `filled_timeout`** in ML outcome (not a distinct outcome).
6. **Orphaned trades on restart.** `_reconcile_orphaned_trades()` marks open trades without a live position as `orphaned_restart` with `pnl_usd=NULL`. Never invent SL-based estimates.
7. **Downstream readers MUST filter `orphaned_restart`** to avoid contaminating DD/stats.

## Rules — adoption & coexistence
1. **Adopted positions get SL recovery.** `_extract_adopted_sl` queries algo orders, picks correct side. Fallback: `entry ± entry × MAX_SL_PCT`. Always log WARNING when fallback used.
2. **`ALLOW_BOT_WITH_MANUAL=false` is the default.** Bot signals rejected when manual position open on same pair. Emit `bot_signal_blocked_by_manual` metric. Opt-in only with full understanding of heat undercounting.

## Never
- Send `reduceOnly=True` on stop-market in net mode (OKX error 51205).
- Cancel an SL without immediately re-placing or closing the position.
- Skip `_calculate_pnl` on any exit path.
- Use `nohup`, `kill`, or `sudo` to manage the bot — always `docker compose up -d --build bot`.
- Modify `clOrdId` derivation without understanding the dedup contract.
- Place an order without checking `is_ob_failed()` first (already wired in `main.py`).

## Verify after changes
```bash
python -m pytest tests/test_execution.py -v --tb=short
```

For real-order verification (live OKX, manual): `tests/test_execution_live.py`

## Telemetry — emitted metrics
`pending_replaced`, `pending_timeout`, `pending_filled`, `time_to_fill_seconds`, `timeout_exit_spread_pct`, `orphan_reconcile_count`, `bot_signal_blocked_by_manual`, `on_sl_hit_callback_error`, `shadow_outcome_resolved_ok/_error`. All to PostgreSQL `bot_metrics` table, fire-and-forget.
