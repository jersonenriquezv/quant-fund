# Edge Audit — 2026-03-25 → 2026-04-24 (30d, experiment `batch1_tp1_rr_1_3_2026_04_20`)

## TL;DR
With **n=8 resolved shadow setups**, this dataset is a **hypothesis-generation sample, not an edge measurement**. Headline numbers (25% WR, PF 0.43, avg pnl −0.2 bps) are statistically indistinguishable from noise. The single most actionable move: **keep live trading paused and instrument for volume** — the bot is producing ~0.27 resolved setups/day, which is far below the ~100-setup threshold needed to validate any of the slices below.

## Population Overview
- **Total resolved setups:** 8 (all shadow; 0 live-resolved; 3 live closed trades exist in a separate table but are out-of-scope for this audit payload)
- **Overall:** WR 25.0%, PF 0.43, avg pnl_pct −0.002, median pnl_pct −0.001, median hold 47.5 min
- **Trust band:** ❌ Insufficient. n=8 over 30 days gives ~±30pp confidence interval on WR. Every slice below is **hypothesis, not finding**.
- **Additional concern:** Of 8 setups, 2 ended `shadow_no_fill` (25% no-fill rate). That's a potential entry-pricing problem, not a directional one.

## Per-Setup Performance

| setup_type   | n | WR    | PF   | avg pnl% | median hold |
|--------------|---|-------|------|----------|-------------|
| setup_d_choch| 4 | 50.0% | 1.87 | +0.002   | 32.5 min    |
| setup_b      | 2 | 0.0%  | 0.00 | 0.000    | n/a (both no_fill) |
| setup_d_bos  | 1 | 0.0%  | 0.00 | −0.006   | 45 min      |
| setup_f      | 1 | 0.0%  | 0.00 | −0.017   | 325 min     |

- **setup_d_choch** is the only setup pulling weight. At n=4, PF 1.87 is a coin-flip away from collapse — tag as **"watch"**, not "validated".
- **setup_b** never filled (2/2 no_fill). Entries sit ~1.1–2.0% away (`entry_distance_pct` 0.0115 and 0.0199). Setup is defining entries too deep into the OB/FVG.
- **setup_f** lone sample is the portfolio's worst trade (−1.74%, held 325 min). Zero data, max damage.

## Feature Edge Slices
All slices below are **n<15 → hypothesis only**. Reporting for direction, not significance.

- **Hour bucket — 18-24 late US (n=4):** WR 50%, PF 1.87, avg +0.002. The only positive bucket. Every positive trade in the dataset is setup_d_choch in this window. Overlap with setup mix is near-total — cannot disentangle "CHoCH edge" vs "late-US edge".
- **Hour bucket — 06-12 EU (n=2):** avg −0.009, includes the −1.74% BTC setup_f. The tape the bot sees here has not worked.
- **htf_bias bearish shorts (n=6):** WR 33%, PF 1.0. Breakeven on bias-aligned shorts.
- **htf_bias bullish longs (n=2):** WR 0%, avg −0.009. Every long lost. Hypothesis: long-side detection or timing is broken.
- **pd_aligned=True (n=5) vs False (n=3):** WR 20% vs 33%. PF 0.93 vs 0.28. **PF favors aligned, WR doesn't.** The aligned bucket has more positive tail (one full TP); the non-aligned bucket is smaller losers. Inconclusive, but the PF signal argues keep the filter.
- **cvd_aligned=True (n=4) vs False (n=4):** WR identical (25% vs 25%), but **PF 0.28 vs 0.93** and avg pnl −0.004 vs −0.000. If anything, CVD alignment is **hurting** in this sample — both top winners on PF terms occurred with cvd_aligned=False or undefined confluence. Flag for inspection: CVD gate may be trailing rather than leading.
- **confluence_count: 2 (n=3, PF 0.28) / 3 (n=3, PF 0.93) / 4+ (n=2, PF 0.00 but both no_fill).** 4+ confluence setups never filled — entries too aggressive.
- **sweep_tier / funding_tier / oi_rising_tier / has_oi_flush:** all values are `null`/`False` across every single setup. **These features are not being populated or none of these conditions triggered.** Either way, they provide zero discrimination in this dataset. Instrumentation gap.

## Shadow vs Live Delta
- **live_resolved = 0.** No comparison possible. The `live_closed_trades_table` has 3 entries but was excluded from the resolved-setup join, so drift cannot be measured this period.
- **Action:** Until live resolved ≥ 20, shadow WR/PF is the only estimator and will overstate edge once fills/slippage bite.

## Regime Edge
- **Winning regime (hypothesis):** ETH shorts, bearish htf_bias, hours 19–20 UTC, setup_d_choch. The 2 TPs (`b9431b…`, `89b8820…`) both fit this exactly.
- **Losing regime:** Longs in bullish bias during EU hours (BTC setup_f 11:00 UTC, −1.74%). Also note the BTC long's trade duration of **19.5M ms ≈ 5.4 hours** hold to SL — massively longer than any other trade and dragged by a 1.64% risk distance.
- **atr_pct range:** 0.0019–0.0040. Winners clustered at atr_pct 0.0033–0.0040 (higher vol); losers include both low-vol (0.0019 breakeven/SL) and the high-vol BTC disaster. No clean ATR cut visible at n=8.

## Instructive Trades
**Winners (shadow TP):**
- `89b8820217ca4af5` — ETH short, setup_d_choch, conf=3, pd_aligned=True (premium), **cvd_aligned=False**, hour 20. +0.66%. Fill in 59ms, trade 15 min.
- `b9431b769d734f5c` — ETH short, setup_d_choch, conf=2, pd_aligned=**False** (undefined), cvd_aligned=True, hour 19. +0.66%. Note 5m CVD −9,935 and 15m −12,854: heavy sell flow present. Instant TP (trade_duration_ms=2, suspicious — verify this isn't a bookkeeping artifact).

**Takeaway:** CHoCH on ETH in the 19–20 UTC window worked regardless of pd/cvd gate state. The gates did not add signal here.

**Losers:**
- `5a3eb8edc6a7466e` — BTC long, setup_f, **bullish bias but pd_aligned=False (undefined)**, conf=2, hour 11. −1.74%, held 5.4h. Entry_distance 0.31% (too close to current price), risk_distance 1.64% (too wide). Worst trade in the book.
- `5ad5df3e53804697` — ETH short, setup_d_bos, conf=2, pd_aligned=False, cvd_aligned=False. −0.60%. BOS variant with zero confluence tailwind.
- `f980a1e2e77b4838` — ETH short, setup_d_choch, conf=3, pd_aligned=True, cvd_aligned=True, hour 1 (Asia). −0.60%. **Same structure as the winners but in Asia session → SL.** Suggests time-of-day dominates structure.

## Leaks Detected
1. **setup_f on BTC longs (n=1).** Single worst trade, 5.4h hold, risk_distance 1.64% is ~8× tighter setups. Hypothesis: setup_f sizing / SL placement is miscalibrated for BTC. **Evidence weak (n=1) but damage is 45% of total period PnL.** Fix: disable setup_f on BTC pending n≥5 out-of-sample validation; verify SL computation.
2. **setup_b entries never fill (2/2 no_fill, conf=4).** `entry_distance_pct` 0.0115 and 0.0199 — entries sit >1% from current price. Hypothesis: limit placement too deep into OB. Fix: cap `entry_distance_pct` at 1× atr_pct (~0.003) for setup_b, or fall back to mid-OB after N minutes.
3. **Asia session (00–06 UTC) is 0/2.** Same `setup_d_choch` structure that wins in late-US loses in Asia. Hypothesis: low liquidity → SL wicks. Fix: gate setup_d_choch to hours 14–24 UTC pending more data.
4. **cvd_aligned filter is not doing its job (PF 0.28 with, PF 0.93 without).** Direction opposite of intent. Hypothesis: CVD calc window is too long (1h CVD dominates) so it confirms exhausted moves. Fix: audit CVD alignment logic to use 5m + 15m confluence rather than 1h.
5. **Telemetry leak — sweep/funding/oi_rising tiers are 100% null.** These are supposed to be the edge-concentrators per system spec. If they're not populated, every downstream slice is flying blind.

## 3 Concrete Adjustments
1. **Gate entries by session: disable setup_d_* between 00:00–14:00 UTC.** Measurable: tracks WR of 00–12 bucket (currently 0/4) vs 18–24 bucket (2/4). Verify by re-running next 30d and confirming 00–14 UTC WR on d_choch stays <30% before re-enabling.
2. **Cap setup_b entry_distance_pct at max(0.005, 1×atr_pct).** Current 2/2 setup_b setups died at no_fill with entry_distance 0.011 and 0.020. Verify by measuring fill rate over next 30d; target ≥60% fill rate before judging WR.
3. **Fix or remove the sweep/funding/oi tier pipeline.** Every setup logged null. Either the detectors aren't wired in, or conditions never triggered. Measurable: next audit should show ≥30% of setups with non-null sweep_tier. Without this, the system's "confluence story" is untestable.

## Open Questions
- **What's in `live_closed_trades_table` (n=3)?** Not joined into this audit. Any shadow-vs-live drift is invisible.
- **Why are sweep/funding/oi/dominance tiers all null?** Detector bug, logging bug, or genuinely no qualifying events? One-line SQL on `ml_setups` will settle it.
- **Is `trade_duration_ms=2` on the `b9431b…` winner real or a clock artifact?** Instant TP-from-entry is physically unlikely at ETH vol 0.22%.
- **What is the true setup generation rate?** 8 resolved / 30 days = 0.27/day is far too sparse to audit. Are detection thresholds too tight, or are setups being filtered pre-resolution?
- **Long-side detection:** 0/2 longs won, both losers held much longer than the shorts. Are SL/TP calculations symmetric for longs vs shorts? Worth a code review before accumulating more losses.