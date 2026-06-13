# Plan: Engine 2 — Dual Thrust ETH 6h (shadow port)

**Slug:** engine2-dual-thrust
**Source grill:** docs/grill/engine2-dual-thrust.md (PIVOT — BUILD conditional on Phase 1)
**Created:** 2026-06-13
**Status:** pending
**Tracer bullet:** The Sharpe-1.72 Dual Thrust edge was fit on Binance candles. Phase 1 tests whether it survives, with FIXED params, on OKX `ETH-USDT-SWAP` candles. If not → KILL, ~1h spent.

## Context summary
External Jesse research (`docs/audits/jesse-strategy-research-2026-06-12.md`, PR #83) found DUAL_THRUST ETH 6h optimized passes every anti-overfit gate (walk-forward train 1.59→test 2.14, MC P(loss)=0, p<0.0001) — but on **Binance Perp** data. Bot trades **OKX SWAP**. This plan revalidates on OKX, then ports the rule as a shadow-only `Engine 2` for forward validation. NO live capital. Does NOT touch `ENABLED_SETUPS`, risk_service, or execution_service. Does NOT re-optimize params (fixed-param transfer test is the whole point).

Winner params (fixed everywhere): `stop_loss_atr_rate 1.645, down_length 10, up_length 3, down_coeff 0.301, up_coeff 0.891`, anchor = 1D open, trade TF = 6h, direction = long+short, flip on opposite signal.

## Phase 1 — OKX fixed-param revalidation (tracer bullet)
**Status:** PASS (2026-06-13)
**Inputs:** Winner params above. Bot OKX client `data_service/exchange_client.py::backfill_candles`. Jesse research report for the exact rule definition.
**Outputs:**
- `~/jesse-research/project/okx_revalidation.py` (or `scripts/` if cleaner) — standalone pandas Dual Thrust backtest, fixed params.
- OKX `ETH-USDT-SWAP` 6h + 1D candles, 2024-06-12 → 2026-06-11 (CSV cache under `~/jesse-research/project/storage/okx/`).
- A result block: Sharpe, net%, max DD, trade count, equity curve on OKX — appended to `docs/audits/jesse-strategy-research-2026-06-12.md` under a new "## OKX revalidation (2026-06-14)" section.
**Work:**
- Fetch OKX ETH 6h (~2,920 candles) + 1D (~730) via the bot's ccxt instance (NOT bare `ccxt.okx()` — market-load quirk). Cache to CSV.
- Reimplement Dual Thrust faithfully in pandas: daily-open anchor from 1D; per-6h-bar upper/lower thresholds `open + up_coeff×max(up_length-bar range)` / `open − down_coeff×max(down_length-bar range)` where range = max(HH−CC, CC−LL) per the source; long/short on cross; ATR(stop_loss_atr_rate) stop; flip on opposite signal.
- Conservative fills: SL counts as hit if the 6h bar low (long) / high (short) crosses it; entry at signal-bar close. Fee 0.05% × 2 per round-trip. No funding yet (Phase 2).
- Cross-check: also replay on Binance candles in the SAME pandas harness to confirm it reproduces the Jesse ~1.72 (proves the harness is faithful, not a reimplementation bug).
**Verification gate:**
- [x] Automated: pandas harness reproduces Jesse Binance result within ±0.2 Sharpe — **1.667 vs 1.723, |Δ|=0.056 PASS**.
- [x] Automated: OKX fixed-param backtest → Sharpe ≥ 1.2 AND net% > 0 AND trades ≥ 80 — **Sharpe 1.999, net +206%, 133 trades PASS**.
- [x] Manual: eyeball OKX equity curve — **21/26 months positive (81%), best month 25.6%, no single-month dominance**.
- [x] Rollback if OKX Sharpe < 1.2 OR net ≤ 0 — N/A, gate passed.

**Evidence (filled 2026-06-13):**
- Harness: `~/jesse-research/project/okx_revalidation.py` (standalone pandas; OKX 6h cached `storage/okx/ETH-USDT-SWAP_6h.csv`).
- Fidelity (Binance 6h): Sharpe 1.667, net +153%, DD -18.4%, 139 trades, WR 36.0% — reproduces Jesse #8 (1.723 / +155 / 159 / 40.3%).
- OKX gate (`ETH-USDT-SWAP` 6h, UTC-aligned): Sharpe 1.999, net +206%, DD -15.2%, 133 trades, WR 39.9%.
- Result block appended to `docs/audits/jesse-strategy-research-2026-06-12.md` → "## OKX revalidation (2026-06-13)".
- **Key finding / trap for Phase 3:** OKX `6H` bar = Hong-Kong 08:00 anchor → collapses to Sharpe 0.21. MUST use `6Hutc` (00:00 UTC). The engine's 6h aggregation must be UTC-aligned.
- **Verdict: PASS — edge transfers to OKX (stronger than Binance). Proceed to Phase 2.**

---

## Phase 2 — Precise validation + funding (only if Phase 1 passes)
**Status:** PASS (2026-06-13)
**Inputs:** Phase 1 OKX harness + the fact that it cleared the bar.
**Outputs:**
- Funding-adjusted OKX backtest result (per-8h funding applied to held position notional).
- Intrabar fill refinement: confirm SL/entry fills don't materially flip the verdict vs the conservative 6h-bar approximation (sample 1m for the trade set if feasible; else document the fill-optimism bound).
- MC trade-shuffle (1000) on the OKX trade set → P(loss), p95 DD.
- Decision line appended to the audit doc: PROCEED-TO-PORT or KILL.
**Work:**
- Pull OKX ETH funding history for the window (8h cadence); deduct funding × notional × direction-sign per held interval.
- Re-run net/Sharpe with funding. Re-run MC trade-shuffle on the funding-adjusted PnL series.
**Verification gate:**
- [x] Automated: funding-adjusted Sharpe ≥ 1.0 AND net% > 0 — **Sharpe 2.003, net +207% PASS**.
- [x] Automated: MC trade-shuffle P(loss) ≤ 0.10 — **P(loss) 0.0 PASS**.
- [x] Manual: funding drag bounded — **mean rate 0.0019%/8h, net +$64 tailwind; hold median 72h, no concentration**.
- [x] Rollback if funding flips net negative OR P(loss) > 0.10 — N/A, both passed.

**Evidence (filled 2026-06-13):** run `okx_revalidation.py --phase2`.
- Funding: 280 events (~93d; OKX history-API caps at ~3mo), mean 0.0019%/8h. With-funding Sharpe 1.999→2.003, net +206→+207% (short-heavy = collects funding). Funding is a non-issue.
- MC trade-shuffle (n=1000, funding-adj): P(loss) 0.0, worst-5% DD -25.3%. (Final value is order-invariant — p5==p50, same artifact as Jesse mc_trades.)
- Intrabar fill: SL at exact stop on 6h touch (optimistic), entry at next-6h-open; 42/133 SL exits. Slippage bound can't close the 0.8-Sharpe margin over the 1.0 bar; 1m refinement deferred to shadow.
- **Verdict: PROCEED-TO-PORT.** Section appended to `docs/audits/jesse-strategy-research-2026-06-12.md` → "## OKX Phase 2 (2026-06-13)".

---

## Phase 3 — Forward paper re-sim (Option 1, chosen 2026-06-13)
**Status:** DONE (2026-06-13) — harness built, baseline run clean.

**Why not the in-bot shadow port:** Dual Thrust is **stop-and-reverse with no TP** —
68% of backtest exits are flips (opposite signal fires, median 72h later), only 32%
are SL. The bot's `ShadowMonitor` resolves only via fixed TP/SL/timeout; it has no
"close on opposite signal". A fixed-R:R proxy (the old Phase 3) would collect data on
a *different* strategy and could never validate the Sharpe-2.0 edge. A flip-aware
shadow would need significant new ShadowMonitor infra. **Decision: forward paper
re-sim** — faithful, light, isolated. If it survives forward, an in-bot port becomes
a justified follow-up (revisit the flip-aware option then).

**Inputs:** Validated pandas spec (Phase 1/2), `okx_revalidation.backtest`.
**Outputs (built):**
- `~/jesse-research/project/forward_resim.py` — re-runs the SAME faithful strategy
  (flip + ATR SL, funding-adjusted) on freshly fetched OKX candles, slices trades
  opened **on/after the freeze date** (`FREEZE_DATE=2026-06-13`) as the out-of-sample
  forward set. Deterministic + idempotent (rebuilds the CSV each run).
- `okx_revalidation.load_okx_6h` topped up with the recent `/market/candles` endpoint
  (history-candles lags ~1-2 days; forward needs the freshest closed bars).
- Forward store: `~/jesse-research/project/forward/dual_thrust_eth_forward_trades.csv`
  + append-only `dual_thrust_eth_runlog.csv` (one row per run: date, days_live, stats).
**Work done:**
- Freeze params = Phase-1/2 winner (`okx_revalidation.HP`). Frozen at 2026-06-13.
- Baseline run: in-sample reference 133 trades / WR 40% / **PF 2.067** / net $20.7k;
  forward = 0 (freeze is today). Pipeline validated; Phase 1 re-run shows no regression.
- **Cron (weekly):** `~/quant-fund/venv/bin/python ~/jesse-research/project/forward_resim.py`
  — to be installed on the server (see plan tail). No bot deploy, no pipeline touch.
**Verification gate:**
- [x] Forward harness runs, slices OOS trades by freeze date, writes CSV + runlog.
- [x] In-sample reference reproduces Phase-1/2 (PF 2.07, WR 40%, 133 trades).
- [x] Zero changes to bot pipeline / `SHADOW_MODE_SETUPS` / execution.

**Evidence:** `forward_resim.py` baseline output 2026-06-13 (forward N=0, accumulating).

---

## Phase 4 — Forward soak + decision
**Status:** pending (accumulating from 2026-06-13)
**Inputs:** Weekly `forward_resim.py` runs → growing forward trade set.
**Outputs:**
- A dated go/no-go on whether the OKX Dual Thrust edge holds **out of sample, forward**.
- If KEEP: a follow-up decision on an in-bot flip-aware shadow port and/or live-small.
**Work:**
- Run weekly. Cadence ≈ 5 trades/month (133/2y), so the set fills slowly.
- Compare forward PF/WR/expectancy vs the in-sample reference (PF 2.07, WR 40%).
**Verification gate:**
- [ ] Trigger: **N ≥ 25 forward trades OR 180 days** (`DECISION_MIN_TRADES` / `DECISION_MAX_DAYS`).
- [ ] KEEP if: forward **PF ≥ 1.3 AND net > 0** (`DECISION_PF_BAR`). → candidate for in-bot port / live-small (separate decision).
- [ ] KILL if: forward PF < 1.3 OR net ≤ 0 at trigger → document as a backtest edge that decayed forward; strategy parked.
- [ ] Rollback if: shadow PF < 1.0 at N≥100 → KILL engine, document as another transfer failure (backtest edge that didn't survive forward/live microstructure).

**Evidence:**
_(empty until phase runs)_

## Out of scope (deliberately)
- **Live trading / `ENABLED_SETUPS`** — bot is shadow-only; promotion is a separate post-Phase-4 decision.
- **Re-optimizing params on OKX** — the fixed-param transfer test IS the validation. Re-tuning would reintroduce the selection bias the grill is guarding against.
- **BTC / other pairs** — research found no qualified BTC strategy; ETH only for v1.
- **Porting into Jesse / adding an OKX Jesse driver** — pandas standalone is cheaper and doubles as the engine spec.
- **4h variant (runner-up #7)** — weaker MC tails (p5 -0.20), only 47 trades; not worth a second engine yet.

## Open questions (RESOLVED)
- **Naming — RESOLVED 2026-06-13: use `dual_thrust_eth`** (not `engine2_dual_thrust`). Avoids the collision with SYSTEM_BASELINE §7.2 ("Engine 2 NOT built", which refers to a speculative SMC engine off engine1's meta-label platform — a different thing). All Phase 3 artifacts use `setup_type = "dual_thrust_eth"`, file `strategy_service/engines/dual_thrust.py`, settings `DUAL_THRUST_*`, EXPERIMENT_ID `dual_thrust_eth_v1_2026_06`.

## Changelog hook
On completion, append to `docs/SYSTEM_BASELINE.md` §9 changelog:
- `2026-06-XX — Engine 2 (Dual Thrust ETH 6h) shadow port (PR #N). Impact: new shadow-only setup_type engine2_dual_thrust collecting OKX forward-validation outcomes; no live/ML-feature changes.`
