# Strategy Refinement Guide

**Audience:** the operator (you), not an ML engineer.
**Purpose:** explain, in plain language, when the bot stops "just collecting data" and starts "refining entries / going live", and where each signal stands today.
**This page copies no thresholds.** The exact numbers (the "bars") live in one place — `SYSTEM_BASELINE.md`. This page teaches the ideas and points you to the bar. If a number here and a number there ever disagree, SYSTEM_BASELINE wins.

> **Last "you are here" snapshot:** 2026-06-08. Current readings below are point-in-time and drift as data accrues; re-pull before acting. The bars do not drift unless SYSTEM_BASELINE is edited.

---

## 1. The four words you need (plain language)

You don't need the math. You need what each word answers.

- **N** — how many trades we've seen. More N = more trust the result wasn't luck. A 70%-win streak over 5 trades means nothing; over 200 it means something.
- **WR (win rate)** — % of decided trades that won. Useful, but a high WR with tiny wins and huge losses still loses money — so WR alone is never enough.
- **PF (profit factor)** — dollars won ÷ dollars lost. **1.0 = break even. Above ~1.3 = actually making money. Below 1.0 = bleeding.** This is the one that tells you if a signal pays.
- **AUC** — a 0.5-to-1.0 score for "can the model tell winners from losers before the trade resolves?" **0.5 = coin flip (knows nothing). 0.7 = it knows something real.** We measure it to answer one question: *is the edge real, or are we fooling ourselves?* It does NOT say "this is profitable" — only "there's signal here worth filtering on."

One more, because it trips people up:

- **Holdout** — trades the model was NEVER shown while learning. We grade it only on these, like an exam with fresh questions, so it can't cheat by memorizing. When holdout score ≈ training score, trust it. When training is great but holdout is mediocre, the model memorized instead of learned — that's the "overfit gap", and the cure is more data, not more tweaking.

---

## 2. The core confusion: "edge" is not "profitable"

These are two different milestones. A signal can have edge and still not make money yet.

- **Has edge** = it beats a coin flip / random entry. Proven by AUC > 0.5 and by beating the random benchmark.
- **Profitable** = PF above break-even after fees.
- **Ready to go live** = profitable AND enough N AND fires often enough AND passes the safety gates.

A signal climbs these like rungs on a ladder. "Collect more data" is not busywork — it's how a signal moves up a rung. The frustration ("collect, collect, when is it ready?") goes away once you see that **each rung has a written bar**, and you can read off exactly which rung each signal is on.

---

## 3. Where each signal stands today (the map)

### engine1_trend_pullback — *has edge, not yet profitable*
- **Reading (2026-06-08):** AUC 0.716 (N=283). Raw WR ~45%, PF ~1.03 — i.e. break-even. Beats random (WR ~27%, PF ~0.48) clearly.
- **Translation:** the signal genuinely knows something (3 stable AUC runs prove it's not luck). But traded raw — taking every signal — it only breaks even.
- **What turns it into money:** a *filter* (meta-label) that takes only the high-quality signals and skips the rest. The AUC says such a filter is feasible.
- **Its bar:** the "is there signal?" pre-gate is `SYSTEM_BASELINE` **§7.2** (passed: EDGE CLARO → do not build Engine 2). The "filter drives real money" gate is **§7.1 (G1–G6)** — NOT yet met (we haven't run the rigorous version: purged cross-validation, calibration, Kelly check).
- **Open question (needs your call):** §7.1's G1 asks for ≥500 *filled* (live) outcomes. In shadow mode there are zero live fills — so G1 can never pass while we stay shadow-only. Either we (a) accept shadow outcomes as the proxy (we already have ~668 resolved shadow outcomes), or (b) decide engine1 must go live small to earn real fills. **This is the real blocker, and it's a decision, not more data.**

### scalp_liq_reclaim_v1 — *profitable, not enough data / too rare*
- **Reading (2026-06-08):** WR ~79%, PF ~2.56, N=44, fires ~2.8×/day. Crushes its random control (WR ~35%, PF ~0.39).
- **Translation:** this is the standout of the whole system — it actually makes money. The catch is twofold: only 44 decided trades (could still be a good-luck regime), and it fires too rarely.
- **Its bar:** the scalp graduation rules in `SYSTEM_BASELINE` **§9** (Scalp Shadow v1). It currently passes WR / PF / beats-random, but **fails on N** (needs more) **and on frequency** (~2.8/day vs the ≥5/day bar). So "profitable" does not yet equal "graduate to live."
- **Is it being meta-labeled?** No — and it shouldn't be yet. With only ~9 losing examples there's nothing for a filter to learn from. Here the detector itself IS the filter (that's why WR is so high). The job is *confirm*, not *refine*: let N grow and see if 79% holds.

### Legacy SMC setups (A / B / D / F) — *dead, collecting labels only*
- Declared empirically dead 2026-05-13 (0 of 10 beat random at N≥15). No refinement work. They stay on only to keep producing training labels. See `docs/grill/bot-viability-2026-05-13.md`.

---

## 4. "Can we refine NOW, with the 283 we have?" — Yes, partly

There are two lanes. Knowing which is which is the whole point.

**Lane A — Analyze now (no live money, allowed today at current N):**
You can already slice engine1's 283 trades by the features the model says matter most (e.g. pullback depth, entry distance from the impulse) and look for a *subset* where PF jumps above break-even. If found, that becomes a tighter **entry gate** — it filters which signals to take **without changing what creates the edge**. This is real, useful, and doable now. It is analysis + a possible entry-rule tweak, not flipping the bot live.

**Lane B — Go live with real money (needs the gates):**
Trusting a filter with capital needs more than a hopeful AUC: a bigger holdout, a smaller overfit gap, calibration, and the §7.1 safety checks. That's where "collect more data" genuinely applies — not for analysis, but for *confidence to bet*.

So the honest answer to "when is it ready?": **ready to analyze and propose a tweak = now. Ready to risk money = when the §7.1 / §9 bars are met.** Different rungs, different bars, both written down.

---

## 5. What we are deliberately NOT doing yet (parked)

The full SMC plan + scenario analysis + multi-timeframe narrative work (from `notes.md`) is **parked as a future phase.** It is intentionally out of scope right now.

**Trigger to un-park it:** both engine1 *and* liq_reclaim have graduated to live and are profitable with real fills, AND capital justifies the added complexity. Until then, adding that layer would be building on signals that haven't earned their keep.

---

## 6. Quick reference — where the real numbers live

| Question | Plain answer | Authoritative bar |
|----------|--------------|-------------------|
| Does engine1 have signal? Build Engine 2? | Yes / No (don't build) | SYSTEM_BASELINE §7.2 |
| When can an ML filter trade engine1 live? | When the safety gates pass | SYSTEM_BASELINE §7.1 (G1–G6) |
| When does a scalp signal graduate to live? | When N / WR / PF / beats-random / frequency all pass | SYSTEM_BASELINE §9 (Scalp Shadow v1) |
| Current standing of each signal | See §3 above (snapshot, re-pull to refresh) | live DB query |
| Why the bot wasn't killed on 6/8 | Non-SMC signals carry edge | SYSTEM_BASELINE §9 (FREEZE resolution) |

To refresh the live readings: `python scripts/report_engine1_shadow.py`, `python scripts/ml_v0_engine1.py`, `python scripts/report_scalp_shadow.py`.
