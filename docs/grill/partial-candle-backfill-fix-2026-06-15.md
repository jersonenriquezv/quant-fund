# Grill: Partial-candle backfill bug fix

**Date:** 2026-06-15
**Topic:** Bot's Postgres `candles` store holds partial (forming) bars; fix source + repair history + re-flag affected ML rows.
**Verdict:** BUILD — proven bug (not vibes), root cause traced to 3 code links, surgical fix. Scope confirmed by user: fix + repair history + re-flag ML.

## Context loaded
- CLAUDE.md, MEMORY.md, git log/status (session start).
- `scripts/dual_thrust_candle_parity.py` (the tracer that surfaced this, Dual Thrust Phase 1b-P1).
- `data_service/websocket_feeds.py`, `data_service/exchange_client.py`, `data_service/service.py`, `data_service/data_store.py`.
- Memory: [[project_partial_candle_bug]], [[project_chart_live_reconcile_fix]].
- Key facts: SHADOW-ONLY since 2026-04-15, ENABLED_SETUPS=[], ML_FEATURE_VERSION=18, 12,370 ml_setups (12,344 with outcome). Today fixed a 29h OKX-null-market-id crash loop.

## Evidence — the bug is proven, not hypothesized

Tracer over 299 overlapping ETH/USDT 4h bars: **143/399 bars (~36%) differ from OKX REST `4H`**, and **143/143 have the bot's range strictly inside REST's range** (bot high ≤ rest high AND bot low ≥ rest low; open always matches = first tick). Cross-pair/TF sweep (BTC/ETH/SOL × 4h/1h/15m): bad% scales with TF duration (4h 35.8% > 1h 12.0% > 15m ~1%) with **identical bad counts per TF across pairs** (107/107/107 at 4h) → bad bars occur at the same timestamps for all pairs = global backfill/reconnect events, not per-pair tick loss.

## Root cause — 3 code links (smoking gun)

1. `exchange_client.py:146` — `backfill_candles` calls ccxt `fetch_ohlcv`, which returns the **currently-forming bar** as the last element. It's stored `confirmed=True` (line 198 comment "Historical candles are always confirmed" is false for that last bar).
2. `data_store.py:948` — candle upsert is `ON CONFLICT (pair,timeframe,timestamp) DO NOTHING`. The later authoritative WS `confirm=1` bar for the same ts is **silently dropped** → the partial is frozen forever.
3. `service.py:103` → `_on_ws_reconnect` → `_backfill_all` runs on startup **and every reconnect**. 50 days of restarts (incl. today's 29h crash loop) accumulated the partials. Longer TFs are more often still-forming at a reconnect instant → more partials. Mechanism predicts the observed 4h>1h>15m direction exactly.

`websocket_feeds.py` itself is clean — it only stores `confirm=="1"` bars. The corruption is 100% backfill + DO-NOTHING.

## Decision tree (resolved from code, per skill rule 4)

- **What current bug motivates this?** Quantified: 143/399 ETH 4h bars wrong; systemic across pairs/TFs. Not speculative.
- **Does it touch risk/execution?** No. `data_service` only (exchange_client, data_store) + a repair script. No money path.
- **Rollback plan?** Fix A/B are additive guards; revert = restore `DO NOTHING` + drop the forming-bar filter. Repair script is idempotent (re-fetch REST = authoritative); re-runnable.
- **Cheaper alternative?** Considered "go-forward only" (A+B, no history repair) and "pivot Dual Thrust to REST-direct, ignore bug." Rejected: the bug taints every SMC setup (OB/FVG/sweep read high/low), every ML feature, and /chart — go-forward-only leaves 12k ml_setups + dashboards dirty. User chose full repair.
- **Survivorship/overfit?** N/A — data-integrity bug, no edge claim.

## Fix design (for /phased-plan)

- **Fix A (prevent at source):** in `backfill_candles`, drop any bar whose close-time (`ts + tf_ms`) > now → never store a forming bar. ~3 lines + a unit test.
- **Fix B (safety net):** candle upsert → `ON CONFLICT DO UPDATE SET open/high/low/close/volume/volume_quote = EXCLUDED.*`. Safe once A stops forming bars entering; lets an authoritative re-fetch or later WS bar correct a stale partial. Guard: only update CLOSED bars (A guarantees this).
- **Fix C (repair history):** one-shot script — for each pair/tf, re-fetch closed bars from OKX REST and overwrite the known partials (needs Fix B's DO UPDATE, or explicit UPDATE). Verify with the parity tracer → 0 mismatches.
- **Fix D (re-flag ML):** identify ml_setups/shadow outcomes computed over a now-known-partial bar and mark/exclude them from training. **Blast radius unsized** — 12,370 rows, 138 cols; setups carry `ob_timeframe`, `htf_bias`, `shadow_*_candle_tf`. 15m partials ~1% (LTF setups mostly OK); 4h/1h partials hit HTF-bias features harder. Needs its own investigation phase — do NOT hand-wave.

## Final verdict — BUILD

Proven bug, traced to exact lines, surgical fix (~2 code changes + 2 scripts). Touches no money path. The only genuinely open work is sizing + method for ML re-flag (Fix D), which is a planning/investigation task, not a proof gap. Strong build.

## Pre-conditions for /phased-plan
- Confirm ccxt OKX `fetch_ohlcv` returns the forming bar last (write the close-time guard to be exchange-agnostic regardless).
- Decide Fix B update predicate: overwrite always vs only when EXCLUDED differs (avoid needless writes / WS-vs-REST volume-unit mismatch — note `volume_quote` is approximated `vol*c` in backfill vs real quote in WS; reconcile so the safety net doesn't thrash).
- Fix D investigation: quantify how many ml_setups had a partial bar in their feature lookback window; choose re-flag (new `outcome_type` exclusion tag in `VALID_OUTCOMES` / `NON_MARKET_OUTCOMES`) vs date-cutoff filter. Respect ML_FEATURE_VERSION discipline (no silent column meaning change).
- Verification: parity tracer must read 0 mismatches post-repair on all 7 pairs × {4h,1h,15m}.

## Handoff
Next: `/phased-plan partial-candle-backfill-fix` using this grill doc as input.
