# Grill: Liquidation cascade mean-reversion

**Date:** 2026-05-25
**Topic:** After a forced-liquidation cascade exhausts, price mean-reverts the overshoot, because the forced flow that caused it is finite and now gone. Structural edge with a clear victim (the liquidated leverage).
**Verdict:** **PIVOT** — the phenomenon is real and the mechanism holds, but the tradeable signal (large cascades) is too rare to validate now (N=10 over 106d / 4 pairs). Park in shadow-collection until N≥30, do NOT deploy capital.

## Why this hypothesis (vs SMC)

Prior session work proved /topdown's SMC triplet has no robust out-of-sample edge (confluence gates collapse to baseline on holdout). SMC patterns have no *victim* — they are shapes everyone sees, reflexive, no structural reason to persist. This hypothesis was chosen because it has a clear victim: leveraged traders force-closed at market during a cascade. The forced flow overshoots fair value; when it exhausts, price snaps back. You harvest the overshoot the victim created.

## Context loaded

- `open_interest_history` table: 142,908 rows, 7 pairs, ~106d (2026-02-08 → 2026-05-25), ~7min sampling.
- `data_service/oi_liquidation_proxy.py` (OI drop >2% in 5min = cascade) — the replacement for geo-blocked Binance liquidation feed.
- Prior failure: Setup E ("Cascade Reversal") disabled at 0W/1L — but N=1, never actually measured.
- OKX funding capped ~0.04% (funding-extreme scalp variant died, 0 emissions) — rules out the funding-reversal cousin.

## Decision tree

### Q1: Does the phenomenon exist on OKX SWAP, and is it reversion or continuation?

**Measured (not asked).** Defined a cascade as OI drop >2% over 15min + concurrent price move >1%, 1h dedup, BTC/ETH/SOL/DOGE, ~106d. Liquidation side inferred from price direction during the flush (price down = longs liquidated → expect bounce up). Measured mean-reversion forward return at 30/60/120min.

- N = 84 cascades. MR 30m: mean +0.181%, median +0.142%, 60.7% positive.
- Baseline (8,000 random bars): 30m signed mean -0.003%, 50.7% positive, mean|move| 0.298%.

**Grade:** ✅ on existence — the directional edge is real (60.7% vs 50.7% baseline = +10pp; baseline signed ≈ 0 = coin flip). The bounce is genuinely directional, not drift.

**But:** the bounce magnitude (+0.18%) is *smaller* than normal 30min volatility (0.298% baseline |move|). Post-cascade volatility is elevated, so any protective SL must be wide while the target is small → poor R:R in the naive form.

### Q2: Is it reversion or continuation? (User's intuition: "depends — continuation if from a trend, reversion if the trend exhausts.")

**Measured by cascade-size bucket (MR 60m):**

| Cascade size | N | mean MR | %pos | net after taker |
|---|---|---|---|---|
| 1–1.5% | 41 | +0.141% | 65.9% | +0.031% |
| 1.5–2.5% | 33 | **−0.125%** | **39.4%** | −0.235% |
| 2.5–4% | 7 | +1.244% | 71.4% | +1.134% |
| 4%+ | 3 | +1.509% | 100% | +1.399% |

**Grade:** ✅ for the user's intuition — cascade SIZE is the continuation-vs-reversion discriminator:
- Medium cascades (1.5–2.5%) CONTINUE (−0.13%, 39% pos) — these are trend acceleration; fading them makes you the victim.
- Large cascades (2.5%+) REVERT hard (+1.24% / +1.51%, 71–100% pos) — true capitulation, exhaustion, snap-back.
Mechanistically clean: medium flush = momentum ignition; large flush = capitulation climax.

**Kill blow:** the tradeable bucket (>2.5%) has N=10 total over 106d / 4 pairs. 100% pos on N=3 is statistically worthless. Cannot deploy capital on 10 events. The naive "fade all cascades" is itself a KILL (the 1.5–2.5% bucket is net negative and the largest by count after the smallest).

## Final verdict

**PIVOT.** The structural thesis survives where SMC did not: a real victim, a real directional edge (+10pp over coin-flip), and a mechanistically sound size-discriminator that matches first-principles (capitulation reverts, acceleration continues). But the profitable slice (large capitulations) is rare — ~10 events per 106d per 4 pairs. There is not enough data to validate a live strategy now, and the naive all-cascade version loses money on the medium bucket.

This is the opposite failure mode from SMC: SMC had plenty of data and no edge; this has a plausible edge and not enough data.

## If BUILD (the path to get there)

1. **Shadow detector, not live.** Add a cascade detector: OI drop >2% in ≤15min + concurrent price move, bucketed by size. Log every event + forward returns (30/60/120min) + a reclaim-confirmation flag. Zero capital. Pure data accumulation.
2. **Widen scope to all 7 pairs** — roughly doubles event rate (~18 large cascades projected; still need ~6 months to N≥30).
3. **Add exhaustion confirmation** (OI stops dropping + reclaim candle) and measure whether it lifts the small/medium buckets out of negative, or sharpens the large-bucket entry (tighter SL below the reclaim → fixes the R:R problem).
4. **Exit criteria:** N≥30 large-cascade (>2.5%) events with forward outcomes, large-bucket WR holding >60% and net-after-fees expectancy >0 with a realistic SL. Only then consider live.
5. **Falsification:** if at N≥30 the large-bucket edge regresses to the medium/small buckets (i.e., the +1.2% was small-sample luck), KILL.

## If KILL

- If the user does not want to wait months for N, this is a KILL by sample-size starvation — the edge may be real but is unmeasurable at current data volume and event frequency.

## Handoff

Recommended: build the **shadow cascade detector** (data accumulation, FREEZE-safe — it touches `data_service/`-style monitoring, not `strategy_service/`, and executes nothing). Accumulate N. Revisit at N≥30. This is the first structural-edge candidate that survived a grill — worth the shadow slot.

Other candidates listed but not grilled: whale-deposit pre-dump (#3, data exists but sparse), perp-spot basis (#4, no infra), funding-reversal (#2, KILLED — OKX funding cap).
