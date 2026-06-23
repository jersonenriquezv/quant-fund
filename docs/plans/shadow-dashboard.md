# Plan — Shadow Dashboard (mirror of real-trades dashboard for shadow mode)

**Status:** PLANNED (not started). Created 2026-06-23.
**Problem:** Dashboard "Trades" tab reads `SELECT * FROM trades` = live OKX executions only. That table froze at 43 closed rows on 2026-04-09 because the bot went **shadow-only 2026-04-15** (no live orders since). All current activity (engine1, legacy A/B/D/F, benchmarks, DT) lives in `ml_setups` / DT shadow logs, which the dashboard has **zero** endpoints for. So the dashboard looks dead at ~74 days.
**Goal:** A Shadow section in the dashboard mirroring the real-trades views — trades opening now (which setup, pair, dir, entry/SL/TP, live state), per-setup stats (WR/PF/profit), synthetic equity/balance/profit, and ML training status.

## Decisions (locked 2026-06-23, user)
1. **Equity basis = synthetic curve from `pnl_usd`.** No real shadow account exists. Equity = fixed starting balance (paper, default $10,000) + cumulative `pnl_usd` of resolved shadow rows, ordered by `resolved_at`, scoped to the current `EXPERIMENT_ID`. Profit = sum `pnl_usd`. Mirrors the DT paper-book style.
2. **Scope = all `ml_setups` shadows + DT shadow integrated.** engine1 + legacy A/B/D/F + benchmarks, filterable by `setup_type` / `experiment_id`. DT shadow (separate $10k paper book, currently logs-only, NOT in `ml_setups`) gets integrated — requires persisting DT state to DB first (its own phase).
3. **MVP first.** Phase 1 = trades list + per-setup stats. Phase 2 = synthetic equity curve + balance/profit panel. Phase 3 = ML training status panel. Phase 4 = DT shadow persistence + merge into the feed.

## Data facts (verified 2026-06-23)
- `ml_setups`: 139 cols, 13,113 rows, max `created_at` = today (live). Per-row fields ready: `setup_type, pair, direction, entry_price, sl_price, tp1_price, tp2_price, entry_distance_pct, sl_distance_pct, outcome_type, pnl_pct, pnl_usd, actual_entry, created_at, resolved_at, shadow_position_size, shadow_leverage, shadow_margin, experiment_id`.
- **"Open now"** = `outcome_type IS NULL`. CAVEAT: ancient orphaned unresolved rows exist (e.g. `setup_b` BTC from 2026-04-08, never resolved). The open-trades query MUST bound by recency (e.g. `created_at > now() - interval '48 hours'`) or filter to live experiment, else dead orphans show as "open".
- **Resolved outcome whitelist** (exclude non-trades): reuse the training filter — exclude `shadow_dedup, shadow_direction_filtered, shadow_pair_filtered, shadow_no_fill, data_blocked, risk_rejected, ai_rejected, replaced, ...` (see `data_service.data_store.VALID_OUTCOMES` + MEMORY training query). Terminal trade outcomes = `shadow_tp, shadow_sl, shadow_breakeven, shadow_time_stop, shadow_timeout`.
- **Fee note:** `pnl_usd` is ALREADY net of taker fees ×2 (`compute_pnl`). Do NOT re-deduct fees ([[feedback_pnl_already_net_of_fees]]).
- **DT shadow:** state in logs (`DT_SHADOW_STATE` / `DT_SHADOW_TRADE`), paper book $10k, in `execution_service/dual_thrust_shadow.py`. NOT in `ml_setups`. Needs a persistence table to surface.

## Architecture (mirror the existing dashboard)
- **Backend:** FastAPI, `dashboard/api/`. Routers registered in `main.py` under `/api`. Queries in `queries.py` (asyncpg via `db.pg_pool`). Mirror `get_trade_stats()` pattern.
- **Frontend:** Next.js `dashboard/web/src/app/`, route-per-folder (`pending`, `manual`, `chart`, `bybit`, ...). Add `shadow/`. Components in `src/components/`. **Mobile responsive mandatory** (375px+, 2 breakpoints in `globals.css`) per CLAUDE.md.

---

## Phase 1 — Shadow trades + per-setup stats (MVP) — ✅ DONE 2026-06-23
**Shipped:** `queries.get_shadow_trades` + `get_shadow_stats` (terminal-outcome whitelist `SHADOW_TERMINAL_OUTCOMES`, 48h orphan-recency bound on open, `EXPERIMENT_ID` scope + override), `routes/shadow.py` (`GET /api/shadow/trades`, `/api/shadow/stats`), models `ShadowTradeRecord`/`ShadowStats`/`ShadowSetupBreakdown`, frontend `web/src/app/shadow/page.tsx` + nav link (Header). Tests `tests/test_shadow_queries.py` (6 pass). Build clean, /shadow 3.3 kB. Live smoke: 1633 resolved shadows, WR 18.2% PF 0.69.
**Caveat surfaced:** aggregate headline stats mix benchmark arms (`bench_engine1_random_direction`, `bench_engine1_market_now`) into total WR/PF — per-setup breakdown separates them. Phase 2+ may want a benchmark exclude toggle on the headline.

**Backend** (`dashboard/api/`):
- `queries.py`:
  - `get_shadow_trades(status, setup_type, limit, offset)` — reads `ml_setups`. `status='open'` → `outcome_type IS NULL AND created_at > now()-interval '48 hours'`; `status='closed'` → terminal-outcome whitelist. Scope `experiment_id = settings.EXPERIMENT_ID` (default; overridable). Return `setup_type, pair, direction, entry_price, sl_price, tp1_price, tp2_price, outcome_type, pnl_pct, pnl_usd, created_at, resolved_at`.
  - `get_shadow_stats(setup_type=None)` — mirror `get_trade_stats` but over `ml_setups` terminal-whitelist: total, WR, PF, total_pnl_usd, avg/best/worst pnl_pct. Plus a **per-setup_type breakdown** array.
- `routes/shadow.py` — `GET /api/shadow/trades`, `GET /api/shadow/stats`. Register in `main.py`.
- `models.py` (api) — `ShadowTradeRecord`, `ShadowStats`, `ShadowSetupBreakdown`.

**Frontend** (`dashboard/web/src/app/shadow/`):
- `page.tsx` — Shadow tab: open-shadows table (setup, pair, dir, entry/SL/TP, age) + closed-shadows table (+ outcome, pnl) + stats header cards (WR/PF/profit/total) + per-setup breakdown table. Reuse real-trades components/styles. Hide low-priority columns on mobile (`display:none` classes); tables scroll-x.
- Add nav link to the Shadow page.

**Tests:** pytest for the two query builders (whitelist correctness, orphan-recency bound, experiment scope). Patch settings explicitly ([[feedback_tests_env_coupling]]).

**Done when:** Shadow tab shows today's opening shadows + correct per-setup WR/PF/profit; mobile clean at 375px; tests green.

## Phase 2 — Synthetic equity / balance / profit
- `queries.py`: `get_shadow_equity_curve(start_balance=10000, experiment_id=...)` — resolved terminal shadows ordered by `resolved_at`, running `start_balance + cumsum(pnl_usd)`. Return points `[{ts, equity, pnl_usd, setup_type}]` + summary (current balance, total profit, max drawdown, return %).
- `routes/shadow.py`: `GET /api/shadow/equity`.
- Frontend: equity curve chart on the Shadow page + balance/profit/DD cards. (Charting lib already in repo — reuse.)

## Phase 3 — ML training status panel
- Surface the engine1 meta-label forward-validation state: forward N / 30 gate, last freeze cutoff + train N, OOF/OOT PF, latest AUC, last `ml_v1_forward_check` verdict.
- Source: `ml_v1_forward_check.py` currently logs to `~/logs/ml_forward_check.log` (not DB). Options: (a) have the forward checker write a small JSON status file / row the API reads, or (b) the API recomputes on demand. Decide at Phase 3 start. Lean (a) — cheap, no DB load.
- Frontend: "ML Training" card — gate progress bar (N/30), AUC, last verdict, next-step text.

## Phase 4 — DT shadow integration
- Persist DT shadow trades: add `dt_shadow_trades` table (or reuse a generic shadow table); write in `dual_thrust_shadow.py` on `new_trades` (flip/SL closes) — order-free, no risk/execution touch.
- Merge DT trades into the shadow feed (UNION or separate panel tagged `dual_thrust_eth`). DT keeps its own $10k paper equity (already computed in the tracker) shown as a separate equity line.
- Backfill existing DT trades from logs if cheap; else forward-only.

---

## Guardrails
- Read-only over `ml_setups` — no writes, no risk/execution path touched (Phases 1–3).
- Mobile responsive every phase (CLAUDE.md hard rule).
- `pnl_usd` already net of fees — never re-deduct.
- Bound "open" by recency to avoid surfacing orphaned ancient unresolved rows.
- Doc-update after each phase: SYSTEM_BASELINE if any config, `docs/context/` for dashboard behavior.

## Out of scope (v1)
- Editing/cancelling shadow trades (read-only viewer).
- Replacing the real-trades tab (kept; Shadow is additive).
- Bybit manual (already has its own `/bybit` page).
