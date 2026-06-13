# Grill: Engine 2 — Dual Thrust ETH 6h port to shadow

**Date:** 2026-06-13
**Topic:** Port the Jesse-validated DUAL_THRUST ETH 6h strategy into the bot as a shadow-only engine.
**Verdict:** PIVOT (in progress) — BUILD conditional on OKX revalidation passing. Grill paused after Q1 at user request to write the phased plan; remaining branches (funding, single-selection overfit, falsification) fold into plan gates.

## Context loaded
- Source research: `docs/audits/jesse-strategy-research-2026-06-12.md` (PR #83). Winner DUAL_THRUST ETH-USDT 6h optimized: Sharpe 1.72, +155%, DD -22.4%, 159 trades, walk-forward train 1.59 → test 2.14, MC trade-shuffle P(loss)=0, MC synthetic candles Sharpe p5 0.98, rule significance p<0.0001.
- Winner params: `stop_loss_atr_rate 1.645, down_length 10, up_length 3, down_coeff 0.301, up_coeff 0.891`, anchor 1D open, trade TF 6h.
- Research ran on **Binance Perpetual Futures** 1m candles. Bot trades **OKX SWAP**.
- Bot mode: shadow-only since 2026-04-15, `ENABLED_SETUPS=[]`. ~$86 OKX (untouched). Adding a shadow engine = data collection only, no live capital — consistent with shadow-only mode.
- Existing engine pattern: `strategy_service/engines/` — self-contained modules (own thesis/detection/geometry/gates), wired in `service.evaluate_all()`, emit `TradeSetup`. New strategies land here, NOT in `setups.py`.
- Promotion rule (strategy_service/CLAUDE.md): new setup → `SHADOW_MODE_SETUPS` first, never direct to `ENABLED_SETUPS`; ≥100 shadow outcomes or 30 days before promotion.

## Decision tree

### Q1: Does the Sharpe-1.72 edge survive the cross from Binance candles to OKX candles?
**My recommended answer:** Unproven = kill signal until checked. Dual Thrust is a range breakout anchored on the daily open + N-bar ranges + ATR — highly sensitive to the exact price series (daily open timezone/settlement, 6h wicks, ATR). Params optimized to 5 decimals (`down_coeff 0.301`) on Binance have NO guarantee of transferring to OKX. The optimization fit the wrong market's microstructure.
**User answer:** "no lo corrí en OKX. pero podemos correrlo con esos params sobre los datos de OKX y ver." Asked for a time estimate.
**Grade:** ✅ — correct instinct. Revalidate-before-port is the right move.
**Notes:** Investigation results:
- Bot postgres has NO 2y OKX 6h data (only intraday from ~Dec 2025; no 6h timeframe stored; 1d back to Nov 2024).
- Jesse 2.3.4 research env has NO OKX driver (Binance-only). Cannot reimport to Jesse.
- Path: fetch OKX `ETH-USDT-SWAP` 6h + 1D 2y via the bot's ccxt client (`data_service/exchange_client.py::backfill_candles`, works in prod; standalone `ccxt.okx()` hit a market-load quirk), then backtest Dual Thrust in standalone pandas (~30 lines) with fixed winner params. That pandas backtest doubles as the Engine 2 spec.
- Time: OKX 6h 2y ≈ 2,920 candles, paginated ~300/batch → ~2-4 min fetch. Pandas backtest run = seconds. First OKX number in <1h.

### Q2 (deferred to plan gate): Funding cost on a multi-hour hold
**Open concern:** 6h Dual Thrust holds positions across funding windows. The research modeled fees (0.05% × 2) but **NOT funding**. For a strategy that can hold 6h–multi-day, funding is a real cost that can erase a thin edge. Folded into Phase 2 gate (precise validation must add a funding model).

### Q3 (deferred to plan gate): Single optimization selection event
**Open concern:** Report self-flags rank-1-of-200-trials selection = residual overfit risk despite walk-forward + MC. OKX revalidation with FIXED params (no re-optimization) is the cleanest out-of-sample test: if the Binance-fit params still work on OKX un-tuned, selection bias is largely ruled out. This is exactly what Phase 1 does.

### Q4 (deferred to plan gate): Falsification criterion for shadow
**Open concern:** Need a dated, concrete kill rule for the shadow soak (Phase 4). Drafted in plan: shadow PF and Sharpe-proxy bar at N≥100 or 30 days.

## Final verdict (partial)
The underlying strategy passed an unusually rigorous Jesse anti-overfit protocol — this is NOT a vibes idea, so default-KILL is not warranted outright. But it was validated on the WRONG exchange's data. The single binding risk is Binance→OKX transfer. Everything else (funding, overfit residual, shadow soak) is downstream and cheap to gate. Verdict = PIVOT to a staged validation where Phase 1 (OKX fixed-param revalidation) is a hard KILL gate. If Phase 1 collapses, the idea dies for <1h of work. If it holds, BUILD proceeds through funding + shadow gates.

## If BUILD: pre-conditions for /phased-plan
- Resolve the "Engine 2" naming collision vs SYSTEM_BASELINE §7.2 ("Engine 2 NOT built") — that rule was about not building a speculative second SMC-style engine; this is an externally-validated non-SMC strategy entering shadow-only for data, not live. Flagged as plan Open Question.
- Phase 1 = OKX fixed-param revalidation (the tracer bullet). Done.

## If KILL: reason + what would revive it
- Killed only if Phase 1 OKX revalidation fails the bar (Sharpe < 1.2 OR negative net on OKX fixed params). Would revive if re-run on a later/larger OKX window clears the bar, or a different anchor/TF transfers.
