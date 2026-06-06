# Audit: Bot distance from AFML + Wyckoff (notes.md second pass)

**Date:** 2026-06-01
**Source:** `notes.md` (Wyckoff Theory, Backtesting, AFML insights / "From determinism to probability" — lines 41-196)
**Scope:** How far is the CURRENT bot from the AFML stations + Wyckoff + backtest discipline the user transcribed. Evidence-based (file:line). Companion to the inducement/confirmation audit (`docs/grill/smc-inducement-pullback-2026-06-01.md`).
**Mode reminder:** Bot SHADOW-ONLY (`ENABLED_SETUPS=[]`, ~$86 OKX untouched). ML v0 baseline AUC 0.70 (N=148), re-run 2026-06-08. `ML_FEATURE_VERSION=18` collection in progress.

---

## Scorecard

| # | Concept (notes) | Status | One-line |
|---|---|---|---|
| S1 | Event-based bars (volume/dollar/tick) vs time bars | 🔴 **ABSENT** | 100% time bars; zero dollar/volume/imbalance sampling |
| S2 | Fractional differentiation / stationarity | 🟡 **PARTIAL** | Stationarity solved the *naive* way (drop raw prices); no fracdiff, no ADF |
| S3 | Triple-barrier labeling | 🟢 **ALIGNED** | TP/SL/timeout, ATR-scaled, binary meta-label — genuine triple barrier |
| S4 | Purged CV / Deflated Sharpe / overfitting | 🟡 **PARTIAL** | Purged+embargo CV built but NOT in decision gate; no Deflated Sharpe / PBO |
| S5 | Meta-labeling / bet sizing (Kelly) | 🟡 **PARTIAL** | Model trained offline, never wired live; Kelly path plumbed but gated off |
| S6 | Hierarchical Risk Parity | ⚪ **ABSENT (by design)** | No portfolio/covariance; irrelevant at $86 / one-shot-per-pair |
| — | Sample weights (uniqueness / time-decay) | 🟡 **PARTIAL** | Uniqueness offline only; not in deployed candidate; no time-decay |
| W | Wyckoff range→spring→reclaim→pullback→markup | 🟡 **PARTIAL** | All pieces exist but scattered; no range/box primitive; `sweep_choch` killed |
| B | Backtest discipline (stats/risk/journal) | 🟢 **ALIGNED** | Auto-stats + 1% risk + mature journal; overfitting rigor is the weak leg |

**Headline:** the bot is *closer to AFML than expected on labeling and infrastructure* (S3 aligned, purged-CV + uniqueness code already written) but the rigor lives in `scripts/` and never reaches the live loop or the decision-gating v0 model. The true conceptual voids are S1 (event bars) and S6 (HRP, but irrelevant now). The biggest *actionable* gaps are overfitting defenses (S4) and wiring already-built code into the gate (S4/S5/sample-weights).

---

## Findings

### S1 — Event bars: 🔴 ABSENT
- Exclusively time bars. `Candle` is OHLCV keyed by `timeframe`+`timestamp` (`shared/models.py:20-31`); ingestion subscribes to OKX clock channels `candle5m/15m/1H/4H` (`data_service/websocket_feeds.py:37-38`). Hard-coded clock TFs (`config/settings.py:115-121`).
- `grep dollar_bar|volume_bar|tick_bar|imbalance.bar` → **zero hits.** Volume is only a per-bar field used for ratios, never a sampling axis.
- **Gap / cheapest win:** a dollar-bar builder over `volume_quote` feeding the same `Candle` pipeline as an alternate sampling path. Big lift, foundational — defer.

### S2 — Fractional differentiation: 🟡 PARTIAL
- Features are mostly **stationary by construction** (pct-distances, ratios, z-scores): `risk_distance_pct`, `atr_pct`, `oi_delta_pct`, `book_imbalance_ratio` (`shared/ml_features.py:60,256,164,431`). Raw levels (prices, OI, CVD) are logged but **dropped at training** as "Non-stationary absolute prices (AFML Ch.5)" (`scripts/feature_importance.py:404-414`). Daily vol = EWMA std of log-returns, cites AFML Ch.3 (`ml_features.py:516-553`).
- **This is the "lose long-term memory" horn AFML's fracdiff exists to avoid.** No fracdiff anywhere (`grep frac` → only comment strings); no ADF / stationarity test in code or training.
- **Gap:** fracdiff transform on price/OI with ADF-driven minimal `d*`, replacing the all-or-nothing drop-raw rule. Medium lift; only worth it once N supports more features.

### S3 — Triple-barrier labeling: 🟢 ALIGNED
- Genuine three-barrier resolution. TP barrier `shared/pnl_engine.py:136`, SL `:137`, same-candle tie → conservative SL/BE `:139-142`. Vertical barrier = `shadow_timeout` at `SHADOW_TRADE_TIMEOUT_HOURS=12h` (`execution_service/shadow_monitor.py:392-396`, `settings.py:1000`).
- **Barriers are ATR-scaled:** engine1 SL is structural-or-ATR-floored (`engines/trend_pullback.py:279-294`, `SL_ATR_FLOOR_MULT=1.0`); TP derives from `risk=entry−SL` so profit barrier inherits the vol scaling — matches AFML `trgt` design.
- **Meta-label exists:** `scripts/ml_v0_engine1.py:133` builds binary "did the bet make money" (`y = outcome=='shadow_tp'`), separate from the deterministic directional engine. Correct AFML split.
- **Gap:** the vertical barrier is a **fixed 12h wall-clock**, not a per-setup expected-holding derived from the ATR target; and timeout outcomes are **dropped** from labeling (`ml_v0_engine1.py:13`) instead of labeled by sign of return — discards path-dependent samples. Cheap fix, real ML-quality lever.

### S4 — Purged CV / Deflated Sharpe: 🟡 PARTIAL
- **The hard part is built and faithful:** `scripts/feature_importance.py:46` `PurgedKFoldCV` with embargo (`:88-91`), 3-overlap purge citing AFML Snippet 7.1 (`:93-101`), uniqueness sample weights Ch.4 (`:473-482`), MDA neg_log_loss (`:215`).
- **But the decision-gating v0 model does NOT use it:** `scripts/ml_v0_engine1.py:157-165` is a single time-sorted 80/20 holdout — no purge/embargo. Result: train AUC 1.0 / test 0.70, overfit gap flagged (`docs/audits/ml-v0-engine1-2026-05-25.md:24-31`).
- **No Deflated/Probabilistic Sharpe, no PBO, no multiple-testing haircut** anywhere (`grep` → zero). `scripts/optimize.py:162-214` sweeps 50-100 Optuna trials, picks best test-Sharpe with **no trials adjustment** — the exact "test thousands of combos → overfit by chance" trap the notes quote. Bootstrap CI (`backtest_bootstrap.py:95`) + window stability exist but are resampling, not walk-forward holdout or trials-deflation.
- **Gaps (highest-value of the whole audit):** (a) add Deflated Sharpe / trials count to `optimize.py` + `TRACKER.md`; (b) swap the naive holdout in `ml_v0_engine1.py` for the already-built `PurgedKFoldCV` once N grows (the script itself promises this at `:26`).

### S5 — Meta-labeling / bet sizing: 🟡 PARTIAL
- Architecture is AFML-correct but **nothing is wired live.** Roadmap (`docs/audits/ai-service-audit-2026-03-18.md:54-78`): Phase 1 (feature importance) done; Phases 2-4 (deploy classifier, Kelly sizing, sample weights) not done. No model loaded at runtime (`grep joblib.load|predict` in non-script code → zero).
- AI filter confirmed bypassed (`ENABLED_SETUPS=[]`, synthetic `AIDecision(confidence=1.0)` at `main.py:359-368`).
- Sizing rule-based: `size=(capital×risk_pct)/|entry−sl|`, fixed `RISK_PER_TRADE=0.01`. **A Half-Kelly path is plumbed** (`risk_service/service.py:169-184`) but gated off (`BET_SIZING_ENABLED=false`, `settings.py:822`) AND only fires when `ai_confidence<1.0` — never, since confidence is hardcoded 1.0.
- ML features are **collected, not consumed** (`main.py:484,512`; no read-back).
- **Gap:** Phase-2 deployment (load trained model → replace `confidence=1.0` with calibrated P) is the single change that activates everything already built. **But:** AUC 0.70 at N=148 / 0.26 overfit gap is NOT calibrated enough for Kelly yet. Blocked on the 6/8 re-run.

### S6 — HRP: ⚪ ABSENT (by design)
- No portfolio/covariance/clustering (`grep hrp|markowitz|covariance|portfolio_weight` → zero). `_btc_correlation` exists only as an ML input feature (`data_service/context_service.py:296`), not for allocation.
- One-position-per-pair, flat risk; cross-pair coupling = two scalar caps only: `MAX_OPEN_POSITIONS=8`, `MAX_PORTFOLIO_HEAT_PCT=0.06` (`risk_service/service.py:248-268`). No correlation/cluster cap — 7 correlated alts can all open at once.
- **Verdict:** irrelevant at $86 / one-shot-per-pair. Only a lightweight crypto-beta/cluster heat cap is worth considering, and only post-scaling (>$1k). Park.

### Sample weights: 🟡 PARTIAL
- Uniqueness `1/(1+concurrent)` exists offline (`feature_importance.py:118-187`) but the deployed candidate `ml_v0_engine1.py:16` uses only `class_weight=balanced`. No time-decay anywhere.
- **Gap:** wire `compute_sample_uniqueness` into `ml_v0_engine1.py`. Matters because engine1's clustered-impulse history (MEMORY: dedup gap) makes overlapping labels a real bias — and this directly affects the AUC the S5 go/no-go leans on. Cheap; function already exists.

### Wyckoff: 🟡 PARTIAL — 5-step coverage map
| Step | Coverage | Evidence |
|---|---|---|
| 1. Range created | 🔴 **ABSENT as primitive** — range exists only as premium/discount bias frame, never a detected consolidation box | `strategy_service/liquidity.py:48-54`; `trade_classifier.py:224` |
| 2. Range swept (spring) | 🟡 PARTIAL — sweep = wick pierce + close back, but anchored to EQH/EQL, not a range low | `liquidity.py:286,311-347` |
| 3. Range broken w/ CLOSURE | 🟢 ALIGNED (general BOS) — requires candle close beyond level, wick-only rejected; but breaks a *swing*, not a *range boundary* | `market_structure.py:170,204,232`; `settings.py:211` (`BOS_CONFIRMATION_PCT=0.001`) |
| 4. Pullback to buy | 🟢 ALIGNED (engine1) — impulse→pullback→Fib-50 limit, SL beyond impulse origin; trend-generic, not range-anchored | `engines/trend_pullback.py:234-294` |
| 5. Markup | ⚪ N/A — TP/exit logic | `compute_tp:297-356` |
- Closest single chained detector = `scalp_sweep_choch_v1` (range(20-bar)→sweep→close-back-inside→confirm, `scalp_setups.py:240-310`) — structurally the Wyckoff spring, **but KILLED** (MEMORY: "sweep_choch kill"). Setup A is nearest swing assembly but sweep is wick-only off EQH/EQL, no "close back above range high" gate.
- **Gap:** no **consolidation-box primitive.** Without it "spring" is a generic sweep and "range broken" a generic BOS. A box detector (then sweep-of-box-low + close-back-above-box) would chain the scattered pieces into a true accumulation detector and revive the killed `sweep_choch` on swing TF.

### Backtest discipline: 🟢 ALIGNED (overfitting = weak leg)
- Auto-stats: WR, PF, annualized Sharpe, expectancy, per-setup/pair/direction breakdowns, target-pass lines (`backtest.py:1268-1300,1326,1589-1637`). ✅
- Risk 1% per trade, matches notes' 0.5-1% (`RISK_PER_TRADE=0.01`, `settings.py:130`). ✅
- Journaling mature: Bybit journal (watcher + mobile annotation + weekly review) + `/chart` replay with OB/FVG overlay. ✅
- Overfitting: `optimize.py` walk-forward 70/30 + `TRACKER.md` overfitting notes, but **no purged CV, no Deflated Sharpe, no cumulative trial count** → see S4.

---

## Recommendation (sequencing — consistent with the 6/8 freeze)

Same posture as the inducement audit: **instrument/fix infra, don't churn ML features mid-baseline.** Ranked by value × independence from the v18→v19 freeze:

**Tier 1 — safe now, no feature-version churn, high value**
1. ✅ **DONE (PR #62, 2026-06-02)** — **S4a Deflated Sharpe + trial count** in `optimize.py` / `TRACKER.md`. Pure backtest-tooling, directly attacks the notes' #1 pitfall (overfitting). No live/feature impact. Also fixed a latent harness bug (`BacktestDataService.get_orderbook_depth` missing → setup-A backtests crashed).
2. **S3 vertical-barrier fix:** stop dropping timeout outcomes — label by sign of return; consider per-setup horizon vs fixed 12h. Improves the labeling that the 6/8 decision rests on.
3. **Sample-weights wiring:** plug existing `compute_sample_uniqueness` into `ml_v0_engine1.py`. Improves AUC honesty before the go/no-go.

**Tier 1b — Data service / `ml_setups` schema (infra only, no `ML_FEATURE_VERSION` bump)**

Post–data-service review (2026-06-04). Goal: make the dataset AFML-ready for purged CV, meta-labeling, and reproducible training queries without changing detector semantics. Safe during the 6/8 freeze if limited to nullable columns + writers + SQL helpers (allowed: `data_service/` bugfixes/infra per SYSTEM_BASELINE §9).

| # | Item | Why (AFML) | Files | Status |
|---|------|------------|-------|--------|
| 1b.1 | **Barrier snapshot at insert** — persist `barrier_upper_r`, `barrier_lower_r`, `vertical_barrier_hours` (and reuse existing `daily_vol` as vol scale at detection). Derive from setup geometry + `SHADOW_TRADE_TIMEOUT_HOURS` / per-setup `time_stop_seconds` when present. | Triple-barrier labels (Ch.3) should be explicit in the row, not re-derived offline from prices. | `data_store.py` (migration + `insert_ml_setup`), `main.py` `_ml_log_setup` or `shared/ml_features.py` | Pending |
| 1b.2 | **Canonical label window** — `label_start_ms` (= `setup.timestamp` or first fill ts), `label_end_ms` (= resolution **candle** `timestamp`, not `resolved_at = NOW()`). `shadow_resolve_candle_ts` (v17) is close; formalize as the purge/uniqueness end time. | Purged k-fold + sample uniqueness (Ch.4, Ch.7) need observation start and holding-period end on market time. | `update_ml_setup_outcome` in `data_store.py`, `shadow_monitor.py` `_resolve` | Pending |
| 1b.3 | **`ml_training_rows()` helper** — single SQL/view: `feature_version >= 4`, `ml_market_outcome_filter_sql()`, `outcome_type IS NOT NULL`, optional `experiment_id` / `setup_type`; document stationary feature whitelist (same set as `feature_importance.py` drops). | One import path for training scripts — avoids ad-hoc queries that leak absolute prices or non-market outcomes. | `data_store.py` (Python helper + optional SQL view), `scripts/feature_importance.py`, `scripts/ml_v0_engine1.py` | Pending |
| 1b.4 | **Outcome-only path-dependent columns** — write `guardian_shadow_*` and optional `risk_daily_dd_pct` / `risk_weekly_dd_pct` on **outcome update**, not on feature insert (or duplicate: NULL at insert, fill at resolve). | Ch.3 meta-label features must be known at bet time; guardian flags are path-dependent (Ch.8 importance only). | `insert_ml_setup` / `update_ml_setup_outcome`, `main.py` `_ml_log_setup` | Pending |

**Already aligned (no Tier 1b work):** confirmed-candle-only pipeline; `VALID_OUTCOMES` / `NON_MARKET_OUTCOMES`; `daily_vol` (v6); PG candle history with `volume_quote` (future dollar bars); CVD `None` when invalid; `created_at` + `resolved_at` for offline purge in `feature_importance.py`.

**Explicitly out of Tier 1b (Tier 3 / post-edge):** dollar-bar builder (S1), fracdiff (S2), live meta-label deploy (S5).

**Tier 2 — after 2026-06-08 re-run (touches the model / gate)**
4. **S4b** swap naive 80/20 holdout → built `PurgedKFoldCV` in the v0 gate once N grows.
5. **S5** Phase-2 deploy: load model, replace `confidence=1.0` with calibrated P → unlocks the already-plumbed Kelly path. Blocked on AUC being trustworthy.

**Tier 3 — foundational / scale-gated (park)**
6. **S1** dollar-bar sampling layer (big lift).
7. **S2** fracdiff + ADF (only once feature budget supports it).
8. **W** consolidation-box primitive → revive `sweep_choch` on swing TF (SMC-side, pairs with the deferred inducement work).
9. **S6** correlation/cluster heat cap — only at >$1k capital.

**Next step:** Tier-1 #1 (S4a) shipped in PR #62. Tier-1 #2 (timeout labels) + #3 (sample-uniqueness) are deferred behind the **2026-06-08** v0 re-run to keep that longitudinal AUC read method-stable — re-evaluate after the Engine-2 verdict is recorded. **Tier-1b** (schema/helpers) can ship in parallel with #2/#3 if it does not bump `ML_FEATURE_VERSION`; otherwise bundle 1b.1–1b.2 with the first post-6/8 code week. **Engine One decision (2026-06-08):** `python scripts/ml_v0_engine1.py` — see `docs/SYSTEM_BASELINE.md` §8 (2026-05-11). If AUC test ≥ 0.60 → Tier-2 + `docs/audits/ai-service-audit-2026-03-18.md` Phase 2–4; if ≤ 0.55 → Engine 2, skip meta-label deploy. Tier-2/3 stay parked behind the same freeze alongside the inducement plan (`docs/plans/smc-inducement-pullback-fixes-2026-06-01.md`).
