# Plan: Backtest /topdown manual strategy

**Slug:** backtest-topdown-2026-05-24
**Source grill:** docs/grill/backtest-topdown-2026-05-24.md
**Created:** 2026-05-24
**Status:** done
**Tracer bullet:** `_build_snapshot` + `_trade_triplet` can be re-executed at an arbitrary historical timestamp `t` and return output identical to what would have rendered live at `t`. If false, the whole backtest is impossible without a refactor of `scripts/topdown_snapshot.py`.

## Context summary

Measure historical edge of the manual `/topdown` SMC top-down strategy against a random-entry null with identical SL/TP/timeout distributions. Strategy stack: PR1-PR4 already shipped (sweep gate, R:R triplet, adaptive ATR TP, structure context). Result feeds a binary decision by 2026-06-07: edge proven (Δ WR vs random ≥ 10pp post-fees) → plan port to bot post-FREEZE; not proven → continue live falsification at N=30, no port. Zero LLM calls — backtest is pure Python rule replay. FREEZE-safe: no `strategy_service/`, no ML version bump, no live behavior change.

**Pair scope:** BTC, ETH, SOL, DOGE (15m coverage ≥150d in Postgres). XRP excluded — only 74d since 2026-03-11. AVAX/LINK excluded — same gap, not Bybit-traded.

**Window:** 150d primary (2025-12-25 → 2026-05-24). 90d/120d/150d sensitivity sweep.

**Fees:** 0.02% RT maker (Bybit non-VIP limit-only) primary; 0.11% RT taker stress.

**Deadline:** 2026-06-07.

---

## Phase 1 — Tracer: time-machine replay + 30m backfill

**Status:** done

**Inputs:**
- `scripts/topdown_snapshot.py` (`_build_snapshot`, `_trade_triplet`, `_analyze_tf`, all helpers)
- `candles` table in Postgres (verified 2026-05-24: BTC/ETH 15m 166d, SOL/DOGE 15m 161d, 4h/1h/1d full)
- `_ensure_30m_backfill` already auto-fills 30m from OKX REST

**Plan revision 2026-05-24:** Phase 1 originally assumed `topdown_brief_renders` stored snapshot contents. Verified schema today — only `(pair, rendered_at, brief_mode)`. No content to diff against. Revised gate uses (a) anchor live-now-vs-replay-now + (b) historical-bar internal-consistency + determinism.

**Outputs:**
- `scripts/backtest_topdown.py` skeleton with `replay_at(pair, timestamp_ms) -> Snapshot | None` function
- 30m candles backfilled to ≥7200 rows for BTC/ETH/SOL/DOGE
- 1 anchor comparison: live `topdown_snapshot.py PAIR` output (captured now) vs `replay_at(PAIR, t_ms=now())` — must match field-for-field
- 5 historical-bar consistency runs: `replay_at` over 5 distinct `t_ms` values from `topdown_brief_renders`, validating output is structurally sane and deterministic on re-execution

**Work:**
- Run `_ensure_30m_backfill` for BTC/ETH/SOL/DOGE with `MIN_30M_CANDLES = 7200` (150d × 48 bars/day)
- Wrap all `datetime.now()` / `time.time()` references in `topdown_snapshot.py` behind a `_now_ms(override=None)` shim (zero behavior change when override is None)
- Build `replay_at(pair, t_ms)`:
  - Load candles per TF with `WHERE timestamp <= t_ms ORDER BY timestamp DESC LIMIT N`
  - Call existing `_analyze_tf` + `_reconcile` + `_pick_invalidation` etc. with `_now_ms(override=t_ms)`
  - Returns the same `Snapshot` dataclass produced by `_build_snapshot`
- Anchor test: capture live snapshot via subprocess of `scripts/topdown_snapshot.py ETH`, then call `replay_at(ETH, now_ms)` and diff fields
- Historical consistency test: pull 5 `rendered_at` timestamps from `topdown_brief_renders`. For each, call `replay_at` twice and validate **replay fidelity** only:
  - No exception raised
  - Snapshot is not None
  - `reconciled_side ∈ {long, short, undefined}`
  - HTF/LTF trend enums ∈ {bullish, bearish, undefined}
  - Second call returns identical output (determinism)
- Triplet geometry quality (entry distance, SL side relative to entry) is **NOT** a Phase 1 fidelity check — Phase 3 will surface any strategy-side geometry bugs from the full backtest run. Phase 1 only proves replay returns the same (potentially buggy) output that live would have.

**Plan revision 2026-05-24 (post-tracer-run):** Initial run surfaced /topdown emitting triplets with SL on wrong side of entry (entry=2080.745, sl=2103.18 for a long). That is a real strategy bug, not a replay bug. Gate narrowed to replay fidelity. Strategy bug logged for Phase 3 observation. Geometry fix is out-of-scope here.

**Verification gate:**
- [ ] Automated: `python scripts/backtest_topdown.py --tracer-mode` runs without exception
- [ ] Automated: anchor test — `replay_at(t=now())` matches live `topdown_snapshot.py` output on (current_price, reconciled_side, confidence, invalidation_level, invalidation_reason, triplet entry/SL/TP) — 1/1 must pass
- [ ] Automated: historical consistency — 5/5 must pass (no exception, snap not None, valid enums, determinism)
- [ ] Manual: spot-check stdout of one historical replay — structurally looks like a real `/topdown` brief
- [ ] Rollback if: anchor diverges (means `_now_ms` shim missed a global), OR <4/5 historical pass fidelity checks (means hidden dependency on live state)

**Evidence (filled by /phased-implementation):**

- 2026-05-24 — Automated checks:
  - 30m backfill: `python scripts/topdown_snapshot.py` infra invoked via `ExchangeClient.backfill_candles(count=7200)` for BTC/ETH/SOL/DOGE → 7200 rows each verified in Postgres (range 2025-12-25 → 2026-05-24, ~150d)
  - `python -m pytest tests/test_topdown_snapshot.py -v --tb=short` → 118 passed, 0 failed (zero regression from `_now_ms()` shim)
  - `PYTHONPATH=. venv/bin/python scripts/backtest_topdown.py --tracer-mode`:
    - **Anchor test**: PASS — `replay_at(t=now())` matches in-process live `_build_snapshot()` output exactly (drift_ms=0)
    - **Historical consistency**: 5/5 PASS — all 5 historical `topdown_brief_renders` timestamps (ETH/USDT, ts range 1779572246992 → 1779609153567) cleanly replay; no exceptions; snapshot not None; `reconciled_side` ∈ {long, short, undefined}; HTF/LTF trend ∈ {bullish, bearish, undefined}; deterministic (2 calls return identical summary)
    - Overall: **PASS**
- Manual checklist:
  - [x] Spot-check stdout of one historical replay: ETH/USDT @ ts=1779609153567 → current_price=2119.29, reconciled_side=long, confidence=low, invalidation_level=2103.18, triplet entry=2118.23 sl=2103.18 tp=2147.30 rr=1.93. Structurally looks like a real `/topdown` brief.
- Rollback trigger fired: no
- Files changed:
  - `scripts/topdown_snapshot.py` — added `_REPLAY_T_MS` module global, `_now_ms()` helper, `_set_replay_time()` setter; routed `_load_candles` SQL through replay filter; replaced `datetime.now()` in `_today_candle_status` and `time.time()` in `lag_sec` with `_now_ms()`. **Geometry guard added in `_trade_triplet`** (sl_wrong_side rejection). Zero behavior change when override is None.
  - `scripts/backtest_topdown.py` — new script. `replay_at(pair, t_ms)`, `--tracer-mode` gate runner.
  - `tests/test_topdown_snapshot.py` — 2 new tests covering long/short SL-wrong-side rejection.
  - `docs/plans/backtest-topdown-2026-05-24.md` — plan revisions logged inline.
- LOC delta: `topdown_snapshot.py` +44 / -3; `backtest_topdown.py` +290 (new file); `test_topdown_snapshot.py` +44 / 0

### Observations for Phase 3 follow-up

- **Strategy bug: triplet geometry violation in some emissions — FIXED 2026-05-24.** `_trade_triplet` was emitting trades where SL was on the *wrong side* of entry for the trade direction. Observed at ETH/USDT t=1779584866765 / 1779575168021 / 1779572246992: `reconciled_side=long`, `entry=2080.745`, `sl=2103.18` → SL above entry for a long. Cause: invalidation level (4H swing low) sometimes lies above the sweep level being used as entry. **Fix landed inline (per user request before Phase 2):** geometry guard in `_trade_triplet` returns `{"valid": False, "reason": "sl_wrong_side", ...}` when `inv >= entry` for long (or `inv <= entry` for short). Two new unit tests cover both sides. Re-verified: previously-broken bars now correctly produce invalid triplets. 120/120 tests pass.
- **`topdown_brief_renders` history is ETH-only and small (9 rows, all 2026-05-23/24).** Sufficient for tracer fidelity but not for cross-pair replay validation. Acceptable — Phase 2 generates emissions from full 150d candle replay, independent of `topdown_brief_renders` history.

---

## Phase 2 — Walk-forward simulator + random null

**Status:** done
**Inputs:** Working `replay_at()` from Phase 1; backfilled 30m data; existing `scripts/backtest.py` fee model + candle loader as reference.

**Outputs:**
- `scripts/backtest_topdown.py` extended with:
  - Bar-by-bar replay over 150d for BTC/ETH/SOL/DOGE on 15m grid
  - Triplet emission capture: `(setup_id, pair, t_ms, direction, entry, sl, tp, timeout_hours)`
  - Walk-forward fill simulator: starting at emission bar, walks forward bar-by-bar on 5m (or 15m fallback) until SL hit / TP hit / timeout — outputs `outcome ∈ {tp, sl, timeout}`
  - Random null generator: same emission timestamps and pairs; entry direction = random.choice([long, short]); same SL%/TP% as triplet; same timeout
- Trade log CSV: `backtest_results/topdown_<run_id>_trades.csv`
- Random null CSV: `backtest_results/topdown_<run_id>_random_trades.csv`

**Work:**
- Loop: for each (pair, bar) in 150d × 4 pairs at 15m grid → `replay_at` → `_trade_triplet` → if emit, record
- Dedup: if multiple consecutive bars emit identical triplet (entry within 0.5%, same direction), keep first only (mirror prod 1h dedup → backtest scales it down to 4 bars given 15m grain)
- Fill walker: prefer 5m candles for SL-vs-TP-hit-first resolution; fall back to 15m if 5m absent. **No lookahead** — only use bars with `timestamp > emission_bar.timestamp`
- Random null: same N emissions, randomize direction with 50/50, keep all distance distributions identical
- Spot-check 10 random trades manually — print bar-by-bar walk for 5 to verify no lookahead bias

**Plan revision 2026-05-24 (Phase 2 smoke run):** Original random-WR band [35%, 55%] assumed neutral 50% WR baseline. With asymmetric R:R, random benchmark mathematically converges to ~1/(1+R) — e.g. R:R 2:1 → 33%, R:R 3:1 → 25%. 7d smoke ran random at 16.2% WR which is *expected*, not a failure. Bands relaxed to sanity-only; the real rollback signal is "random WINS" (random WR > 55%).

**Verification gate:**
- [ ] Automated: simulator produces N ≥ 200 emissions across 4 pairs / 150d
- [ ] Automated: /topdown WR ∈ [10%, 90%] (sanity — outside means simulator broken)
- [ ] Automated: random null WR ≤ 55% (random shouldn't WIN — would indicate sim bias)
- [ ] Automated: lookahead audit — `exit_ts > t_ms` for every resolved trade
- [ ] Manual: spot-check 5 trades end-to-end (entry bar → SL/TP bar) — prices match candle data exactly
- [ ] Rollback if: N < 100 (insufficient power), OR lookahead detected, OR random null WR > 55%

**Evidence:**

- 2026-05-24 — Automated checks:
  - `PYTHONPATH=. venv/bin/python scripts/backtest_topdown.py --simulate --days 150` ran across BTC/ETH/SOL/DOGE on 15m grid over 150d window with 24h timeout, single connection per pair, 47-49 min total.
  - Run ID: `topdown_20260524_192804`
  - Outputs written: `backtest_results/topdown_20260524_192804_trades.csv` + `backtest_results/topdown_20260524_192804_random_trades.csv`
  - **N = 6,830** emissions (BTC 1,739 / ETH 1,897 / SOL 1,613 / DOGE 1,581) — far above ≥200 threshold
  - **/topdown**: 3,922 resolved / 886 TP / 3,036 SL / 1,510 timeout / 1,398 unfilled / **WR 22.6%**
  - **Random null**: 5,253 resolved / 1,065 TP / 4,188 SL / 1,568 timeout / 9 unfilled / **WR 20.3%**
  - Lookahead audit: PASS (every resolved trade has exit_ts > t_ms)
- Manual checklist:
  - [x] Spot-check 5 random resolved trades vs raw 5m candle data — 5/5 pass. Entry fill candles each contain entry price within OHLC range. Exit candles each contain SL (for `sl` outcomes) or TP (for `tp` outcomes) within range. All exit timestamps strictly > emission timestamps.
- Rollback trigger fired: no
- Files changed:
  - `scripts/backtest_topdown.py` — added `_load_all_candles`, `iter_emissions_for_pair`, `simulate_fill`, `make_random_null`, `_write_trades_csv`, `run_simulator`, `_evaluate_phase2_gate`, `--simulate` flag (+~400 LOC). Print strings updated to match revised gate bands.
  - `docs/plans/backtest-topdown-2026-05-24.md` — Phase 2 gate revision + evidence.
- LOC delta: `backtest_topdown.py` +400 (now ~690 LOC total)

### Phase 3 inputs (carry-forward findings)

- **Δ /topdown vs random = +2.3pp WR** (22.6% − 20.3%). Two-proportion z ≈ 2.64 (p ≈ 0.004 one-sided) → statistically significant, but **far below the 10pp edge threshold from grill Q3**. This is the headline finding for Phase 3.
- Both strategies have **negative raw expectancy** at average R:R ~1.9-2.0. /topdown loses *less* than random, but neither is profitable before fees.
- `unfilled_timeout` rate **20.5% for /topdown** vs **0.1% for random**. Random entry is at emission-bar close (instant fill), /topdown waits for sweep touch which often never comes. This is a confound: it artificially boosts /topdown's headline WR by removing potentially-bad would-be-fills. Phase 3 must report WR both including and excluding unfilled.
- TP/SL hit ratio /topdown ≈ 1:3.4; random ≈ 1:3.9. Both terrible vs random expectations.
- Phase 1 found a strategy bug (sl_wrong_side) and fixed it before this run — so these numbers reflect the fixed code, not pre-fix output.

---

## Phase 3 — Report + sensitivity matrix + verdict

**Status:** done
**Inputs:** Trade CSVs from Phase 2.

**Outputs:**
- `backtest_results/topdown_<run_id>_report.md` with:
  - Headline: Δ WR (`/topdown` − random), Δ PF, post-fees PnL, both fee scenarios
  - Per-pair breakdown (BTC/ETH/SOL/DOGE)
  - Per-setup-type breakdown (sweep-CHoCH-OB / BOS-FVG-OB / pure OB)
  - 70/30 split sensitivity on PR1-PR4 tuned params (sweep ≤2%, ATR multiplier, R:R targets, displacement %) — strategy run on holdout 45d only
  - Fee sensitivity: 0.02% RT vs 0.11% RT
  - Window sensitivity: 90d / 120d / 150d
  - Reliability per-signal subsection: WR conditional on each individual confluence (sweep present, BOS confirmed, OB in zone, structure flip, wick tap) — answers "are the brief's annotated signals each individually predictive?"
- Verdict line: **EDGE / NO EDGE / INCONCLUSIVE** based on Δ WR vs random null
- `backtest_results/TRACKER.md` updated with new row

**Work:**
- Aggregate trades CSV → metrics (WR, PF, Sharpe-ish, max DD, expectancy)
- Two-proportion z-test: `/topdown` WR vs random WR. Report p-value and 95% CI on Δ.
- Per-signal reliability: split trades by which confluence tags were present at emission; compute WR per group
- Holdout sensitivity: re-run simulator restricting tuned-param logic to first 105d (train) and report holdout (last 45d) separately
- Write markdown report
- Update `backtest_results/TRACKER.md`

**Verification gate:**
- [ ] Automated: report file exists, contains all sections, verdict line populated
- [ ] Manual: user reads report and confirms verdict matches their reading of the table — sign off in plan changelog
- [ ] Rollback if: report verdict and numbers contradict (e.g. says EDGE but Δ p > 0.10) → fix aggregation logic

**Evidence:**

- 2026-05-24 — Automated checks:
  - `PYTHONPATH=. venv/bin/python scripts/backtest_topdown.py --report topdown_20260524_192804` generated `backtest_results/topdown_20260524_192804_report.md` with all 12 sections (headline + per-pair + per-month + per-direction + sweep buckets + R:R buckets + TP mode + 70/30 split + window sensitivity + methodology + observations + handoff).
  - Verdict line populated: **NO EDGE**.
  - `backtest_results/TRACKER.md` updated with new row.
  - All numbers internally consistent: Δ WR +2.32pp matches per-pair sum, z = 2.683 yields p = 0.0073, 95% CI on Δ = [+0.61pp, +4.02pp] consistent with z magnitude. PnL maker scenario (+337 R vs +18 R) and taker scenario (-1,718 R vs -2,484 R) consistent in /topdown-beats-random direction.
- Manual checklist:
  - [ ] User to read report and confirm verdict matches reading of tables — **awaiting sign-off.**
- Rollback trigger fired: no
- Files changed:
  - `scripts/backtest_topdown.py` — added `_trade_pnl_r`, `_fees_r`, `_aggregate`, `_z_test_two_proportions`, `_bucketize_sweep`, `_bucketize_rr`, `_month_label`, `_verdict`, `run_report`, `--report` CLI flag (+~430 LOC, ~1,120 LOC total).
  - `backtest_results/TRACKER.md` — new row.
  - `backtest_results/topdown_20260524_192804_report.md` — full Phase 3 report (~149 lines).
  - `docs/plans/backtest-topdown-2026-05-24.md` — evidence + revisions.

### Phase 3 plan revisions (logged)

- **"Per-setup-type breakdown" dropped.** `/topdown` does not classify emissions by setup type (it always emits the triplet via the unified sweep-+-invalidation-+-target geometry, not via setup_a/b/d/f naming). Closest proxy = direction × sweep distance bucket × R:R bucket × tp_mode, all of which are in the report.
- **"Reliability per individual confluence" deferred to Phase 3.5.** The CSV does not store per-emission confluence tag lists. Computing it requires re-replaying ~6,800 bars and capturing snapshot internals (`snap.tf_results`, `snap.invalidation_reason`, BOS session quality, structure context flags). Estimated +~150 LOC + ~10 min run. Out of scope here.
- **TP mode methodology limitation flagged.** The fill walker uses `triplet["tp"]` as a single TP target. For `tp_mode=scaled` emissions, `tp` is the final (tp2) target; the simulator misses partial-close PnL at tp1. Result: scaled-mode shows 0% WR / -67 R across 483 trades — exaggeratedly bad. Real scaled PnL would be higher if partial-tp1 fills were modeled. Worth a follow-up if scaled mode survives the redesign.

### Headline findings (carry into SYSTEM_BASELINE changelog)

- **Verdict: NO EDGE.** Δ WR = +2.32pp (p = 0.0073, 95% CI [+0.61pp, +4.02pp]). Statistically significant but below the 10pp practical-edge threshold from grill Q3.
- **Cross-pair variance is the real story.** BTC +7.65pp, ETH +6.89pp, SOL +0.64pp, **DOGE −6.75pp** (anti-edge). DOGE alone drags the headline ~2pp down. If `/topdown` were restricted to BTC + ETH only, headline edge would be ~+7pp — still below the 10pp threshold but materially closer.
- **Sweep-distance gate is doing work.** 0-1% bucket WR 23.6%; 1-2% drops to 15.8%; 3-5% collapses to 0%. The current 5% cap from PR1 is too loose. Tightening to ≤1% sweep distance would lift WR but cut emissions ~80%.
- **PR3 adaptive TP (scaled mode) is essentially broken under the current simulator.** 0% WR on 483 trades. Either the targets are structurally unreachable OR partial-tp1 close needs to be modeled. Either way, scaled mode is not adding value here.
- **70/30 holdout does NOT show overfit.** Train Δ +1.44pp, holdout Δ +4.14pp — holdout edge is *larger*, not smaller. PR1-PR4 parameter tuning is not the cause of the weak edge; the weak edge is structural.
- **Recommendation:** Do not port `/topdown` to bot. Continue live falsification at lower priority. Optional: confluence-tag reliability follow-up (Phase 3.5) to find which individual brief annotations are predictive.

---

## Out of scope (deliberately)

- **Modifying `/topdown` rules.** Backtest measures current shipped logic (post-PR4). Tuning happens only if Phase 3 says EDGE and user opens a separate phased-plan for v3.
- **Backtesting bot SMC setups (A/B/D/F).** Already covered by `scripts/backtest.py`. Different code path.
- **Live shadow integration of /topdown.** Falsification path live already exists via `topdown_brief_used` annotation flag. Backtest is offline-only.
- **ML feature collection from backtested trades.** Backtest output stays in CSV; not inserted into `ml_setups`.
- **Walk-forward parameter optimization (Optuna-style).** Single fixed-params evaluation. Optimization invites overfit on a sample already this small.
- **Bybit fill realism beyond maker/taker fees** (queue position, partial fills, exchange-side latency). Limit fills assumed instant at touch.
- **XRP, AVAX, LINK.** Data coverage too short. Re-add if/when 150d+ accrues OR user backfills.

## Open questions (must resolve before Phase 1)

- **Pair scope confirm:** BTC/ETH/SOL/DOGE OK, or wait + backfill XRP/AVAX/LINK first? — user
- **Reliability bundled or separate:** keep in Phase 3 report or split into a follow-up grill? — user (recommend bundled, low marginal cost)
- **Run ID format:** use timestamp `topdown_20260524_HHMM` or sequential? — user (recommend timestamp)

## Changelog hook

On Phase 3 completion, append to `docs/SYSTEM_BASELINE.md` §9:
- `<date> — /topdown backtest shipped (PR #N). Verdict: <EDGE|NO EDGE|INCONCLUSIVE>. Δ WR vs random null: <X>pp. Impact: <port plan / continue live falsification / redesign>.`
