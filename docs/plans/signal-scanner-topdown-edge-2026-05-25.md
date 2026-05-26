# Plan: signal_scanner engine → /topdown edge-triplet
**Slug:** signal-scanner-topdown-edge-2026-05-25
**Source grill:** docs/grill/signal-scanner-topdown-edge-2026-05-25.md
**Created:** 2026-05-25
**Status:** done (all 3 phases, 2026-05-26)
**Tracer bullet:** Phase 1 proves a dry-run of the new engine emits valid edge-triplets on BTC/ETH (limit price = sweep level, SL correct side, sweep ≤0.5%) at roughly the expected ~3/day under 6h dedup.

## Context summary
The classifier-graded `signal_scanner` (grade A/B from `trade_classifier`) emits SMC setups proven to have no out-of-sample edge. Replace its **engine** with the `/topdown` edge-triplet logic that measured +0.13R maker (+0.20R deduped) on BTC/ETH (`docs/audits/topdown-edge-expectancy-2026-05-25.md`). Keep the scanner **shell** (systemd timer, `signal_scanner_alerts` dedup table, Telegram delivery). Result: ~3 alerts/day, WR ~33%, each a LIMIT (maker) entry. **Do NOT modify `/topdown` behavior** — only add a behavior-preserving public helper that exposes the existing triplet. FREEZE-safe: read-only analytics, no `strategy_service` / ML touch, no bot execution.

## Resolved parameters (from grill)
- Pairs: **BTC/USDT, ETH/USDT** only (`SCANNER_PAIRS`, not `TRADING_PAIRS`).
- Gate: triplet `valid==True`, `sweep_distance_pct <= 0.5`, geometry SL on protective side, reconciled bias defined.
- TP: **single only** — alert TP = triplet `tp` (final target). Ignore `tp_mode` scaled entirely.
- Dedup: **6h per pair+direction** (existing `_recently_alerted`).
- Alert: explicit **LIMIT** entry price, SL, single TP, R:R, bias, sweep distance.

---

## Phase 1 — Edge-signal helper + dry-run engine (tracer)
**Status:** done (2026-05-26)
**Inputs:**
- `scripts/topdown_snapshot.py`: `_build_snapshot(cur, conn, pair)`, `_trade_triplet(snap)` (returns valid/entry/sl/tp/rr/sweep_distance_pct/tp_mode), `_connect()`.
- `scripts/signal_scanner.py`: existing `scan()` loop, `_recently_alerted`, dedup table.

**Outputs:**
- New public helper in `topdown_snapshot.py`: `build_edge_signal(pair) -> dict | None`. Builds the snapshot once, calls `_trade_triplet`, returns `{side, entry, sl, tp, rr, sweep_distance_pct, risk_pct, bias_confidence, current_price}` when the triplet is `valid` else `None`. **Additive, behavior-preserving** — does not alter any existing `/topdown` path.
- New `scan_edge(dry_run=True)` function in `signal_scanner.py` iterating `SCANNER_PAIRS=[BTC,ETH]`, applying the gate, printing candidate alerts. No send, no DB write.

**Work:**
- Implement `build_edge_signal` reusing `_build_snapshot` + `_trade_triplet` (no new market logic).
- Implement `scan_edge` gate: skip if `None`, skip if `sweep_distance_pct > 0.5`, force single TP = `tp`.
- Add `--edge --dry-run` CLI path.

**Verification gate:**
- [ ] Automated: `build_edge_signal('BTC/USDT')` returns a dict-or-None without exception; same for ETH.
- [ ] Automated: `scan_edge(dry_run=True)` exits 0, every emitted candidate has `sweep_distance_pct <= 0.5`, `entry==sweep level`, SL on protective side (long: sl<entry; short: sl>entry), rr>0.
- [ ] Manual: printed candidates look like the backtested triplets (sane levels on BTC/ETH).
- [ ] Rollback if: helper changes any `/topdown` brief output (must be byte-identical) → revert, expose triplet via a copy instead.

**Evidence:** (2026-05-26)
- `build_edge_signal('BTC/USDT')` → dict (short, entry 76531.4, sl 77876.5, tp 73602.5, rr 2.18, sweep 0.17%, bias medium). `build_edge_signal('ETH/USDT')` → None. No exception.
- `python scripts/signal_scanner.py --edge` exits 0; emits one candidate: `LIMIT SHORT BTC/USDT @ 76531.4 | SL 77876.5 | TP 73602.5 | R:R 2.18 | sweep 0.17% | bias medium`. ETH correctly produced no candidate.
- Gate assertion harness: all candidates satisfy `sweep ≤0.5`, SL on protective side (short sl>entry), rr>0. PASS.
- `/topdown` regression: additive-only (new public `build_edge_signal` + new `scan_edge`/`--edge`; no existing function touched → byte-identical by construction). `pytest tests/test_topdown_snapshot.py tests/test_topdown_push.py` → 124 passed.

---

## Phase 2 — Wire engine into scanner shell + alert format
**Status:** done (2026-05-26)
**Inputs:** Phase 1 `build_edge_signal` + `scan_edge`. Existing `_format_telegram`, `_recently_alerted`, `_record_alert`, `TelegramNotifier`.

**Outputs:**
- `scan()` engine swapped: classifier path (`classify`, `MIN_GRADE`, `GRADE_RANK`, `_compute_geometry`) replaced by the edge engine. Classifier functions retained in-file but dead (replay), or moved below with a comment.
- `_format_telegram_edge(signal)` — mobile-friendly, leads with **"LIMIT [side] @ <entry>"**, then SL, TP (single), R:R, bias+confidence, sweep distance. States "orden límite" explicitly.
- Dedup unchanged (6h). `signal_scanner_alerts` insert tags `auto_setup_type='topdown_edge'` so source is queryable.

**Work:**
- Rewrite `scan()` to call `scan_edge` logic on `SCANNER_PAIRS`, send via notifier, record alert with source tag.
- New formatter; retire classifier imports from the live path.
- Keep `--dry-run` for safe inspection.

**Verification gate:**
- [ ] Automated: `python scripts/signal_scanner.py --dry-run` exits 0, prints edge-format alerts (LIMIT wording present), 0 classifier references in the executed path.
- [ ] Automated: dedup blocks a second alert for the same pair+direction within 6h (unit or integration).
- [ ] Manual: format readable on mobile (375px not relevant — Telegram, but no line overflow / monospace columns align).
- [ ] Rollback if: alert omits limit price OR emits market-entry wording → fix before any live send.

**Evidence:** (2026-05-26)
- `scan()` rewritten as the edge engine (SCANNER_PAIRS, `build_edge_signal` → `_edge_candidate` gate → 6h dedup → `_format_telegram_edge` + `_record_alert_edge`). Old classifier engine renamed `scan_classifier()` (dead, replay-only); `classify` import moved local to it. Top-level classifier import removed.
- `python scripts/signal_scanner.py --dry-run` exits 0; prints LIMIT alert (`LIMIT SHORT @ 76531.4`, single TP, "orden límite (maker)" wording). ETH → no candidate.
- Classifier-token scan of live `scan()` source: NONE (`classify`, `_compute_geometry`, `_format_telegram(`, `Geometry`, `auto_grade`, `GRADE_RANK` all absent).
- Dedup roundtrip (throwaway TEST/USDT, cleaned after): before False → after `_record_alert_edge` True; opposite direction unaffected. Insert tags `auto_setup_type='topdown_edge'`.
- No code/timer references the removed Phase-1 `--edge`/`scan_edge`. systemd `signal-scanner.service` runs the script flagless → edge engine is now the default path. No unit change.

---

## Phase 3 — Falsification hook + tests + deploy
**Status:** done (2026-05-26)
**Inputs:** Phase 2 live engine. `signal_scanner_alerts` table. Existing systemd `signal-scanner.timer`.

**Outputs:**
- `signal_scanner_alerts` carries `source` (or reuse `auto_setup_type='topdown_edge'`) + the emitted `rr`, `sweep_distance_pct`, `entry/sl/tp` for later WR reconciliation against Bybit closes.
- Unit tests: gate logic (sweep>0.5% rejected, scaled forced to single, geometry guard, pair filter excludes non-BTC/ETH), formatter contains LIMIT + price.
- No new systemd unit (existing hourly timer runs the same script). Doc update.

**Work:**
- Persist emitted geometry in the alert row (columns may already exist; add idempotent ALTER if not).
- Write `tests/test_signal_scanner_edge.py`.
- `doc-update`: SYSTEM_BASELINE changelog + signal_scanner section.

**Verification gate:**
- [ ] Automated: `pytest tests/test_signal_scanner_edge.py` green; full suite green.
- [ ] Manual: one real hourly run → if a setup exists, alert arrives on phone with LIMIT instruction; if none, no alert (quiet is correct).
- [ ] Falsification armed: after N≥30 closed Bybit trades taken from these alerts, live WR ≥30% AND realized maker expectancy >0, else revert/kill (per grill).
- [ ] Rollback if: scan throws on real data OR floods (>10/day sustained) → disable timer, revert engine.

**Evidence:** (2026-05-26)
- `signal_scanner_alerts` gained idempotent columns `sweep_distance_pct`/`risk_pct`/`bias_confidence` (ALTER … ADD COLUMN IF NOT EXISTS in `_ensure_alert_table`). `_record_alert_edge` writes them + `auto_setup_type='topdown_edge'`. Roundtrip confirmed: inserted row reads back all columns populated (snapshot JSONB kept as redundant copy).
- `tests/test_signal_scanner_edge.py` — 12 tests: sweep cap (≤MAX accepted, >0.5 rejected, None rejected), geometry guard (long/short wrong-side rejected, valid accepted), rr≤0 rejected, single-TP passthrough (no tp1/tp2 surface), pair scope = BTC/ETH only, formatter (LIMIT + price + "orden límite" + "Not executed"). All green.
- Full suite: `pytest tests/` → 1288 passed, 1 xfailed.
- No new systemd unit — existing `signal-scanner.service` runs the script flagless; edge engine is the default path.

**Falsification armed:** after N≥30 closed Bybit trades from these alerts, require live WR ≥30% AND realized maker expectancy >0, else revert/kill. Reconcile `signal_scanner_alerts` (source=`topdown_edge`) against `bybit_trade_annotations` manually for the first N≥30.

---

## Out of scope (deliberately)
- Any change to `/topdown` brief logic or its Phase 4 push. (`build_edge_signal` is additive only.)
- Live bot execution — alert is a manual heads-up; user sizes + places the limit on Bybit.
- ML feature / `strategy_service` detector changes (FREEZE-respect).
- Re-validating the edge — settled in the audit. This plan instruments it; forward WR is the falsification.
- DOGE/SOL/XRP/LINK/AVAX — edge not confirmed; explicitly excluded.

## Open questions (resolve before starting)
- **Q: Keep classifier code or delete?** Decision: retain in-file dead (replay value), swap only the live loop. No deletion.
- **Q: How to link an alert to its eventual Bybit outcome for WR?** Decision: log full geometry + source tag in `signal_scanner_alerts`; manual reconciliation against `bybit_trade_annotations` for the first N≥30. Automated join is a later optimization.
- **Q: Force single-TP how?** Decision: alert always uses triplet `tp` (final target) as the single TP; `tp_mode` ignored. Matches the single-mode slice that carried the edge.

## Changelog hook
On completion, append to `docs/SYSTEM_BASELINE.md` §8:
- `2026-05-XX — signal_scanner engine replaced: classifier grade A/B → /topdown edge-triplet (BTC/ETH, sweep ≤0.5%, single-TP, maker-limit). ~3 alerts/day, backtested +0.20R maker. Forward falsification: N≥30 live WR ≥30%.`
