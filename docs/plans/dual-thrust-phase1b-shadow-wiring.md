# Plan: Dual Thrust Phase 1b — pipeline shadow wiring

**Slug:** dual-thrust-phase1b-shadow-wiring
**Source grill:** docs/grill/dual-thrust-phase1b-shadow-wiring-2026-06-15.md
**Created:** 2026-06-15
**Status:** pending
**Tracer bullet:** Phase 1 tests the one assumption that kills everything — that the bot's own WS-built ETH 4h candle store reproduces OKX REST `4H` exactly. If it does not, the bot cannot reproduce the validated signals live and the whole shadow→live path is moot.

## Context summary
Wire the already-validated Dual Thrust engine (Phase 0 parity PASS) into the live bot as a **shadow** evaluator for **ETH 4h only**. Logs signals + a theoretical flip position; places NO orders. Goal: retire feed/real-time-path risk cheaply before $86 goes live (Phase 1c). Does NOT touch `risk_service`/`execution_service`, does NOT bump `ML_FEATURE_VERSION`, does NOT change `ENABLED_SETUPS`. Bot stays shadow-only. 6h deferred (no `candle6Hutc` WS sub yet).

## Phase 1 — Candle-parity tracer (bot 4h store vs REST `4H`)
**Status:** pending
**Inputs:** Grill hard condition (anchor must come from 4h bars, never bot `candle1D` = HK-aligned). Bot stores ETH 4h candles (PG `candles` + in-memory). REST `4H` is the source `live_check` already validated against the harness.
**Outputs:** `scripts/dual_thrust_candle_parity.py` + a printed verdict (mismatch count over last N bars). NO `main.py` changes.
**Work:**
- Read the bot's stored ETH 4h candles (PG `candles` where pair=ETH/USDT, timeframe=4h) for the last ≥200 closed bars.
- Fetch the same window from OKX REST `4H` (reuse `dual_thrust_live_check.fetch_okx_bars`).
- Compare bar-for-bar: timestamp alignment + OHLC equality (tolerance for float rounding only).
- Also assert: engine `replay_signals` on the bot candles == on the REST candles (same signal stream).

**Verification gate:**
- [ ] Automated: `python scripts/dual_thrust_candle_parity.py` → **0 OHLC/timestamp mismatches over ≥200 bars** AND identical signal stream.
- [ ] Manual: eyeball the few most recent bars side-by-side.
- [ ] Rollback if: any unreconcilable mismatch → STOP. Either the WS feed needs fixing, or pivot live execution to read REST candles directly (making 1b moot). Document and re-grill.

**Evidence (filled by /phased-implementation):**
_empty_

---

## Phase 2 — Flip-aware shadow tracker + guarded hook
**Status:** pending
**Inputs:** Phase 1 PASS (bot 4h feed == REST, signals identical). Engine `latest_signal` (Phase 0). Pipeline entry `main.py:on_candle_confirmed`.
**Outputs:** `execution_service/dual_thrust_shadow.py` (or extend) — a flip-aware theoretical tracker (real-time replay of the harness fill model: entry at signal, flip on opposite, ATR stop, no orders). Flag-gated hook in `on_candle_confirmed` for ETH 4h. Shadow persistence (reuse a shadow/log table). New setting `DUAL_THRUST_SHADOW_ENABLED` (default true once Phase 1 passes; it is order-free so safe).
**Work:**
- Tracker class: feed it the trailing ETH 4h window, derive anchor from those 4h bars (NEVER bot `candle1D`), compute signal, maintain a theoretical position (side/entry/stop), flip on opposite signal, record each event + theoretical PnL.
- Hook in `on_candle_confirmed`: `if settings.DUAL_THRUST_SHADOW_ENABLED and candle.pair == "ETH/USDT" and candle.timeframe == "4h":` → call tracker. Wrap in try/except (an engine error must never break the pipeline).
- Persist signals/flips to a shadow table (or structured log) with enough to re-derive parity.
- Deploy via `docker compose up -d --build bot`; run the deploy-verification checklist.

**Verification gate:**
- [ ] Automated: unit tests for the flip state machine (entry→flip→stop) + a parity test that the tracker's recorded signals over the soak window match a harness re-run on the SAME bot candles (0 diffs).
- [ ] Automated: `pytest tests/test_execution.py tests/test_data_service.py` → 0 new failures (proves the hook didn't disturb existing paths).
- [ ] Manual: after deploy, confirm bot stays `Up (healthy)`, no new errors, and ≥3–5 real ETH 4h signals observed with the flip state machine behaving correctly.
- [ ] Rollback if: bot regresses (crash, error spam) OR tracker signals diverge from the harness re-run → flag off (`DUAL_THRUST_SHADOW_ENABLED=false`), no redeploy of logic needed; revert hook if persistent.

**Evidence (filled by /phased-implementation):**
_empty_

---

## Phase 3 — Gate handoff to live-small (Phase 1c)
**Status:** pending
**Inputs:** Phase 2 shadow running clean.
**Outputs:** A go/no-go note appended to `docs/plans/dual-thrust-live-small-port.md` (Phase 1c entry).
**Work:**
- Confirm gate: candle-parity PASS (Phase 1) + flip state machine correct on ≥3–5 real signals + tracker==harness over the soak.
- If PASS → Phase 1c (live-small) becomes the next plan: this is where `execution_service` + sizing + real orders enter, and it gets its OWN grill/plan (it touches the money path).

**Verification gate:**
- [ ] Manual: operator sign-off that shadow behaved as the harness predicts.
- [ ] Rollback if: shadow shows the real-time path can't reproduce the validated behavior → do not go live; investigate.

**Evidence (filled by /phased-implementation):**
_empty_

## Out of scope (deliberately)
- **Real orders / `execution_service` / sizing** — that is Phase 1c (live-small), a separate money-path change needing its own grill. 1b is order-free.
- **6h variant** — bot has no `candle6Hutc` WS subscription; deferred to Phase 2 of the parent plan.
- **Standalone REST runner** — rejected in grill Q5 (duplicates execution/monitor/reconcile infra). Reuse the bot's candle-driven pipeline instead.
- **2-week soak as a hard gate** — collapsed (grill Q6) to a deterministic candle-parity check + a short behavioral confirm.

## Open questions (must resolve before starting)
- None blocking. The one real risk (feed drift) IS Phase 1 — resolved by running it, not by deferring.

## Changelog hook
On completion, append to `docs/SYSTEM_BASELINE.md` §8 changelog:
- `2026-06-?? — Dual Thrust Phase 1b shadow wiring shipped (PR #N). Impact: ETH 4h Dual Thrust now evaluated in the live pipeline in shadow (order-free); feed-parity vs REST retired; ready for live-small (Phase 1c). No risk/execution change, no ML version bump.`
