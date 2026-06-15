# Plan: Dual Thrust Phase 1b — pipeline shadow wiring

**Slug:** dual-thrust-phase1b-shadow-wiring
**Source grill:** docs/grill/dual-thrust-phase1b-shadow-wiring-2026-06-15.md
**Created:** 2026-06-15
**Status:** done (code complete; live soak = open gate before Phase 1c)
**Tracer bullet:** Phase 1 tests the one assumption that kills everything — that the bot's own WS-built ETH 4h candle store reproduces OKX REST `4H` exactly. If it does not, the bot cannot reproduce the validated signals live and the whole shadow→live path is moot.

## Context summary
Wire the already-validated Dual Thrust engine (Phase 0 parity PASS) into the live bot as a **shadow** evaluator for **ETH 4h only**. Logs signals + a theoretical flip position; places NO orders. Goal: retire feed/real-time-path risk cheaply before $86 goes live (Phase 1c). Does NOT touch `risk_service`/`execution_service`, does NOT bump `ML_FEATURE_VERSION`, does NOT change `ENABLED_SETUPS`. Bot stays shadow-only. 6h deferred (no `candle6Hutc` WS sub yet).

## Phase 1 — Candle-parity tracer (bot 4h store vs REST `4H`)
**Status:** done (gate FAILED → resolved via pivot; see evidence + Plan revision 2026-06-15)
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
- **2026-06-15 — GATE FAILED.** `scripts/dual_thrust_candle_parity.py` over 299 overlapping ETH/USDT 4h bars (2026-04-26 → 2026-06-15): **215 OHLC mismatches across 143/399 bars (~36%)**, signal stream 1 diff / 272 (2026-05-12 12:00 flipped 0→-1).
- **Root cause (proven):** 143/143 mismatched bars have the bot's range **strictly inside** OKX REST `4H` range (bot high ≤ rest high AND bot low ≥ rest low; open always matches = first tick). Signature of WS 4h candles confirmed/stored on a **partial tick window** — the bot misses wick extremes (and sometimes close). Chronic + intermittent (spread evenly Apr→Jun, not the 6/15 crash-loop alone). Worst example 2026-06-12 12:00: bot saw 1669–1672, real bar was 1652–1690.
- **Conclusion:** the bot's WS-built 4h store **cannot** faithfully reproduce the validated Dual Thrust signals. Signal impact is small TODAY (1/272) only because Dual Thrust is mostly FLAT with wide thrust margins — not safe to rely on near a threshold or for live capital.
- **Decision → pivot (plan rollback option B):** live/shadow path must read OKX REST `4H` directly (the authoritative source Phase 0/1a already validated against), NOT `data_store.load_candles`. Phase 2 re-scoped: hook still fires on the bot's confirmed ETH 4h candle (timing trigger only), but the tracker fetches REST bars for computation.
- **Broader flag (out of scope for 1b):** partial-candle drift likely affects ALL pairs/TFs in the WS store — every SMC setup + ML feature reads `load_candles`. Worth a separate investigation/ticket; NOT fixed here.

---

## Phase 2 — Flip-aware shadow tracker + guarded hook
**Status:** done (automated gate PASS; deploy + soak = Phase 3 gate)

**Plan revision 2026-06-15:** Phase 1 gate FAILED — the bot's WS candle store was unreliable (partial bars). Root cause fixed separately (PR #90, deployed 2026-06-15: `dropped_forming` guard + DO-UPDATE upsert; parity 21/21 PASS, candles now clean). Per Phase 1 rollback option B, this phase reads **OKX REST `4H` directly** for the signal computation, NOT `data_store.load_candles` — REST is the authoritative source Phase 0/1a validated against, and this decouples the shadow from any residual feed risk. (Post-#90 the bot store is also viable, but REST-direct is chosen for robustness; the per-4h-close fetch is trivial.) The bot's confirmed ETH 4h candle is the **timing trigger only**.

**Inputs:** Phase 1 resolved (pivot to REST-direct). Engine `replay_signals` / `latest_signal` (Phase 0, `strategy_service/engines/dual_thrust.py`). REST fetch `scripts/repair_partial_candles.fetch_closed_candles` (or `dual_thrust_live_check.fetch_okx_bars`). Pipeline entry `main.py` confirmed-candle hook.
**Outputs:** `execution_service/dual_thrust_shadow.py` — a flip-aware theoretical tracker (replay of the harness fill model: entry at signal, flip on opposite, ATR stop, no orders), computing on freshly-fetched REST `4H` bars. Flag-gated hook in the confirmed-candle path for ETH 4h. Shadow persistence (structured log / shadow table). New setting `DUAL_THRUST_SHADOW_ENABLED` (default false; order-free so safe to flip on).
**Work:**
- Tracker class: on each ETH 4h close, fetch the trailing REST `4H` window, derive the anchor from those 4h bars (NEVER bot `candle1D` = HK-aligned), compute the signal via the verbatim engine, maintain a theoretical position (side/entry/stop), flip on opposite signal, record each event + theoretical PnL.
- Hook in the confirmed-candle path: `if settings.DUAL_THRUST_SHADOW_ENABLED and candle.pair == "ETH/USDT" and candle.timeframe == "4h":` → call tracker. Wrap in try/except (an engine error must never break the pipeline).
- Persist signals/flips with enough to re-derive parity.
- Deploy via `docker compose up -d --build bot`; run the deploy-verification checklist.

**Verification gate:**
- [ ] Automated: unit tests for the flip state machine (entry→flip→stop) + a parity test that the tracker's recorded signals match a harness re-run on the SAME REST candles (0 diffs).
- [ ] Automated: `pytest tests/test_execution.py tests/test_data_service.py` + new tracker tests → 0 new failures (hook didn't disturb existing paths).
- [ ] Manual: after deploy, bot stays `Up (healthy)`, no new errors, and ≥3–5 real ETH 4h signals observed with the flip state machine behaving correctly.
- [ ] Rollback if: bot regresses (crash, error spam) OR tracker signals diverge from the harness re-run → flag off (`DUAL_THRUST_SHADOW_ENABLED=false`); revert hook if persistent.

**Evidence (filled by /phased-implementation):**
- 2026-06-15 — Automated checks (branch `feat/dual-thrust-phase1b-shadow`):
  - `execution_service/dual_thrust_shadow.py`: `simulate_fills` (verbatim port of harness fill loop) + `DualThrustShadowTracker` (re-fetches REST 4h each close, full deterministic replay). Constants verbatim (RISK_PCT 2.0, START_BALANCE 10000, FEE_RATE 0.0005).
  - Hook in `main.py:on_candle_confirmed` (ETH/USDT 4h only, gated by `DUAL_THRUST_SHADOW_ENABLED`, run in executor, try/except). Fetcher = `_data_service._exchange.backfill_candles("ETH/USDT","4h",500)` (authoritative REST, forming bar dropped post-#90). Setting added (default OFF).
  - `tests/test_dual_thrust_shadow.py` → 6 passed: flip state machine (entry→flip→short), intrabar SL fill, no-signal, pair/TF filter, same-bar dedup, insufficient-candles guard.
  - **Parity gate** `scripts/dual_thrust_shadow_parity.py` → **PASS**: port == harness `_fill_sim_with_engine` on 1000 fresh OKX 4h bars — 16/16 trades, 0 field diffs. Theoretical balance 10431.77.
  - `pytest tests/test_execution.py tests/test_data_service.py tests/test_dual_thrust_shadow.py` → 145 passed. Full suite → 1387 passed, 1 xfailed. `py_compile main.py` OK.
- Manual checklist (DEFERRED — needs deploy + soak):
  - [ ] Enable `DUAL_THRUST_SHADOW_ENABLED=true`, deploy, observe ≥3–5 real ETH 4h signals with the flip machine behaving. DT is mostly FLAT → expect days between signals.
- Rollback trigger fired: no.
- Files changed: `execution_service/dual_thrust_shadow.py` (new), `main.py` (hook+init), `config/settings.py` (flag), `tests/test_dual_thrust_shadow.py` (new), `scripts/dual_thrust_shadow_parity.py` (new).

---

## Phase 3 — Gate handoff to live-small (Phase 1c)
**Status:** done (handoff note written; live-small GATED on soak — see go/no-go)
**Inputs:** Phase 2 shadow running clean.
**Outputs:** A go/no-go note appended to `docs/plans/dual-thrust-live-small-port.md` (Phase 1c entry).
**Work:**
- Confirm gate: candle-parity PASS (Phase 1) + flip state machine correct on ≥3–5 real signals + tracker==harness over the soak.
- If PASS → Phase 1c (live-small) becomes the next plan: this is where `execution_service` + sizing + real orders enter, and it gets its OWN grill/plan (it touches the money path).

**Verification gate:**
- [ ] Manual: operator sign-off that shadow behaved as the harness predicts.
- [ ] Rollback if: shadow shows the real-time path can't reproduce the validated behavior → do not go live; investigate.

**Evidence (filled by /phased-implementation):**
- 2026-06-15 — Go/no-go note appended to `docs/plans/dual-thrust-live-small-port.md` Phase 1b/1c section. Summary: 1b code SHIPPED + parity proven by construction (engine + fill-model both verbatim, parity scripts PASS); candle-parity risk retired by the partial-candle fix (#90, 21/21 PASS) — the original "≥2-week parity on the bot feed" requirement is SUPERSEDED because the tracker reads authoritative REST directly, not the bot feed. **Remaining gate before Phase 1c is NO-GO until:** flag deployed + ≥3–5 real ETH 4h flip events observed behaving correctly + no pipeline regression. Phase 1c (live-small, real money) then gets its OWN grill — not opened here.

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
