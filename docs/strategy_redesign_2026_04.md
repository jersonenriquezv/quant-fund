# Strategy Redesign — 2026-04-27

> Status: **proposal, not implementation**. Bot remains shadow-only throughout this document. No changes ship from this file alone — each section names follow-up PRs explicitly.
> Author context: critical review of SMC strategy under shadow mode. Anti-bias posture. Author is not consistently profitable manually; no SMC dogma is taken as ground truth.
> Scope priority (per request): §1 diagnosis, §3 per-setup decisions, §4 hypotheses, §7 plan, §9 promotion criteria. §5/§6/§8/§10 expand follow-up.

---

## 0. TL;DR

1. **The current shadow dataset cannot answer the strategy question.** Experiment `batch1_tp1_rr_1_3_2026_04_20` is 6 days old (started 2026-04-21), not 14 — the user's mental model is stale. It has 11 market-resolved outcomes (TP/SL/BE) plus 2 no_fill across all setups, with 10 of the 11 resolved on ETH. Every per-setup, per-pair, per-session number anyone has cited is a hypothesis, not a finding.
2. **Setups A and B are functionally dead under current gates.** Setup A: 59 detections, 54 direction-filtered, 0 resolved. Setup B: 41 detections, 35 dedup, 2 no_fill, 0 directional outcomes. Setup F: n=1 (a loss). Setup D_choch: n=8, the only thing actually generating data.
3. **The setups are SMC checklists, not hypotheses.** Each is a conjunction of indicator conditions ("if BOS and OB and FVG and PD…") with no thesis about why the next N candles should move favorably. This is what the user already suspects and the data is consistent with that read.
4. **The infrastructure is sound and worth keeping.** Data, risk, execution, shadow monitor, ML logger, backtester analytics, audit pipeline — none of these are the bottleneck. The bottleneck is in `strategy_service/`.
5. **Recommendation: pause adding ML features and tuning thresholds. Redesign three signal modules from explicit hypotheses, run them in parallel shadow tracks against simple benchmarks, and re-evaluate in 6–10 weeks.** No live trading until both promotion gates and ML gate G1–G6 (SYSTEM_BASELINE §7.1) clear independently.
6. **Bottom line on time-to-live: realistically 3–6 months of shadow before any setup deserves $86 of real capital, and longer before the $4.6k Bybit pool is involved.** Anything faster is wishful.

---

## 1. Honest Diagnosis

### 1.1 What the data actually says (current experiment)

Experiment `batch1_tp1_rr_1_3_2026_04_20`, queried 2026-04-27 from `ml_setups` (feature_version ≥ 4). Exact SQL in §A.1.

"Resolved" below = market outcome counted toward edge: `shadow_tp`, `shadow_sl`, `shadow_breakeven`. `shadow_no_fill` is reported as a separate column because it does *not* test the directional thesis — entry never triggered — and should not be averaged into a WR.

| Slice | Detections | Resolved (TP/SL/BE) | no_fill | Dedup | Dir-filtered | Pending/orphan |
|---|---|---|---|---|---|---|
| **Total** | 114 | 11 | 2 | 42 | 54 | 5 |
| setup_a | 59 | 0 | 0 | 4 | 54 | 1 |
| setup_b | 41 | 0 | 2 | 35 | 0 | 4 |
| setup_d_choch | 8 | 8 (2 TP / 4 SL / 2 BE) | 0 | 0 | 0 | 0 |
| setup_d_bos | 4 | 2 (1 TP / 1 SL) | 0 | 2 | 0 | 0 |
| setup_f | 2 | 1 (SL) | 0 | 1 | 0 | 0 |
| **By pair (resolved + no_fill)** | | | | | | |
| BTC | 56 | 1 (setup_f SL) | 0 | 1 | 54 | 0 |
| ETH | 39 | 10 (3 TP / 5 SL / 2 BE — all d_bos / d_choch) | 1 (setup_b) | 27 | 0 | 1 |
| DOGE | 14 | 0 | 0 | 11 | 0 | 3 |
| SOL | 2 | 0 | 1 (setup_b) | 1 | 0 | 0 |
| XRP | 3 | 0 | 0 | 2 | 0 | 1 |
| LINK / AVAX | 0 | 0 | 0 | 0 | 0 | 0 |

Sums check: resolved 11 = (1 BTC) + (10 ETH). no_fill 2 = (1 ETH setup_b) + (1 SOL setup_b). Of the 11 resolved, **10/11 are on ETH**. Adding the 2 no_fills (which test entry geometry, not direction), ETH still owns 11/13 of all "engine activity" outcomes.

Key facts that override prior narrative:
- **Sample is 13, not 18, and concentrated on ETH d_choch.** Anything broken out further is single digits.
- **DOGE produces 0 resolved outcomes**, not "majority DOGE+ETH" as previously assumed. DOGE detections all hit the dedup wall.
- **BTC is silent.** 56 detections, 1 resolved trade (a setup_f SL). The shadow direction filter on setup_a blocks every BTC long candidate.
- **Setup B's dedup rate is ~85%** — same BOS gets re-detected for 1+ hour and the dedup cache eats every duplicate. The 2 detections that escaped dedup both no-filled.
- **Setup A's direction filter blocks 91% of detections.** What remains then dedups.

What this means:
- The W17 edge audit's slices (`docs/audits/edge-audit-2026-W17.md`) cover a 30-day window that **largely predates the current experiment**. Findings ("late-US ETH d_choch wins, BTC longs lose") are hypotheses to retest, not validated edge.
- "Days in shadow × pairs × timeframes" is not the right denominator. The right denominator is **resolved outcomes per setup per pair**, and on that basis only `setup_d_choch on ETH` has more than n=2.

### 1.2 What the bot is actually doing (vs. what docs claim)

The docs in `docs/context/02-strategy.md` and even `docs/SYSTEM_BASELINE.md` lag the code in several places:

| Source | Claim | Code reality | Severity |
|---|---|---|---|
| `02-strategy.md` §Setup A | `SETUP_A_ENTRY_PCT = 0.65` | `0.50` since 2026-04-02 (settings.py:294) | medium — material for fill rate |
| `02-strategy.md` §Setup A | `SETUP_A_MODE = "both"` default | `"continuation"` since 2026-04-02 (settings.py:302) | high — completely changes which trades fire |
| `02-strategy.md` §Setup F | `entry_distance ≤ 5%` | `0.025 (2.5%)` (settings.py:362) | medium |
| `02-strategy.md` §Setup A | `gap = 60 (Optuna 45)` | hard-coded 60 (settings.py:323) | low — but Optuna's validated value is not what's running |
| Tests `test_setups.py` | "B vs F equivalence", "F hardening" | These tests still pass but the live behavior of A/B/F has shifted under the cascade + structural TP since the tests were written | low |
| Edge audit § "Telemetry leak — sweep/funding/oi tiers null" | Not addressed in any baseline entry | Open — instrumentation gap, see §10 |
| `SYSTEM_BASELINE` §Hypotheses H5 | "If no trades 1–2w, try `HTF_BIAS_REQUIRE_4H=False`" | Already `False` (settings.py:402) — recommendation is stale | low |

These are not catastrophic, but they confirm a documentation/code drift pattern that erodes trust in any narrative built from the docs alone. The redesign effort should not start from `02-strategy.md`; it should start from the source code and the `ml_setups` table.

### 1.3 The structural problem

Each current setup is a conjunction of SMC features:
- Setup A: `sweep AND CHoCH AND OB AND PD aligned AND HTF aligned AND volume confirmation AND ATR ≥ X AND target space ≥ 1.4R AND entry distance ≤ Y AND OB score ≥ Z AND touch_count ≥ 3 AND CHoCH displacement ≥ 0.2%`.
- Setup B: same shape, swap sweep for FVG-OB adjacency.
- Setup F: same shape, drop FVG.

The conjunction is long. The hypothesis behind each conjunct is implicit. Several of these gates have been added or moved by audits without an accompanying expectancy claim — they exist because, on a previous backtest or shadow slice, removing them looked worse. That is a curve-fitting mode of operation, not hypothesis-driven design.

The deeper issue: **none of these setups answers the question "why should the next N candles move in our direction by ≥X% before they move against us by Y%?"** They answer "is this a recognizable SMC picture?". Those are different questions, and only the first has a tradeable expectancy claim attached.

This matches the user's read. It is not a doc-presentation issue; it is a design issue.

### 1.4 Why "more thresholds" is not the answer

A relaxation pass would:
- Lower `SETUP_A_MIN_SWEEP_TOUCH_COUNT` or `SETUP_A_MIN_CHOCH_DISPLACEMENT_PCT` → more A detections → more trades, but no reason to expect higher edge per trade. We have already lived through this loop (see SYSTEM_BASELINE changelog 2026-04-15 → 2026-04-16: "freeze v15 → shadow_tuning v16 because freeze was collecting garbage").
- Loosen `SETUP_B_MAX_ENTRY_DISTANCE_PCT` → more B fills, but the audit already shows entries that filled at >1% died (`setup_b entry_distance 0.0115/0.0199` both no_fill). The right fix is not "bigger window," it is "different entry rule."
- Drop dedup TTL → more shadow rows from the same underlying event, inflating sample and overstating independence. Worse than useless for ML.

The relaxation lever has been pulled multiple times in March/April. It produced more trades (97 in the 2026-03-10 aggressive 60d backtest) but fragile edge that disintegrated when AI was added (-$5,454 vs baseline). That fragility is the signature of conditional-on-narrow-window edge, not robust hypothesis.

---

## 2. Infrastructure to Keep

The user is right that the engineering is not the problem. Specifically:

**Keep, do not touch:**
- `data_service/*` — OKX WebSocket, Redis cache, Postgres persistence, ml_setups insert path, oi_liquidation_proxy, news, whale tracking. The data side is healthy.
- `risk_service/*` — guardrails (RISK_PER_TRADE, MAX_DAILY_DRAWDOWN, portfolio heat, MAX_SL_PCT, capital snapshot per trade). The 2026-04-23 audit fixes (capital_at_trade migration, refresh_capital_from_exchange, `risk tracker row match by opened_timestamp`) are exactly the kind of plumbing the redesign depends on.
- `execution_service/*` and `execution_service/shadow_monitor.py` — order lifecycle, SL/TP management, breakeven logic, the `shared/pnl_engine.py` extraction in Batch 0. Adopted-position SL recovery (audit fix #5) and bot+manual coexistence rule (`ALLOW_BOT_WITH_MANUAL=False`) close the worst residual heat-blindness.
- `shared/ml_features.py` and the v17 feature schema — 40-ish features, `experiment_id` column, VALID_OUTCOMES whitelist, `ml_market_outcome_filter_sql()`. We will reuse the table for the redesign; we will not rewrite it.
- Backtester (`scripts/backtest.py`) plus the new analytics scripts (`backtest_bootstrap.py`, `backtest_stability.py`, `backtest_regime_split.py`). Bootstrap CI + chronological stability split is the right toolkit. Walk-forward refactor remains deferred but is not blocking.
- Weekly edge audit pipeline (`scripts/weekly_edge_audit.py` → Opus 4.7) and pre-trade `/check` Telegram tool. These are observability, and they should keep running on every new experiment_id.

**Keep, but treat as logger only:**
- `ai_service/*` — the Claude filter is still bypassed and should stay bypassed. The 2026-03-18 audit's verdict (LLM-as-filter is wrong; replace with meta-labeling classifier) stands. The AFML roadmap (Phase 1 feature importance → Phase 2 meta-label → Phase 3 bet sizing) remains the right path **once we have a sample population large enough to be honest about**.

**Re-scope, but keep:**
- Volume Profile + Volume Profile OB-quality flags (`vp_poc_confluence`, `vp_hvn_confluence`, `vp_lvn_warning`) — useful as features for the redesigned engines, not as a load-bearing TP-snapping mechanism. (See §10.2.)

**Things the redesign will replace, not delete:**
- `strategy_service/setups.py` and `strategy_service/quick_setups.py` will get **new entry points beside the old ones**, not in-place edits. Old setups remain in code and SHADOW_MODE_SETUPS until the new ones beat them on the same population. No big-bang.
- `_apply_expectancy_filters` (ATR floor, target space) is a useful post-filter but currently the only behavior layer that resembles "is this even tradeable" — the redesign promotes this concept to a first-class regime filter (see §4.1).

---

## 3. Per-Setup Decisions

For each, an honest call. Justification first; severity-of-action second.

### 3.1 Setup A — Sweep + CHoCH + OB → **REDESIGN as "Failed Breakout / Sweep Reversal"**

- **Live data:** 0 resolved out of 59 in the current experiment. The direction filter (long disabled, BTC blocked entirely) makes the live behavior structurally different from any historical backtest.
- **Historical backtest:** 45% WR, slightly negative PnL on 60d aggressive baseline. But that backtest predates the cascade, structural TP, ATR SL floor 4.5×, MIN_RISK_REWARD 2.0, and SETUP_A_MODE="continuation". None of the historical numbers describe the current Setup A.
- **Hypothesis quality:** The hypothesis (sweep takes liquidity → CHoCH confirms reversal → enter on OB retest) is the only one of the four that comes close to a real microstructure thesis. The mechanism — trapped traders forced out — has independent academic support (Lo & MacKinlay reversal effect; mean-reversion after liquidity vacuums). It is not the problem.
- **The implementation** is the problem: stacked gates (touch ≥ 3, displacement ≥ 0.2%, OB age ≤ 84h, OB score ≥ 0.35, PD aligned, ATR ≥ 0.35%, target space ≥ 1.4R, plus SETUP_A_MODE="continuation") layered on top of a thesis that doesn't actually need them. Most of those gates were added to compensate for poor entry/exit, not to express the hypothesis.
- **Decision:** Redesign as Engine 2 (failed-breakout / sweep reversal — see §4.2). Strip the conjunction back to: a clear liquidity event, an objective rejection, an entry rule that doesn't require an OB at all. Keep the OB as an optional confluence feature for ML. Run the new module in parallel shadow alongside the legacy `setup_a` for at least 30 days before retiring the old one.

### 3.2 Setup B — BOS + FVG-OB → **FREEZE → KILL**

- **Live data:** 41 detections, 35 dedup, 2 no_fill (1 ETH, 1 SOL), 0 directional outcomes (no TP, no SL, no BE). The dedup rate alone (>85%) means the detector is firing on the same underlying BOS over and over, and the 12-candle BOS-age window is not solving it.
- **Historical:** 49% WR / +$3,647 in 60d aggressive baseline → 21.4% WR / -$1,028 with AI v1. That ±$5k swing on a 51-trade sample (small even by backtest standards) reads as "wide confidence interval, not robust edge."
- **Hypothesis quality:** "BOS happens, FVG forms adjacent to OB, price retests" is a chart pattern, not a hypothesis. There is no claim about *why* the retest should resolve favorably more than 50% of the time. The FVG-OB adjacency rule was added to rationalize observed wins; it is post-hoc, not pre-registered.
- **Decision:** Freeze (no further parameter tuning, no Optuna pass, no doc updates) for 30 days while Engine 1 (Trend-Pullback) runs in parallel — Engine 1 occupies the same niche but has a thesis. After 30 days: if Engine 1 produces ≥50 resolved outcomes and is at WR ≥45% / PF ≥1.3, **kill setup_b** entirely (remove from SHADOW_MODE_SETUPS; tombstone code with reference). If Engine 1 fails, revisit setup_b only as comparison baseline.

### 3.3 Setup F — Pure OB Retest → **REDESIGN as "Trend-Pullback / Impulse Retest"**

- **Live data:** 1 resolved (a BTC long SL, -1.74%, the audit's worst trade) out of 2. n=1.
- **Historical:** 58.8% WR / +$1,753 on 60d aggressive baseline. This is the strongest historical entry, but the same caveats apply (pre-cascade, pre-structural-TP).
- **Hypothesis quality:** Closest in spirit to a trend-continuation thesis, but written as "BOS + recent OB + OB score". The expansion side (the BOS itself) is treated as a confirmation rather than the core of the thesis. There is no explicit "expansion → controlled pullback → continuation" structure.
- **Decision:** Redesign as Engine 1 (Trend-Pullback / Impulse Retest — see §4.1). The new module will measure expansion quality (range vs. recent ATR, body decisiveness, displacement persistence) and pullback quality (depth, time, opposing-side absorption) as first-class quantities, with the OB as one possible entry-zone proxy among several. Run alongside legacy `setup_f` for 30 days minimum before retiring it.

### 3.4 Setup D_choch — LTF CHoCH Scalp → **KEEP, but quarantine**

- **Live data:** n=8, 25% WR, 2 BE, 4 SL. PF ~0.5 on this slice. All ETH. Audit notes the two TP wins are both ETH at 19–20 UTC, both with mixed PD/CVD alignment ("the gates did not add signal here").
- **Hypothesis quality:** "LTF reversal in HTF-aligned direction" is at least a thesis. The 5-minute timeframe makes sample accumulation realistic.
- **Decision:** Keep in shadow as-is. Do not change parameters. Quarantine to ETH+BTC only (drop SOL/DOGE/XRP/LINK/AVAX from this setup explicitly — they have not produced a single resolved outcome and absorb the dedup machinery). Aim to reach **n ≥ 50 resolved on ETH d_choch** before drawing any conclusion. At current rate (~1 resolved/day on ETH for this setup), that is ~7 weeks.

### 3.5 Setup D_bos — LTF BOS Scalp → **FREEZE alongside D_choch**

- **Live data:** n=2 (1 TP / 1 SL).
- **Hypothesis quality:** "Continuation after impulse on 5m" is a thin thesis when the impulse is a single 5m candle break.
- **Decision:** Freeze (no parameter changes). Allow data to accumulate alongside D_choch. If at n ≥ 30 D_bos is materially worse than D_choch (PF gap > 0.4 or WR gap > 10pp), kill it. Otherwise re-evaluate at the same n=50 point as D_choch.

### 3.6 Removed setups (C, E, G, H) — leave dead

These were correctly killed. Don't reanimate. The temptation to bring back Setup C ("Funding Squeeze") as its own setup should be resisted — the funding signal is more useful as a feature than as a primary trigger.

### 3.7 Decision summary

| Setup | Decision | Action this quarter |
|---|---|---|
| setup_a | **REDESIGN → Engine 2 (Failed Breakout)** | Build Engine 2 in parallel; retire setup_a after 30d if Engine 2 wins on n ≥ 50 |
| setup_b | **FREEZE → KILL** | No edits; remove after Engine 1 hits its bar |
| setup_f | **REDESIGN → Engine 1 (Trend-Pullback)** | Build Engine 1 in parallel; retire setup_f after 30d if Engine 1 wins |
| setup_d_choch | **KEEP, ETH+BTC only** | Quarantine pairs; ride to n ≥ 50 |
| setup_d_bos | **FREEZE** | No edits; ride to n ≥ 30 |
| setup_c/e/g/h | dead | leave dead |

---

## 4. New Hypotheses

Three engines, in priority order. Each has: (a) a market-state thesis, (b) entry, (c) invalidation, (d) target/risk, (e) features to record, (f) a benchmark, (g) a kill condition. None of these ship code now — this section is the design contract for the implementation phase.

Notation: timeframe is `15m` for Engine 1 and Engine 3, `5m` for Engine 2. All examples are notional; thresholds are starting points, not optimized. **Pairs: BTC and ETH only at first.** SOL added if and only if both engines pass their 30-day bar on BTC+ETH.

### 4.1 Engine 1 — Trend-Pullback / Impulse Retest

**Thesis.** When price has just made a strong directional impulse (statistically large body, on volume, persisting beyond the noise floor), and then retraces against that impulse without invalidating it (fails to reach the start of the impulse), the next move tends to be a continuation in the impulse direction. The mechanism is straightforward: participants who entered late on the impulse are still profitable through the pullback; those who tried to fade got punished; on the retest, late entrants and re-entries from the original direction add to the move. This is well-documented in trend-following literature (Asness/Moskowitz time-series momentum; cross-section pullback effect in equities and FX).

**Why this isn't just setup_f:** setup_f gates on "BOS happened" and "OB exists." Engine 1 gates on **measurable impulse persistence** and **measurable pullback control**, neither of which setup_f computes directly.

**Setup state required:**
- HTF bias (4H or 1H, per current logic) defined.
- Recent **impulse leg** identifiable on 15m: last K candles (K=3–8) where directional displacement > N × ATR(14), majority candles same direction, accumulated body-to-range ratio above threshold. This is a multi-bar definition, not a single-candle BOS.
- **Pullback** identifiable: subsequent retracement of M candles (M=2–6) in the opposite direction, **without** retracing past the impulse origin AND without strong opposite-side body domination.

**Entry condition.**
- Default: limit at the **38.2%–61.8% retracement of the impulse leg**, biased toward 50%, OR at the most recent 15m demand/supply zone (last opposite-color candle inside the pullback) — whichever comes first.
- Distance gate: entry must be ≤ 1.5 × ATR(14) from current price at the time of detection (this kills the "B entries 1–2% out → no_fill" leak directly).

**Invalidation (SL).**
- Hard: a close beyond the impulse origin.
- Soft (initial SL on exchange): max(ATR_SL_FLOOR_MULTIPLIER × ATR(14), distance to impulse origin × 1.05). Always wider than entry-zone wick noise.

**Target / risk.**
- TP1 at 1.0R as a partial exit (50%), trigger SL → entry only after **close** through TP1 (the BE_CONFIRM_CLOSES knob — already shipped, currently 0; for this engine should be 1).
- TP2 at the more conservative of: (a) 2R fixed, (b) nearest opposing 4H/1H swing **with ≥1.4R of clear room remaining after fees and slippage**.
- Min entry-to-TP2 R:R after fees: 1.6 (i.e., gross 1.8 minus ~0.2R for fees+slippage).

**Features to log (in addition to v17 schema):**
- impulse_atr_multiple (range / ATR), impulse_body_ratio, impulse_candle_count, impulse_displacement_pct
- pullback_depth_pct (of impulse), pullback_atr_multiple, pullback_candle_count, pullback_max_opposing_body
- entry_to_impulse_origin_pct, entry_to_TP2_pct
- regime tags from §4.4

**Benchmark.** A simple "in HTF bias direction, enter on every 15m close that pulls back 1×ATR from a 5-bar high (longs) / 5-bar low (shorts), SL = entry origin, TP = 2R" baseline. Run side-by-side. Engine 1 is only useful if it beats this baseline by ≥30% on PF and by ≥5pp on WR over n ≥ 50.

**Kill condition.** Engine 1 retired if, after 30 calendar days AND n ≥ 50 resolved on BTC+ETH:
- PF ≤ 1.0, OR
- WR < 40% AND average R < 1.4, OR
- the simple benchmark above beats Engine 1 on PF.

### 4.2 Engine 2 — Failed Breakout / Sweep Reversal

**Thesis.** When price runs a clearly identifiable liquidity pool (equal highs, range high, recent swing) and then fails to hold above/below it within a defined number of candles, the market has revealed unwilling buyers/sellers at that level. Failed breakouts have positive expectancy in equity-index futures (Lo, Mamaysky, Wang 2000 on technical pattern profitability; Hasbrouck on liquidity-driven reversals) and in crypto perp data where retail-style stop runs are common (the 2024–2025 funding-rate cycle literature documents this regime explicitly).

**Why this isn't just setup_a:** setup_a's thesis is the same in spirit, but the implementation has stacked gates (touch ≥ 3, displacement ≥ 0.2%, mode=continuation) that constrain the universe to setups where the *reversal direction is the HTF trend*. That removes the original mechanism — the failed-breakout edge is the trapped opposite-side, regardless of HTF trend.

**Setup state required:**
- Identifiable liquidity level: equal highs/lows ≥ 2 touches within the last 24h on 15m, OR a prior swing high/low from 1H/4H that has not been swept yet.
- **Sweep event**: 5m close beyond the level by < 0.15% and within 2 candles a 5m close back below/above the level (i.e., wick takes liquidity, body rejects).
- Volume on the sweep wick ≥ 1.5× the 20-bar average (kept low — the audit shows tier instrumentation is still null in many cases; we don't want to gate on volume tiers we can't measure yet).

**Entry condition.**
- Limit at the **midpoint of the rejection candle's body** OR at the level itself ± 0.1× ATR — whichever is closer to current price.
- Distance gate: ≤ 0.8 × ATR(14) from current price.
- HTF bias is **logged but not gated on**. This is deliberate. The mechanism does not need HTF alignment.

**Invalidation (SL).**
- A close beyond the sweep extreme by ≥ 0.05% (i.e., the rejection failed, the breakout was real). This is structurally tighter than current setup_a SLs and is the whole point.
- For Engine 2 v1 (this redesign): if structural SL would be tighter than the global `MIN_RISK_DISTANCE_PCT` floor, **widen the SL to the floor** (current behavior — same as legacy setups). This avoids a risk-model change in v1.
- **Future Engine 2 v2 (deferred, requires risk-model change):** keep the structural-SL tight and reduce position size proportionally so dollar risk stays at `RISK_PER_TRADE × capital`. This needs a new "risk distance for sizing" field in `RiskApproval` (separate from `sl_price`) and a corresponding change in `risk_service/PositionSizer`. Out of scope for the redesign sprint; tracked as a separate proposal once Engine 2 v1 has demonstrated edge.

**Target / risk.**
- TP1 at 1.0R as partial exit (50%), BE on close through.
- TP2 at the next opposing swing or VAH/VAL/POC depending on regime (range vs. trend), with min 1.5R after fees.
- Failed-breakout setups should have R:R ≥ 1.5 net of fees by construction; if structure forces less, skip.

**Features:**
- sweep_extension_pct (how far beyond level the wick went)
- rejection_body_ratio (rejection candle body / range)
- sweep_volume_atr_norm (volume × ATR normalization for cross-pair comparability)
- time_to_rejection_ms, candles_above_level
- `level_touches_24h`, `level_age_hours`
- distance to next opposing structural level

**Benchmark.** "Random short on every 5-bar high break that fails within 2 bars" (and the long mirror). If Engine 2 cannot beat this on PF, it is curve-fitting on rejection-body specifics.

**Kill condition.** Same template as Engine 1: 30d AND n ≥ 50; retire if PF ≤ 1.0, or WR < 40% with avg R < 1.3, or random-direction benchmark wins.

### 4.3 Engine 3 — Volatility Compression Breakout

**Thesis.** Sustained low realized-volatility regimes followed by an expansion candle / range break carry directional momentum. The compression provides a defined risk anchor (the range itself), and the expansion is the entry. This is the cleanest of the three theses to test: it has been measured to death in equities (NR7 patterns, Bollinger squeeze breakouts, NYSE volume-volatility breakouts) and is well-suited to crypto's low-liquidity overnight regimes.

**Setup state required:**
- 15m Bollinger Band width percentile (existing v16 feature `bb_squeeze_percentile`) below the 20th percentile of the trailing 30-day window — i.e., genuinely compressed.
- Range over the last K=10 candles within 2 × ATR(14).
- ADX(14) (existing v16 feature) below 20 during the compression — i.e., not a stalled trend, an actual range.

**Entry condition.**
- Stop or stop-limit on the close that *closes outside* the compressed range by ≥ 0.1% with a body-to-range ratio ≥ 0.6.
- Direction = direction of the breakout. **HTF bias is not gated on but logged.**
- Distance from breakout close to entry must be < 0.5 × ATR(14) (otherwise we are chasing).

**Invalidation (SL).**
- Inside the opposite end of the compressed range plus a small buffer (0.3 × ATR). The whole point: SL is structural, not arbitrary.

**Target / risk.**
- TP1 = 1.0R partial.
- TP2 = the historical range of the last expansion phase (use the 14-day rolling 95th percentile of expansion-phase ranges as a proxy), capped at the next 4H/1H swing.
- Compression breakouts often go fast or fail fast; min holding-time guardrail = one 15m candle close in favor before BE arms.

**Features:**
- `bb_squeeze_percentile`, `bb_width_pct`, `adx_14` at detection
- `compression_age_candles` (how long the squeeze has lasted)
- `breakout_body_ratio`, `breakout_volume_ratio`
- `range_at_compression_pct` (size of the box being broken)

**Benchmark.** "Long on any 15m close above the trailing 10-bar high during a sub-20-pct BB-width regime, SL inside the box, TP 2R." If Engine 3 only beats this benchmark on engineered details, it is overfit.

**Kill condition.** Same template. Engine 3 is the most disciplined of the three — if even this one cannot beat random momentum entries on the same compression filter, the user (and we) should treat that as serious negative evidence about the entire research direction and reset.

### 4.4 Pre-engine: Regime Filter

A regime filter runs *before* any engine considers a signal. This is the only common gate. Every engine receives the regime tags as features, but the filter only blocks setups when a regime is structurally hostile to all three.

**Inputs (all already in v17 features):**
- `volatility_regime_ratio` — current ATR vs. trailing 30-day ATR
- `adx_14` and `bb_squeeze` — trend vs. range vs. compression
- `trading_session` (Asia / EU / US) — categorical
- `funding_8h` and tier — directional crowding
- `btc_correlation_30m`, `btc_return_60m` — for ETH/altcoin context
- F&G score (kept as feature only, not gate, per freeze v15)

**Hard regime block (rejects ALL engines):**
- Volatility ratio outside [0.5, 3.0] — too dead or too crashing.
- F&G < 5 (systemic crisis, retained as the only F&G gate).
- Spread > 5 bps on the candle close (illiquidity).
- BTC return |60m| > 2.5% (cascade in progress; mean reversion of mean reversion is too noisy).

**Soft regime tag (logged, used by engines):**
- `regime_label ∈ {trend_strong, trend_weak, range, compression, breakout, hostile}` — derived from ADX + BBW + ATR ratio per a documented decision table.
- Engines refer to this rather than re-implementing their own regime logic.

This is a very intentional narrowing: instead of every setup carrying its own conjunction of session/volatility/funding gates, the regime filter handles the systemic ones once.

### 4.5 What this redesign deliberately does *not* try to do

- It does not try to pick "the best" setup. Three engines are kept because three independent failure modes are easier to diagnose than one combined failure.
- It does not attempt cross-pair/correlation trades, lead-lag (BTC → alts), pair-relative-strength, or basis trades. Those are real edges in crypto, but they need data infrastructure we don't have today (orderbook tick history, multi-exchange feeds) and would be a separate project.
- It does not attempt market-making, options, or anything requiring options IV / dealer-flow data. Out of scope.
- It does not introduce ML scoring as part of the gating decision yet — see §6 and SYSTEM_BASELINE §7.1.

---

## 5. Features to Measure

(Per request priority, brief — full schema in `shared/ml_features.py` already.)

**Reuse from v17, no schema change:** all 40+ features. Particularly: `atr_pct`, `daily_vol`, `volatility_regime_ratio`, `adx_14`, `plus_di_14`, `minus_di_14`, `bb_width_pct`, `bb_percent_b`, `bb_squeeze_percentile`, `bb_squeeze`, `wt_*`, `stoch_rsi_*`, `rsi_14`, `rsi_zone`, `rsi_divergence`, `cvd_*`, `oi_*`, `funding_*`, `btc_correlation`, `trading_session`, `pd_zone`, `pd_aligned` (strict).

**Add for the three engines (would bump ML_FEATURE_VERSION to 18):**
- Engine 1: `impulse_atr_multiple`, `impulse_body_ratio`, `impulse_candle_count`, `impulse_displacement_pct`, `pullback_depth_pct`, `pullback_atr_multiple`, `pullback_max_opposing_body_ratio`.
- Engine 2: `sweep_extension_pct`, `rejection_body_ratio`, `sweep_volume_atr_norm`, `level_touches_24h`, `level_age_hours`, `time_to_rejection_ms`.
- Engine 3: `compression_age_candles`, `breakout_body_ratio`, `range_at_compression_pct`.
- Common: `regime_label` (categorical from §4.4), `entry_atr_distance` (entry distance / ATR), `tp2_to_target_pct_after_fees`.

**Fix instrumentation gap** (per W17 audit): `sweep_tier`, `funding_tier`, `oi_rising_tier`, `has_oi_flush` are 100% null in the current dataset. Either the detector path doesn't write them on shadow paths, or no qualifying event is firing. Resolve before adding more columns. Could be a single one-line bug in `extract_setup_features`.

---

## 6. Benchmarks

For each engine, three benchmarks must be tracked side-by-side in shadow:

1. **Random-direction baseline** — same regime filter, same entry geometry, but direction chosen by deterministic pseudo-random function at signal time: `direction = "long" if hash(f"{pair}|{timestamp_ms}|{engine_id}|{experiment_id}") % 2 == 0 else "short"`. Use a stable hash (e.g. `hashlib.sha256` of the joined string, take first 8 bytes as int). Reproducible across reruns and audits. If the engine cannot beat this, it has no directional edge.
2. **Same-hour, same-direction baseline** — for Engine 1 only: in HTF-bias direction, enter every 15m close in the same hour that the engine fired, same SL/TP geometry. Tests whether the entry rule (the impulse + pullback structure) adds anything over "trade in HTF direction during this session."
3. **Single-feature baselines** — momentum (e.g., trend continuation on RSI > 60 + close > MA20), mean reversion (RSI < 30 + close < lower BB), breakout (close > 20-bar high). Each ~10 lines. The engine should not match any of these single-feature baselines too closely; if it does, the engine is just that single feature in a more expensive wrapper.

Benchmark trades are **logged as setup_type="benchmark_X" in `ml_setups` with their own experiment_id suffix** so they share resolution machinery with real setups. Bootstrap CI from `scripts/backtest_bootstrap.py` is the right tool to compare.

A signal engine that fails to beat its random-direction benchmark by a statistically meaningful margin (bootstrap-CI non-overlap on PF and PnL with N ≥ 50 in each arm) is killed without further ceremony.

---

## 7. Plan: 4–8 Weeks Shadow

This is the realistic version. The user explicitly requested honesty about whether 6 months is required; in §0 I said 3–6 months. The 4–8 week plan below is the "make Engine 1+2 ship-ready" phase. It does not promote anything to live. The full live-promotion path is in §9 and is longer.

**Week -1 to 0 (pre-work, ~3–5 days):**
- (a) ✅ Sync `docs/context/02-strategy.md` to current settings.py + Setup A gap 60→45 (commit `219b237` 2026-04-27).
- (d) ✅ `SHADOW_PAIR_FILTER` quarantine setup_d_* to BTC+ETH (commit `4e29052` 2026-04-27).
- (b) ✅ Fix tier extraction — funding_tier + oi_rising_tier from raw signal magnitude (commit `64026ed` 2026-04-27). Diagnosis showed sweep_tier was already correctly populated; only funding/oi were broken by gate-shaped parsing.
- (c) ✅ `regime_label` categorical added to `shared/ml_features.py`. v1 heuristic from ADX + BBW + ATR ratio + spread + btc-return + F&G. Migration 19. `ML_FEATURE_VERSION 17 → 18`. `EXPERIMENT_ID` → `redesign_pre_2026_04_27` (commit pending below).

**Week 0.5 — Pipeline plumbing (multi-signal emission), ~3–5 days. PREREQUISITE for any engine work. ✅ shipped 2026-04-27 (commit pending below).**
The current `strategy_service/service.py` returns a single `TradeSetup` per evaluate-call (`return setup` on first match — see e.g. `strategy_service/service.py:251`). That contract makes parallel-track shadow research impossible: legacy setup_f, Engine 1, the random-direction benchmark, and the momentum baseline cannot all observe the same candle and emit independently if the first match short-circuits the rest.

This phase ships the contract change. Specifically:
- New method `evaluate_all(pair, candle) -> list[TradeSetup]` next to `evaluate(...)`. Iterates legacy setups + engines + benchmarks; each emits 0 or 1 `TradeSetup` independently; `evaluate()` keeps its single-return contract for live execution path (`ENABLED_SETUPS`-gated only) by selecting the first live-eligible match from the list (or returning the first item, since live is empty).
- `main.py` shadow path consumes `evaluate_all()` and routes every emitted setup to the shadow monitor. Live path keeps consuming `evaluate()`.
- Dedup cache becomes per-`setup_type`, not global — so Engine 1 emitting on the same candle as legacy setup_f does not deduplicate either.
- Engines and benchmarks each carry their own `setup_type` string (e.g. `engine1_trend_pullback`, `bench_engine1_random_direction`). All flow through `ml_setups` under the same `experiment_id`.
- Tests: a real-candle integration test that asserts a single 15m candle can produce ≥3 emitted setups in `ml_setups` when the legacy + engine + benchmark conditions all match.

Without this phase, the §6 benchmark comparisons cannot share a population — each track would see a different subset of candles depending on detection ordering. This is the single biggest risk to the validity of the whole research program; do not skip.

**Weeks 1–2: Engine 1 (Trend-Pullback) shadow ship. ✅ shipped 2026-04-27.**
- Module: `strategy_service/engines/trend_pullback.py`. Does not edit `setups.py`.
- Setup type: `engine1_trend_pullback`. Registered in `SHADOW_MODE_SETUPS`.
- Pair scope: `SHADOW_PAIR_FILTER` restricts to `["BTC/USDT", "ETH/USDT"]`.
- Wired into `StrategyService._iterate_setups` after Setup G — co-emits via `evaluate_all()` alongside legacy setup_a/b/f. Live path unchanged.
- Owns its own gates (entry distance ≤ 1.5×ATR, target space ≥ 1.4R after fee buffer, net R:R ≥ 1.6) — does NOT inherit `_apply_expectancy_filters`.
- v1 thresholds documented as module-level constants (NOT optimized): impulse 3–8 candles ≥ 2× ATR, body ratio ≥ 0.55, directional ≥ 60%; pullback 2–6 candles, retrace 30–85%, max single opposing body ≤ 70% of pullback range.
- Tests: 36 unit + 1 multi-emit integration (TestEvaluateAll::test_engine1_co_emits_alongside_legacy).
- Benchmarks **shipped 2026-04-27** in `strategy_service/engines/benchmarks.py`:
  - `bench_engine1_random_direction` — sha256(pair|ts|engine_id|experiment_id) coin flip on direction; SL/TP mirrored across entry when flipped so R:R is preserved. Tests directional skill above noise.
  - `bench_engine1_market_now` — same direction as Engine 1 but entry at `current_price` (no pullback wait), SL/TP at the same R-multiples Engine 1 used. Tests whether the pullback-retest entry adds edge over an immediate market entry on HTF bias.
  Both benchmarks co-emit on every Engine 1 detection via `evaluate_all()`, are registered in `SHADOW_MODE_SETUPS` + `SHADOW_PAIR_FILTER` (BTC + ETH only), and produce parallel rows in `ml_setups` so bootstrap CI from `scripts/backtest_bootstrap.py` can compare WR / PF directly. Engine 1's API was unchanged — the benchmarks consume the emitted `TradeSetup` and reuse its geometry.
- Daily: monitor shadow_health Grafana dashboard. Weekly: run `weekly_edge_audit.py`.

**Weeks 3–4: Engine 2 (Failed Breakout) shadow ship.**
- Same shape: new `strategy_service/engines/failed_breakout.py`.
- Engine 2 may produce on the 5m timeframe; quarantine to BTC+ETH.
- Add benchmarks (random direction, single-feature mean-reversion).
- Continue legacy setup_a in shadow for direct comparison (it's already producing 0 outcomes, so this is no-cost).

**Weeks 5–6: Engine 3 (Compression Breakout) shadow ship.**
- New `strategy_service/engines/compression_breakout.py`.
- Add benchmarks (single-feature breakout).
- This engine is most likely to be the cleanest expectancy story; it is also most likely to be sample-starved (compression regimes are ~10–20% of the time).

**Weeks 7–8: Comparison + retirement decisions.**
- Bootstrap CI per-engine vs. per-benchmark on `ml_setups`.
- Stability split per engine (4 windows over the period).
- Per-engine vs. legacy setup retirement decisions: if Engine 1 ≥ legacy setup_f on PF and WR with bootstrap CI non-overlap → retire setup_f. If Engine 2 ≥ legacy setup_a → retire setup_a. Same for setup_b vs. Engine 1 (Engine 1 occupies setup_b's niche too).
- **Decision rule (separates "no edge" from "starved"):**
  - If engine has **n ≥ 50 resolved AND fails to beat its random-direction benchmark** (bootstrap CI overlap on PF, no advantage on PnL): **kill or redesign**. This is a real negative result.
  - If engine has **n < 50 resolved** by week 8: **sample-starved, not failed**. Decide one of: (i) extend the validation window by 4 more weeks, (ii) widen pair scope to SOL (small expansion only — not all 7), or (iii) declare the engine starved at this regime/capital and pause it. Do not kill on n<50 — that punishes the wrong cause.
  - If by week 8 **all three engines are sample-starved**, that is itself a finding: the regime filter + pair scope is too narrow to validate anything. Loosen one axis (likely the regime filter, which is a single piece of code, before touching the entry rules) and run another 4 weeks. Do not loosen entry thresholds — that is the relaxation trap.
- If by week 12 (extended) no engine has both n ≥ 50 AND beats its random-direction benchmark with statistical confidence: **stop, do not propose live**, return to design and ask whether the project has a viable hypothesis at all. This is a real possible outcome and should be planned for.

**Throughout:**
- No live trading. ENABLED_SETUPS stays `[]`.
- AI service stays bypassed. Bet sizing stays inert.
- Bybit manual ($4.6k) is unrelated to this and continues separately under the watcher / `/check` flow.
- The 1% risk-per-trade and 4.5× ATR SL floor are not touched.

**Estimated calendar time to "Engine ready for live consideration":** 6–10 weeks for the engine to exist and produce enough data; an additional 4–8 weeks to hit the n ≥ 200 promotion bar in §9; plus 2–4 weeks of small-size live trial after promotion. **Total realistic: 4–6 months from today.**

---

## 8. Files / Modules That Would Change in Future Phases

(Order of phases, not all at once.)

**Phase A — Pre-work (sync + instrumentation), ~1 week:**
- `docs/context/02-strategy.md` — sync with settings.py, mark stale.
- `shared/ml_features.py` — fix sweep_tier/funding_tier/oi_rising_tier nulls; add `regime_label` derivation; bump version.
- `data_service/data_store.py` — add new columns if tier fix requires; migration NN.
- `config/settings.py` — set new `EXPERIMENT_ID`, set `BE_CONFIRM_CLOSES=1` only inside the redesign experiment branch (not globally yet).
- `tests/test_ml_features.py` — regression for tier null fix.

**Phase A.5 — Multi-signal emission plumbing, ~3–5 days. PREREQUISITE for engines.**
- `strategy_service/service.py` — add `evaluate_all(pair, candle) -> list[TradeSetup]` next to `evaluate(...)`. Iterates all registered detectors; each emits 0 or 1 setup independently. `evaluate(...)` keeps single-return contract by selecting first live-eligible from the list.
- `strategy_service/service.py` — dedup cache becomes per-`setup_type` rather than global, so engines + benchmarks + legacy don't mutually deduplicate.
- `main.py` — shadow path consumes `evaluate_all()`, live path consumes `evaluate()`.
- `tests/test_strategy_service.py` — new test: real candle that satisfies legacy + engine + benchmark conditions emits ≥3 rows in `ml_setups`.
- No risk/execution changes. No live behavior change (ENABLED_SETUPS still empty).

**Phase B — Engine 1 (Trend-Pullback), ~2 weeks. Depends on Phase A.5.**
- `strategy_service/engines/__init__.py` (new package)
- `strategy_service/engines/trend_pullback.py` (new)
- `strategy_service/engines/benchmarks.py` (new — shared benchmark hooks, deterministic-seed random direction per §6.1)
- `strategy_service/service.py` — register engine + benchmarks in `evaluate_all()`
- `tests/test_engine_trend_pullback.py` (new — property-based + real candle slices)
- `docs/SYSTEM_BASELINE.md` §1 + §5 — register new shadow setup type, kill criteria, exit bar.

**Phase C — Engine 2 (Failed Breakout), ~2 weeks:**
- `strategy_service/engines/failed_breakout.py` (new)
- `tests/test_engine_failed_breakout.py`
- `strategy_service/service.py` — wire 5m path

**Phase D — Engine 3 (Compression Breakout), ~2 weeks:**
- `strategy_service/engines/compression_breakout.py`
- `tests/test_engine_compression_breakout.py`

**Phase E — Retirement (after Phase B–D show edge), ~1 week:**
- `config/settings.py` — drop retired setups from `SHADOW_MODE_SETUPS`
- `strategy_service/setups.py` / `quick_setups.py` — tombstone (do not delete code; mark as deprecated, exit early when called)
- `docs/context/02-strategy.md` — replace setup-by-setup descriptions with engine-by-engine

**Phase F — ML re-entry (only after promotion gates §9 + ML gate §7.1):**
- AFML Phase 1 (feature importance) on per-engine v18 dataset
- AFML Phase 2 (meta-label classifier per engine)
- `ai_service/*` — replace Claude with classifier; keep Claude as audit-only

What does **not** change at any phase: `risk_service/*`, `execution_service/*` (other than picking up the new setup_type strings), `data_service/*`, `shared/pnl_engine.py`, `scripts/backtest.py` (other than knowing about new setup types).

---

## 9. Promotion Criteria

These are the bars an engine must clear **before** entering live trading at $86 of OKX capital. Bybit ($4.6k) does not enter this discussion.

All criteria must hold simultaneously, computed on the engine's own resolved `ml_setups` rows under a single `experiment_id`. Bootstrap CI computed from `scripts/backtest_bootstrap.py`.

| # | Criterion | Threshold | Measurement |
|---|---|---|---|
| **P1** | Sample size, resolved | n ≥ 200 per engine | `COUNT(*)` on ml_setups WHERE outcome ∈ market outcomes |
| **P2** | Win rate | WR ≥ 45% (after fees + slippage assumptions) | `wins / (wins+losses)` excluding BE |
| **P3** | Profit factor | PF ≥ 1.4 (P5 of bootstrap ≥ 1.1) | bootstrap 2000-resample |
| **P4** | Sharpe (per-trade returns, daily annualized proxy) | Sharpe ≥ 1.5 | bootstrap |
| **P5** | Average R per trade | ≥ 0.25R after costs | mean of (pnl_pct / risk_distance_pct) per trade |
| **P6** | Stability across windows | quartile CV of PF ≤ 0.5 (no golden-period collapse) | `scripts/backtest_stability.py` 4 windows |
| **P7** | Beats random-direction benchmark | bootstrap-CI non-overlap on PF and PnL | benchmark engine in same period |
| **P8** | Beats single-feature baselines | strictly higher PF than every benchmark in §6 | bootstrap |
| **P9** | Per-pair stability | If multi-pair, at least 2 pairs with n ≥ 50 each at P2/P3 thresholds | per-pair bootstrap |
| **P10** | Per-session stability | No single session contributes >70% of resolved PnL | session split |
| **P11** | Out-of-sample / walk-forward | Last 25% of period (held out during analysis) shows PF within 30% of full-period PF | chronological split |
| **P12** | Shadow replay consistency | Engine outcomes recomputed via `shared/pnl_engine.py` on the persisted candle (migration 17 `shadow_resolve_candle_*` columns) match the stored `outcome_type` on ≥ 95% of resolved rows | `@pytest.mark.db` exact-replay test, see Batch 0 changelog |

**Live-vs-shadow drift** (separate, post-promotion): once an engine is live, the first 30 live trades are compared to the trailing 30 shadow trades on the same engine on the same pair. If live PF < 0.7 × shadow PF, revert to shadow. This is a *first-pilot* criterion, not a pre-live one — it cannot exist before live exists.

Promotion does **not** mean uncapped live. It means:
- 1 engine on at most 1 pair to start.
- `RISK_PER_TRADE` reduced to 0.5% (half the configured 1%) for the first 30 live trades.
- ENABLED_SETUPS contains exactly one setup_type.
- Re-evaluation after 30 live trades (or 30 days, whichever comes first). If live PF < 0.7 × shadow PF on the same engine, **revert** to shadow.

**Hard capital blocker (BTC on OKX).** OKX BTC-USDT-SWAP minimum notional is ~$850 at $85k BTC (`MIN_ORDER_SIZES`). With current $86 OKX capital × 1% risk × 7× max leverage, the bot **cannot** size a BTC live trade. **BTC live on OKX requires ≥$500 OKX capital, OR a venue/account change.** Until that is resolved, BTC engines may be promoted in shadow but not in live execution. ETH-only live promotion is the only option at current capital.

ML re-enablement (AFML meta-label, bet sizing) follows the **separate** gate G1–G6 in `SYSTEM_BASELINE.md §7.1`. Promotion to live and ML re-enablement are independent — an engine can go live as a deterministic rule before its meta-labeling classifier is trained.

### 9.1 If shadow data never clears these bars

Plausible. The honest position: if 6 months in, no engine has cleared promotion criteria, the conclusion is **the SMC-on-7-pairs framework is not a viable edge for this account size**, not "tune more." At that point, the conversations are:
- Are we trying to extract edge from a market regime that just isn't there for retail with this data?
- Is the right move to stop building a bot and pivot the $4.6k Bybit pool to a more disciplined manual workflow with the `/check` second-opinion already shipped?
- Is the content business (`jerdev_quant`) the better expression of the work?

These are real possibilities and the redesign plan must be designed to surface them, not to hide them.

---

## 10. Contradictions, Drift, and Technical Debt

(For the user's reference. Not all of these are blockers; flagged so they don't keep recurring.)

### 10.1 Doc/code drift (resolve in pre-work)
- `docs/context/02-strategy.md` § Setup A — entry pct (0.65 vs 0.50 actual), mode ("both" vs "continuation" actual). Entry distance also unstated.
- `docs/context/02-strategy.md` § Setup F — entry distance 5% vs 2.5% actual.
- `docs/context/02-strategy.md` § Setup A — `SETUP_A_MAX_SWEEP_CHOCH_GAP=60` "aggressive (Optuna 45)" — the Optuna-validated value (45) is documented but not used. Either re-validate at 60 or revert to 45; do not run with un-validated thresholds and a stale doc.
- `SYSTEM_BASELINE.md` §5 H5 — recommendation already implemented (`HTF_BIAS_REQUIRE_4H=False`); table entry stale.
- `SYSTEM_BASELINE.md` §1 still references `SETUP_A_MAX_ENTRY_DISTANCE_PCT = 5%` in the Setup-Specific Parameters table; settings.py says 5% — but the doc/strategy summary in `02-strategy.md` says it is added 04-15, while §Setup F dropped from 5% to 2.5% the same day. Consistent within SYSTEM_BASELINE, inconsistent vs. context doc.

### 10.2 Architectural debt (does not block redesign, but worth eyeballing)
- **Geometry cascade** layered on top of structural-TP layered on top of fixed-RR fallback is three competing definitions of "where does the trade go". A single `_calculate_tp_levels` is the right primitive, but the cascade adds entry/SL search dimensions that interact with TP calculation in ways the tests don't fully exercise. The redesign should pick **one** entry rule per engine (no cascade), and let TP be structural-or-fallback. Cascade is keepable for legacy setups but should not be inherited by engines.
- **`_apply_expectancy_filters`** is two filters in a function (ATR floor, target space). Promote both to per-engine config; remove from the shared codepath. The current behavior — every setup uses the same filter values — is an artifact of having only one type of strategy.
- **Tests vs. behavior:** ~915 tests pass, but the tests that exercise the cascade + structural TP + per-setup R:R fallback interactions are sparse. A small number of "given a real OB at price X, expect entry / SL / TP_a / TP_b" golden-file tests on real candles would catch behavior regressions much faster than property tests on individual primitives. Batch 6 added some; more would help.
- **AI service** is dead code in the live path (synthetic AIDecision). `AI_BYPASS_SETUP_TYPES` is the entire active swing setup list. The AI branch in `evaluate()` should be tombstoned with a single early-return until the meta-label classifier replaces it. Right now it is unreachable code that still imports Claude.
- **Bet sizing** — `BET_SIZING_ENABLED=False` and would not work even if True (synthetic confidence=1.0). Settings present, no execution path. Leave as-is until ML gate G3+ passes.
- **Backtester / live drift** — backtester does not model funding cost in PnL, models slippage only via fill-buffer. For 12h swing trades on perps, funding can be 0.04–0.12% of notional per cycle; on a 2R trade with risk 1% that's 4–12% of the take-home R. Not catastrophic, but enough to bias backtest comparisons. Adding a simple "subtract `funding_8h_avg × hours_held / 8` from each closed trade" would close most of the gap.

### 10.3 Data debt
- ~~W17 audit's instrumentation gap (sweep_tier / funding_tier / oi_rising_tier 100% null)~~ — **resolved 2026-04-27 (pre-work item b)**. Diagnosis:
  - `sweep_tier` populates correctly for sweep-based setups (setup_a: 59/59 in current experiment). Null for setup_b/d/f is expected — those setups have no sweep gate. Not a bug.
  - `funding_tier` was null because the prior implementation parsed direction-filtered confluence strings; the strategy gate only emits `funding_X_long`/`funding_X_short` when funding aligns with the trade side. Fixed: feature now derives directly from raw `snapshot.funding.rate` magnitude, direction-agnostic.
  - `oi_rising_tier` was null for the same reason. Fixed: feature now derives from extracted signed `oi_delta_pct` against `OI_DELTA_MILD/MODERATE/STRONG_PCT`, decoupled from gate emission.
  - `has_oi_flush` is correctly populated when `recent_oi_flushes` is non-empty (1/114 in current experiment — genuine signal sparsity, not a bug).
  - Future ml_setups rows will carry correct tier values; historical pre-fix rows remain as captured (do not backfill — that would mix instrumentation regimes inside a single experiment_id).
- `ml_setups` rows from before `experiment_id` was added (pre 2026-04-16) are unjoinable to current experiment cleanly. Leave; do not try to reconcile.
- `live_resolved` per the W17 audit: 0 in audit window. The 3 closed live trades exist in `trades` but were not joined. This is fine — `trades` and `ml_setups` should not be cross-joined for ML training (per SYSTEM_BASELINE §7.0). But for post-promotion live-vs-shadow drift measurement (the first-pilot criterion noted under §9 P12), we need a small SQL join script that lives outside training paths.

### 10.4 Mismatches between ambition and capital
- $86 OKX capital × 1% risk = $0.86 per trade. 7× leverage caps notional at $602. OKX BTC-USDT-SWAP min order is 0.01 BTC ≈ $850 notional at $85k BTC — **the bot cannot trade BTC at this capital under these settings**. This is in the codebase as `MIN_ORDER_SIZES` but not flagged as a blocker.
- The $86 capital is too small to validate anything live at promotion size. The honest path is: complete shadow validation, then either (a) move bot to Bybit with a small carve-out from the $4.6k (e.g., $300, separately tracked) and revisit OKX migration, or (b) wait until live capital is at least $500. The current setup is structurally incapable of generating meaningful live signal.

---

## 11. What I Am *Not* Confident About

In the spirit of anti-bias:
- I am not confident that all three engines should ship in parallel. Engine 1 (Trend-Pullback) is the most theoretically defensible; Engine 3 (Compression) is the most testable; Engine 2 (Failed Breakout) is the highest-overlap with what the user already understands and could be the right place to start *if* Engine 1 is too sample-starved on BTC+ETH (likely, given the regime filter). A reasonable alternative plan: ship Engine 1 first solo, evaluate at week 4, then add Engine 2 only if Engine 1 is producing data but not edge.
- I am not confident that 7 pairs is wrong long-term. It is wrong *for research and validation*. Once an engine has demonstrated edge on BTC+ETH, expanding to SOL is straightforward; expanding to DOGE/XRP/LINK/AVAX needs an explicit thesis ("does this engine work on lower-cap pairs that have different liquidity microstructure?") and per-pair calibration, which are second-order problems.
- I am not confident the AFML meta-labeling roadmap (§7.1) is the right ML plan once engines are validated. It is a defensible plan, but for a 3-engine, low-frequency strategy, simpler approaches (per-engine logistic regression on top features, regime-conditional thresholds) may produce comparable improvements with far less infrastructure. We should re-evaluate the ML approach after engines exist, not commit to AFML upfront.
- I am not confident that a 4–6 month timeline is achievable. The setup-validation process can stall on either statistical insignificance (engine works but n is too small to prove it) or data drift (engine works in regime A, the regime changes). Both are ordinary outcomes. Plan for 6–9 months and treat 4–6 as the optimistic case.

---

## 12. Open Questions — Resolved 2026-04-27

User confirmations:

1. **Sequential.** Engine 1 first solo. Engines 2/3 only after multi-signal plumbing + benchmarks proven on Engine 1.
2. **BTC+ETH only for the first 2 months.** SOL added later only if sample-starvation forces expansion. DOGE/XRP/LINK/AVAX never used as design baseline.
3. **`SETUP_A_MAX_SWEEP_CHOCH_GAP` synced to 45** in pre-work. Setup A is bound for redesign; do not run un-validated thresholds.
4. **BTC min-order blocker surfaced explicitly in §9.** Line added: BTC live on OKX requires ≥$500 capital or venue/account change. No migration planned yet.
5. **Setup B will be killed (not frozen) at 30 days** if Engine 1 hits its promotion bar.

---

## A. Appendix — Reproducible Queries

### A.1 §1.1 detection / outcome counts

Run from the Postgres container: `docker exec quant-fund-postgres-1 psql -U jer -d quant_fund -c "<query>"`. Snapshot date 2026-04-27.

**Total funnel:**
```sql
SELECT
  COUNT(*) FILTER (WHERE outcome_type IN ('shadow_tp','shadow_sl','shadow_breakeven','filled_tp','filled_sl','filled_trailing')) AS market_resolved,
  COUNT(*) FILTER (WHERE outcome_type = 'shadow_no_fill')                         AS no_fill,
  COUNT(*) FILTER (WHERE outcome_type = 'shadow_dedup')                           AS dedup,
  COUNT(*) FILTER (WHERE outcome_type = 'shadow_direction_filtered')              AS dir_filtered,
  COUNT(*) FILTER (WHERE outcome_type IN ('shadow_orphaned','replaced') OR outcome_type IS NULL) AS pending_or_orphan,
  COUNT(*) AS total
FROM ml_setups
WHERE feature_version >= 4
  AND experiment_id = 'batch1_tp1_rr_1_3_2026_04_20';
```
Snapshot result: `market_resolved=11, no_fill=2, dedup=42, dir_filtered=54, pending_or_orphan=5, total=114`. Sums to 114 ✓.

**Per setup_type × outcome:**
```sql
SELECT setup_type, outcome_type, COUNT(*)
FROM ml_setups
WHERE feature_version >= 4
  AND experiment_id = 'batch1_tp1_rr_1_3_2026_04_20'
GROUP BY setup_type, outcome_type
ORDER BY setup_type, count DESC;
```

**Per pair × setup × outcome:**
```sql
SELECT pair, setup_type,
  COUNT(*) FILTER (WHERE outcome_type IN ('shadow_tp','shadow_sl','shadow_breakeven')) AS resolved,
  COUNT(*) FILTER (WHERE outcome_type = 'shadow_no_fill') AS no_fill,
  COUNT(*) FILTER (WHERE outcome_type = 'shadow_dedup') AS dedup,
  COUNT(*) FILTER (WHERE outcome_type = 'shadow_direction_filtered') AS dir_filtered,
  COUNT(*) AS total
FROM ml_setups
WHERE feature_version >= 4
  AND experiment_id = 'batch1_tp1_rr_1_3_2026_04_20'
GROUP BY pair, setup_type
ORDER BY pair, setup_type;
```

**Resolved trades detail (the n=11 + 2 no_fill):**
```sql
SELECT setup_type, pair, EXTRACT(HOUR FROM created_at) AS hour, outcome_type
FROM ml_setups
WHERE feature_version >= 4
  AND experiment_id = 'batch1_tp1_rr_1_3_2026_04_20'
  AND outcome_type IN ('shadow_tp','shadow_sl','shadow_breakeven','shadow_no_fill')
ORDER BY created_at;
```

**Experiment date range:**
```sql
SELECT MIN(created_at), MAX(created_at), COUNT(*)
FROM ml_setups
WHERE feature_version >= 4
  AND experiment_id = 'batch1_tp1_rr_1_3_2026_04_20';
```
Result: `min=2026-04-21 10:15:01, max=2026-04-27 15:15:01, count=114`. → 6 days, **not** 14.

### A.2 If counts diverge from this document later

Re-run the four queries above. If totals differ from §1.1, the most likely cause is more outcomes resolving (good — sample is growing) or `experiment_id` having been bumped (check `EXPERIMENT_ID` in `config/settings.py`). Update §1.1 in place; do not write a new section.

---

## 13. Document Lifecycle

- This document is the *design* contract. Implementation lives in PRs that reference its sections explicitly.
- It is **not** a replacement for `docs/SYSTEM_BASELINE.md`. SYSTEM_BASELINE remains source of truth for active config and changelog. This file describes *intent over the next quarter*; SYSTEM_BASELINE describes *current state*.
- Update this file when an engine ships, when a kill condition fires, or when the user changes scope. Do not edit it for minor parameter tweaks.
- When an engine reaches the end of its validation cycle (promoted, retired, or rolled back), record the verdict in §3 and move the engine description to SYSTEM_BASELINE.
- Sunset target: this file is closed when either (a) at least one engine is promoted to live and one is killed, OR (b) the entire redesign is abandoned in favor of a different approach. Whichever comes first. Either way, the closing entry summarizes what was learned.
