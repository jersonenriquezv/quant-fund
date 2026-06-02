# Grill: /topdown session-quality gate

**Date:** 2026-05-24
**Topic:** Suppress (or downgrade to "spectator") `/topdown` triplet emissions during the Asian killzone, on the thesis that off-session entries are lower quality.
**Verdict:** **KILL** — crypto WR is session-agnostic in 150d data. The forex session-quality logic does not transfer to 24/7 crypto. The "dangerous" Asian session is in fact the *highest*-WR session in the sample.

## Origin

User shared a top-down trading video (2026-05-24). Point 4 (16:47 in the source): "A perfect technical analysis can fail if the hour is wrong. Trading London/NY open gives the volume; trading off-session (Asia) is dangerous — the market ranges or lacks force." Proposed adapting this to `/topdown` by gating emissions on killzone.

Existing partial implementation: `_bos_session_quality` in `scripts/topdown_snapshot.py` already labels Asian-session BOS as low quality, but only *annotates* — it does not suppress the triplet.

## Context loaded

- `scripts/topdown_snapshot.py` — `_killzone_now` (ICT killzone hours), `_bos_session_quality` (Asian = low, else high). Annotation only, no gating.
- `backtest_results/topdown_20260524_192804_trades.csv` — 6,830 emissions, 150d, BTC/ETH/SOL/DOGE.
- Verdict on the related 1D-veto idea: KILL (`docs/grill/_archive/1d-htf-veto-layer-2026-05-20.md`).

## Decision tree

### Q1: Does WR actually differ by killzone session in our data? (Answered by query, not asked.)

**Recommended answer (prior):** If the video thesis holds, Asian/dead-zone WR should be materially worse than London/NY.

**Measured (CSV, resolved trades only):**

| Session (UTC) | N | resolved | WR |
|---|---|---|---|
| London 7-10 | 859 | 515 | 21.6% |
| NY 12-15 | 887 | 536 | 23.1% |
| Asian 20-24 | 1,138 | 622 | 23.8% |
| AsiaLate 0-7 | 1,937 | 1,105 | 23.1% |
| dead | 2,009 | 1,144 | 21.7% |

**Grade:** ❌ — kills the thesis. WR spread across all sessions is 2.2pp (21.6%–23.8%), within noise. The "dangerous" Asian killzone is the *highest*-WR bucket; London (supposedly premium) is the *lowest*. There is no session signal to gate on.

## Final verdict

**KILL.** The forex session-quality concept is venue-specific: forex volume concentrates in banking-hours overlaps, so off-session liquidity is genuinely thin. Crypto trades 24/7 across a globally distributed participant base; there is no structurally "dead" session. The data confirms this directly — gating out the Asian killzone would remove the *best*-performing slice and lose ~1,138 emissions for zero WR benefit.

This does not refute the video wholesale — points 1, 2, 3, 6, 7 (TF hierarchy, one-step-down, SMC primitives, don't-fight-HTF, look-left) already map cleanly to `/topdown` and the look-left principle was just validated by the sweep-distance tightening (5.0 → 1.0). Only the session-timing point fails the crypto transfer test.

## What would revive it

- A re-run on a longer window (e.g. 12 months) showing a persistent ≥8pp WR gap between London/NY and Asia/dead. Unlikely given the flatness here.
- Restricting to a single high-volume pair (BTC) where institutional flow may be more session-clustered. Worth a 5-minute follow-up query if the user insists, but the all-pair flatness makes a per-pair signal improbable.
- Slippage/spread data showing off-session *execution cost* is materially worse even if WR is flat. This is the only angle with a plausible crypto basis — Bybit spread may widen in thin hours — but it is an *execution-cost* argument, not an *entry-quality* one, and would need orderbook history we do not currently store.

## Handoff

Do not build the session gate. Keep `_bos_session_quality` as annotation only. If the user wants to pursue the execution-cost angle (off-session spread), that is a separate, data-gated investigation requiring orderbook history.
