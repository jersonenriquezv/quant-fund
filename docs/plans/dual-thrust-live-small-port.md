# Dual Thrust — In-Bot Port + Live-Small (ETH 4h + 6h)

**Status:** PLAN — not started (2026-06-15)
**Owner decision:** break the "shadow-collect-forever" loop. Dual Thrust already passed walk-forward (test Sharpe > train), Monte Carlo P(loss)=0, and Binance→OKX cross-exchange transfer (Sharpe 1.999) — the exact out-of-sample test engine1 failed. Next information gain is **real fills**, not more simulation.
**Capital:** OKX live balance verified $86.30 USDT (2026-06-15, SANDBOX=False). Sufficient — see §7.

---

## 1. Goal & scope

Port the validated Dual Thrust rule into the bot as a **flip-aware engine**, run it **live-small** on ETH with real money at 0.5–1% risk/trade. Validate the edge survives real fills + the risk/executor plumbing, before scaling capital.

**In scope:** ETH-USDT-SWAP only. 4h variant first (native TF), 6h variant second (needs new subscription). Long/short flip strategy.
**Out of scope:** other pairs (BTC failed research; alts unvalidated), re-optimization (fixed-param transfer is the whole point), touching engine1 / existing setups.

---

## 2. Strategy spec (exact, from `~/jesse-research/project/candidates.json`)

Rule (per `RESEARCH_REPORT.md` line 45 + `okx_revalidation.py`):
- **Anchor** = current 1D candle open (UTC, no lookahead).
- **Upper trigger** = `anchor_open + up_coeff × max(HH−CC range over last up_length bars)`
- **Lower trigger** = `anchor_open − down_coeff × max(range over last down_length bars)`
  where range per bar = `max(HH−LC, HC−LL)` style (use harness's exact `_range` def — copy verbatim).
- **Long** when bar close > upper trigger. **Short** when bar close < lower trigger.
- **Stop** = `ATR(14) × stop_loss_atr_rate` from entry.
- **Flip on opposite signal** (always in market once triggered; opposite signal closes + reverses).
- Entry executes at **next bar open** after signal (no same-bar lookahead). SL is intrabar.

**Params (optimized-rank1-by-train):**

| TF | stop_loss_atr_rate | up_length | up_coeff | down_length | down_coeff |
|----|----|----|----|----|----|
| **6h** (winner) | 1.6452 | 3 | 0.8911 | 10 | 0.3011 |
| **4h** (runner-up) | 0.5947 | 19 | 0.7110 | 27 | 0.5809 |

**Critical trap:** 6h MUST be UTC-aligned (`6Hutc`, 00:00 UTC anchor). OKX default `6H` = Hong-Kong 08:00 → collapses Sharpe to 0.21. 4h on OKX is already UTC-aligned via the bot.

---

## 3. Current state

- **Validated, NOT in bot:** Dual Thrust lives only as the Jesse/pandas harness (`~/jesse-research/project/`). Does not touch `ENABLED_SETUPS`, risk, or execution.
- **Bot TFs:** HTF=4h,1h; LTF=15m,5m; +1d (campaign bias). → **4h + 1D anchor are native. 6h is NOT subscribed.**
- **Bot is shadow-only** (`ENABLED_SETUPS=[]`). Going live = first live setup since 2026-04-15.

---

## 4. Integration challenges (the real work)

1. **Flip-aware position model ≠ bot's discrete SL/TP model.** Existing setups = one-shot entry with SL/TP/timeout, no reversal. Dual Thrust is **continuously in-market**, flips long↔short on signal. Needs a dedicated engine + execution path (model on `campaign_monitor.py`, which already does long-lived positions + adjustments), NOT the `TradeSetup`→`execute()` one-shot path. The flip = close current + open opposite atomically (reuse the "new SL before old cancel" discipline; here it's "open reverse, then confirm flat").
2. **6h candle subscription missing.** 4h variant ships with zero new data infra. 6h variant requires adding a `6Hutc` business-WS channel (or UTC-aligned aggregation from 1h). Do 4h first to validate the engine, add 6h after.
3. **1D anchor, no lookahead.** Use the *open* of the in-progress UTC day, frozen at day start. Bot has 1d candles (campaign bias TF) — reuse.
4. **Sizing.** 0.5–1% risk/trade. ETH min order = 0.001 ETH (~$1.7 notional) — NOT binding at $86 (see §7). risk_service passes strategy SL through (does not tighten); confirm the flip engine bypasses one-shot guardrails that assume SL/TP/timeout.
5. **Fees/funding.** Flip = taker entries + continuous hold. At $17–35 notional, fee ~$0.017 RT, funding pennies. Negligible vs ATR moves. Model 0.05% taker ×2 (already the bot's `TRADING_FEE_RATE`).

---

## 5. Phased plan

### Phase 0 — Brain port + parity gate (no money) — ✅ DONE 2026-06-15
Port the signal brain verbatim, prove it reproduces the harness before money.
- **Deliverables shipped:** `strategy_service/engines/dual_thrust.py` (brain verbatim — `wilder_atr`, thrusts incl. down quirk, raw signal, 1D anchor), `scripts/dual_thrust_parity.py` (authoritative gate vs harness), `tests/test_dual_thrust_engine.py` (CI-safe unit tests, 7 passing).
- **PARITY RESULT:** engine reproduces the harness **trade-for-trade** — 6h: 133 trades identical, Sharpe **1.9967**, net **+206.43%**, final $30,642.51 (= documented winner). 4h param branch also bit-identical on the same candles. **PASS ✅.**
- Brain fidelity proven. What remains is the data feed (live candles) + execution (real orders) — Phases 1–2.
- **Still TODO before live:** wire as a *shadow* engine in the pipeline (per-confirmed-candle `latest_signal`) so live signals are logged + re-checked vs harness on fresh candles for ≥2 weeks.

### Phase 1 — 4h live-small (native TF, fastest)
Flip `dual_thrust_eth_4h` to live, $86, **0.5% risk/trade**. 4h needs no new data infra.
- Real fills, real flips, real risk/executor exercised.
- Monitor: fill slippage vs harness assumption, flip latency, SL placement, any `sl_too_close`/guardrail rejections.
- Run until **N≥20 real trades** (4h ≈ 8–12 trades/mo → ~6–8 weeks).

### Phase 2 — Add 6Hutc + 6h live
Add `6Hutc` WS subscription, ship `dual_thrust_eth_6h` live alongside 4h. 6h is the statistically cleaner winner (MC p=0.00 vs 4h p=0.09) but slower (~5 trades/mo).

### Phase 3 — Scale or kill decision
- **KEEP / scale:** combined live PF ≥ 1.3 AND net > 0 at N≥20 → add capital to $500→$1,000 (separate decision).
- **KILL:** live PF < 1.0 at N≥30 → document as transfer failure (backtest edge that died on real microstructure), revert to shadow-only.

---

## 6. Risk controls

- Hard cap: ETH only, $86, 0.5% risk/trade Phase 1. No scaling until Phase 3 gate.
- `TRADING_HALTED` kill switch + `/emergency` remain wired.
- Max one open position per TF variant (4h + 6h can both be open = max 2 ETH positions, opposite or same side — cap total ETH exposure).
- Daily DD guardrail from risk_service still applies.
- Every flip logged; weekly parity re-check vs harness during Phase 1.

---

## 7. Capital verdict — is $86 enough?

**Yes, to VALIDATE. No, to MATTER in dollars.**

- OKX ETH min order = 0.001 ETH ≈ $1.7 notional → min-size is **NOT binding**. At 0.5% risk ($0.43) with a 2.45% stop, position ≈ $17 notional = 0.01 ETH — comfortably above minimum.
- So $86 runs the full strategy at correct risk %. The point of live-small is **proving the edge + plumbing survive real fills**, which $86 does fine.
- But absolute returns at $86 are trivial (a great +50% run = +$43). This phase is for **proof, not income**.

**Recommendation:** Start with the existing $86. Do **NOT** add money until it proves itself live (Phase 3 gate: PF≥1.3, net>0, N≥20). Adding capital before live proof = repeating the engine1 mistake (committing before out-of-sample survival). Once it passes live, scale to $500→$1,000.

---

## 8. Deliverables checklist
- [ ] `strategy_service/engines/dual_thrust.py` — rule + both param sets, flip-aware signal.
- [ ] Shadow engine wiring + parity report (Phase 0).
- [ ] Flip-aware execution path (extend `campaign_monitor.py` or new `flip_monitor.py`).
- [ ] `6Hutc` WS subscription (Phase 2 only).
- [ ] Sizing + guardrail integration for flip model.
- [ ] `ENABLED_SETUPS` / live toggle for `dual_thrust_eth_4h`, then `_6h`.
- [ ] Docs: SYSTEM_BASELINE setup status + changelog; this plan status updates.

**Refs:** `docs/plans/engine2-dual-thrust.md` (shadow-port plan, Phase 1–4), `docs/audits/jesse-strategy-research-2026-06-12.md`, `~/jesse-research/project/okx_revalidation.py`.
