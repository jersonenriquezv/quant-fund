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
**Status:** pending
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
- [ ] Automated: pandas harness reproduces Jesse Binance result within ±0.2 Sharpe (harness fidelity check).
- [ ] Automated: OKX fixed-param backtest → **Sharpe ≥ 1.2 AND net% > 0 AND trades ≥ 80** over the 2y window.
- [ ] Manual: eyeball OKX equity curve — no single-trade or single-month dominance.
- [ ] Rollback if: OKX Sharpe < 1.2 OR net ≤ 0 → **KILL**. Mark grill verdict KILL, stop. No engine code written.

**Evidence (filled by /phased-implementation):**
_(empty until phase runs)_

---

## Phase 2 — Precise validation + funding (only if Phase 1 passes)
**Status:** pending
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
- [ ] Automated: funding-adjusted Sharpe ≥ 1.0 AND net% > 0.
- [ ] Automated: MC trade-shuffle P(loss) ≤ 0.10.
- [ ] Manual: confirm funding drag is bounded (held-time distribution doesn't concentrate cost).
- [ ] Rollback if: funding flips net negative OR P(loss) > 0.10 → KILL (edge was an artifact of ignoring funding).

**Evidence:**
_(empty until phase runs)_

---

## Phase 3 — Port to Engine 2 (shadow), only if Phase 2 passes
**Status:** pending
**Inputs:** Validated pandas spec (Phase 1/2). Engine pattern from `strategy_service/engines/` + `service.evaluate_all()`.
**Outputs:**
- `strategy_service/engines/dual_thrust.py` — self-contained engine: 6h+1D candle aggregation buffer, Dual Thrust detection, own entry/SL/TP geometry, own gates. Emits `TradeSetup` with `setup_type = "engine2_dual_thrust"`.
- Wiring in `service.evaluate_all()` (NOT `evaluate()` first-match path).
- `config/settings.py`: `ENGINE2_*` params (defaults = winner params), pair scope (ETH only v1).
- `"engine2_dual_thrust"` added to `SHADOW_MODE_SETUPS` (NOT `ENABLED_SETUPS`).
- `tests/test_engine_dual_thrust.py`: 6h aggregation correctness, threshold cross long/short, SL direction (`_check_sl_direction`), flip logic, parity vs pandas backtest on a fixed candle fixture.
- Docs: SYSTEM_BASELINE §setup-status + §thresholds; `docs/context/02-strategy.md` detector behavior.
**Work:**
- Build 6h+1D aggregation: buffer confirmed LTF candles, emit on 6h boundary close + maintain rolling 1D open. Pipeline evaluates per-candle but engine only acts on 6h boundary.
- Implement detection mirroring the validated pandas rule exactly (shared constants where possible to prevent drift).
- Wire, gate behind `SHADOW_MODE_SETUPS`, set `EXPERIMENT_ID` tag for this engine's rows (own regime tag, e.g. `engine2_dualthrust_eth_v1_2026_06`).
**Verification gate:**
- [ ] Automated: `pytest tests/test_engine_dual_thrust.py` — all pass, 0 new failures in `tests/test_strategy_integration.py`.
- [ ] Automated: parity — engine emits the same long/short/flip decisions as the pandas backtest on a shared 100-bar fixture (≥95% match; document any divergence).
- [ ] Manual: deploy to shadow (`docker compose up -d --build bot`), confirm engine2 emits in logs within first 6h boundary; run deploy verification checklist.
- [ ] Rollback if: integration tests regress OR parity < 95% OR engine double-blocks/cannibalizes existing setups → revert branch, engine stays unmerged.

**Evidence:**
_(empty until phase runs)_

---

## Phase 4 — Shadow soak + decision
**Status:** pending
**Inputs:** Live shadow `engine2_dual_thrust` emissions.
**Outputs:**
- `scripts/report_engine2_shadow.py` (mirror of `report_engine1_shadow.py`): separates `to`/`tp`/`sl`/`be`, per-pair WR/PF.
- A dated go/no-go on whether OKX shadow reproduces the backtest edge.
**Work:**
- Collect until **N ≥ 100 resolved outcomes OR 30 days** (same exit bar as engine1).
- Compare shadow PF/WR vs backtest expectation and vs a co-emitted random-direction benchmark (reuse `engines/benchmarks.py` pattern).
**Verification gate:**
- [ ] Automated: N ≥ 100 resolved OR 30 days elapsed.
- [ ] Quantitative: shadow PF ≥ 1.3 AND WR beats random-direction benchmark by ≥ 10pp → candidate for live-small discussion (separate decision, NOT in this plan).
- [ ] Rollback if: shadow PF < 1.0 at N≥100 → KILL engine, document as another transfer failure (backtest edge that didn't survive forward/live microstructure).

**Evidence:**
_(empty until phase runs)_

## Out of scope (deliberately)
- **Live trading / `ENABLED_SETUPS`** — bot is shadow-only; promotion is a separate post-Phase-4 decision.
- **Re-optimizing params on OKX** — the fixed-param transfer test IS the validation. Re-tuning would reintroduce the selection bias the grill is guarding against.
- **BTC / other pairs** — research found no qualified BTC strategy; ETH only for v1.
- **Porting into Jesse / adding an OKX Jesse driver** — pandas standalone is cheaper and doubles as the engine spec.
- **4h variant (runner-up #7)** — weaker MC tails (p5 -0.20), only 47 trades; not worth a second engine yet.

## Open questions (must resolve before starting)
- **"Engine 2" naming vs SYSTEM_BASELINE §7.2 ("Engine 2 NOT built").** That rule referred to NOT building a speculative second SMC-style engine off engine1's meta-label platform. This Dual Thrust is an externally-validated non-SMC strategy entering shadow-only for data collection — different basis. Decide: keep the `engine2_dual_thrust` name (and add a §7.2 note distinguishing it) vs pick a non-colliding name (e.g. `dual_thrust_eth`). **User answers before Phase 3** (cosmetic — does not block Phase 1/2).

## Changelog hook
On completion, append to `docs/SYSTEM_BASELINE.md` §9 changelog:
- `2026-06-XX — Engine 2 (Dual Thrust ETH 6h) shadow port (PR #N). Impact: new shadow-only setup_type engine2_dual_thrust collecting OKX forward-validation outcomes; no live/ML-feature changes.`
