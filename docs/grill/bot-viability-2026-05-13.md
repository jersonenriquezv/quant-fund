# Grill: Bot viability audit
**Date:** 2026-05-13
**Topic:** Does this trading bot have demonstrable edge, or do we tear it down and restart with a focused thesis?
**Verdict:** PIVOT — freeze existing bot + measure-first on Bybit pivot + rebuild strategy_service only after data justifies one specific thesis

## Context loaded
- `CLAUDE.md` — 5-layer architecture, 7 pairs OKX SWAP, 24/7
- `docs/SYSTEM_BASELINE.md` — too large to read in full; pulled summary from MEMORY.md
- `MEMORY.md` — full state index
- `backtest_results/TRACKER.md` — full
- `git log -30` — last 30 commits, all incremental fixes/kills
- service surface: strategy_service has setups.py, quick_setups.py, scalp_setups.py, engines/, plus 6 indicator modules; risk_service has guardrails + position_sizer + state_tracker; execution_service has 5 modules including campaign_monitor + shadow_monitor + position_guardian
- audits already on file: ai-service (2026-03-18), edge-audit W17, engine1-maker-fillrate, ml-v0-engine1, scalp-fee-viability, scalp-silent-detectors

## Key facts pulled

| Fact | Value | Source |
|------|-------|--------|
| Live trades since 2026-04-15 | 0 | MEMORY.md |
| Live capital | $86 | MEMORY.md |
| Best backtest PnL | +$7,558 / 60d / aggressive / no AI | TRACKER.md |
| Best backtest PF | 2.63 (N=18) | TRACKER.md row 16 |
| Active shadow setups | A short, B, D_choch, D_bos, F, engine1 (7 pairs short), scalp (3 variants) | MEMORY.md |
| Setups killed last 8 days | sweep_choch, funding_extreme, setup_a long, setup_g, setup_h, setup_c, setup_e | MEMORY.md + git log |
| ML feature version | 18 (v1-v3 corrupted, churn rate ~1/week) | MEMORY.md |
| AI service | bypassed for all setups since v2 (89.6% approval = no value) | TRACKER.md |
| ML v0 AUC | 0.72 provisional, single run | MEMORY.md |
| Engine1 v1c forward-test | WR 0-8% (N=14) vs bench_random 15% | MEMORY.md |
| Capital basis for backtests | unspecified (synthetic) | TRACKER.md |

## Decision tree

### Q1: One-sentence microstructure thesis
**My recommended answer:** None defensible. Bot is bag of SMC pattern detectors borrowed from retail pedagogy. No mechanism, no proven persistence on OKX SWAP.
**User answer:** "Liquidity sweeps past stop clusters trigger forced liquidations, then price reverts to fill institutional orderblock imbalances — bot fades sweep, captures mean reversion."
**Grade:** ⚠️ weak
**Notes:**
- Cascade part is measurable (~2.4 OI flushes/day OKX)
- "Fill OB imbalance" = unfalsifiable SMC framing — every reversion post-hoc labeled OB-fill
- Not OKX-specific (generic crypto, ignores OKX funding-cap reality)
- **Empirical record contradicts thesis:** every sweep/fade setup killed or unproven
  - setup_a long killed (5% WR, 1/20)
  - scalp_sweep_choch killed (WR 7.7% vs random 30%)
  - scalp_funding_extreme killed (0 emissions/4d)
  - B/D/F: 0 live trades in 30d, no positive shadow result
- User self-admits SMC not working, asks about engine1 alternative
- Engine1 is trend-pullback (opposite thesis), also failing WR 0-8% N=14

### Q2: Prior probability any discretionary-pattern strategy has positive expectancy on retail crypto perps after fees
**My recommended answer:** ~5-15%. Base rate brutal. 0/10 own setups beat random at N≥15. Academic TA-edge literature post-2010 ~zero net of fees.
**User answer:** Prior 8%. Beta(1,11) posterior on 0/10 setups beating random. Honest sunk-cost diagnosis. Pivot rank: (1) Bybit execution bot for $4.6k manual book — discipline > discretion, no alpha needed. (2) Funding carry backtest, weekend of code. (3) ML v0 meta-gate as filter over existing dead setups; expects regression to ~0.55 on clean holdout. (4) Stat-arb/vol/MM gated by capital+infra.
**Grade:** ✅ survives
**Notes:**
- Bayesian rigor present and correct
- Sunk-cost self-diagnosis explicit
- Pivot ranking respects $108 + retail latency + 1 venue
- One concern: pivot #3 anchored on AUC 0.72 from contaminated training data — must be discounted heavily

### Q3: Define edge / falsification / success metric for top pivot (Bybit execution bot) in ≤25 words
**My recommended answer:** Edge = remove measured -2.3R revenge / -1.5R no-SL leaks recovers ~$X/mo. Falsify if max-loss tail still beyond 1R after 30d. Success = 60 trades zero SL violations + DD ≤ planned-R×√N.
**User answer:** Conceded — no numbers. Hypothesis only: revenge + no-SL + oversize cost >X bps/trade vs clean. Magnitude unmeasured. Defined Phase 0 = measurement only, no code. Decision table: leak >50bps → build bot; 10-50bps → checklist + review; <10bps → discipline isn't the problem; rule-following also losing → kill verdict accepted, stop building, trade manual 60 trades with checklist. Prior on "execution bot is the answer" dropped from "highest EV" to ~30% conditional on Phase 0.
**Grade:** ✅✅ strong survives
**Notes:**
- Conceded same failure mode without prompting
- Refused to fabricate numbers
- Built quantitative decision rules with bps thresholds
- Included "stop building bots entirely" branch — rare and correct
- Caveats raised: define rule-violating taxonomy BEFORE SQL (anti p-hack); use Welch's t-test on paired samples + Cohen's d not p-value alone

### Q4: Existing bot's fate
**My recommended answer:** B — freeze, no touches to strategy_service, ML v0 re-train at scheduled dates, decide A or C based on holdout result.
**User answer:** Accepts B. Asks to move first re-run to 5/18 (12 days vs 26). Confesses real diagnosis: lost the thread, built 7 strategies in parallel without validating any, Engine1 stalled while Engine2/3 already queued, scalping piled on top. Self-identified correct discipline: sequential, not parallel — one strategy → test → ship if works → next. Asks to rewrite strategy_service from scratch with one clear strategy when the time comes.
**Grade:** ✅✅✅ honest pivot
**Notes:**
- Confession of feature-factory pattern is the most important insight of the whole grill
- Date correction: first re-train should be 5/25 (N≈120-150) not 5/18 (N≈60-80, too thin)
- Bybit Canada concern raised: Mexican-registered account mitigates but Bybit historically stricter than OKX on off-jurisdiction; manageable at $4.6k, problem if scaling
- Future strategy_service rewrite must pass /grill-me FIRST. No Engine 2/3/scalp in parallel — one thesis, one gate, sequential

## Final verdict

**PIVOT.** Three concurrent commitments, in priority order:

1. **Freeze the existing bot** with hard rule: zero touches to `strategy_service/`, `quick_setups.py`, `scalp_setups.py`, `engines/` between today and 6/8. Shadow keeps emitting. ML v0 re-trains 5/25 + 6/8.
2. **Phase 0 Bybit measurement** in parallel. Define rule-violation taxonomy in a written doc BEFORE running SQL. Compute leak in bps. Decision rules already defined in Q3.
3. **Hold all strategy_service rewrites** until both (1) ML v0 holdout result is in AND (2) Phase 0 result is in. Whoever wins informs what the new strategy_service looks like — or whether to abandon strategy code entirely (option C: extract platform).

**Forbidden between now and 6/8:**
- New setups, even "small" ones
- ML feature version bumps
- Engine 2 / Engine 3 work
- Scalp variant tuning
- Any commit touching `strategy_service/` files

**Allowed:**
- Bug fixes in `data_service/`, `risk_service/`, `execution_service/` if they don't change setup behavior
- Phase 0 Bybit code (lives outside `strategy_service/`)
- Docs, monitoring, infrastructure

## If BUILD: pre-conditions for /phased-plan

When 6/8 results land and a rebuild path is justified, the next /grill-me must answer:
- Which single thesis (funding carry / stat arb / Bybit execution / something else)?
- Why this one and not the other defensible options?
- What's the smallest possible Phase 1 tracer bullet?

## If KILL of any future idea: reasons that would revive it

- New academic evidence specific to OKX SWAP / Bybit perps post-2024 (not pre-2020 equity studies)
- Capital scaling to $5k+ which unlocks strategy classes currently gated by infra cost
- Direct measurement of a structural inefficiency (not pattern-on-chart)

## Process commitments going forward

- **Sequential, not parallel.** One strategy at a time. No Engine N+1 until Engine N has either passed gate or been killed with evidence.
- **Every non-trivial change** goes through /grill-me → /phased-plan → /phased-implementation → /pr-creator → /babysit-pr.
- **Kill list grows, not feature list.** Default action is delete, not add.

