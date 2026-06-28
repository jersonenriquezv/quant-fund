# Runbook — Engine 1 ML-filter Phase 3 go-live (small)

**Plan:** `docs/plans/engine1-ml-filter-live.md` §Phase 3
**What this is:** first real OKX capital since shadow-only (2026-04-15). engine1
top-tercile (score ≥ 0.847) routes to real execution at $1.5 risk. Kill line
10R = $15. Everything else stays shadow.

**Safety net is NOT a sandbox — it is the tiny R + the kill switch.** That is by
design ("go live small").

---

## Pre-flip checklist
- [ ] PR #111 merged to main (Phase 2 wiring). ✅ 2026-06-28
- [ ] `OKX_SANDBOX=false` (real account). Confirmed.
- [ ] `~$86` funded on OKX. Confirmed.
- [ ] `ENABLED_SETUPS=[]` (engine1 live is the ONLY live path → no interference).
- [ ] `TRADING_HALTED` unset/false.
- [ ] Frozen cutoff `ENGINE1_SCORE_CUTOFF=0.847` (do NOT lower — that was a
      sandbox-only plumbing trick).

## The flip (one variable)
In `config/.env`:
```
ENGINE1_LIVE_GATED_ENABLED=true
```
Deploy (never sudo/nohup/kill):
```
docker compose up -d --build bot
```
Post-deploy verify (reference_deploy_verification checklist):
```
docker compose ps bot                 # Up, Restarts=0
docker compose logs --tail=50 bot | grep -E "live_gate=ON|LIVE: \[\]|balance"
```
Expect: `ENGINE1_LIVE_SCORE ... live_gate=ON` on the next engine1 emission,
real balance (~$86) fetched, no crash loop.

---

## What to watch (first ~15–20 real trades)

Live emission → execution log trail:
```
docker compose logs -f bot | grep -E "ENGINE1_LIVE_SCORE|engine1 LIVE gate|Risk approved|execute|KILL SWITCH"
```
Per eligible setup you should see, in order:
1. `ENGINE1_LIVE_SCORE ... score=0.8x eligible=True live_gate=ON`
2. `engine1 LIVE gate ... routing to REAL execution (risk=$1.5)`
3. `Risk approved: size=... leverage=...`
4. execution_service places the order on OKX (real).

Cross-check on OKX: order at min size, SL + TP attached, position closes at SL/TP.

Per-trade log (grill watch-items):
- intended entry vs actual fill (slippage)
- fill rate (≥ 80% target)
- SL rate — shadow top-tercile had **0 SL**; a live SL cluster = overfit signal.

---

## Kill / rollback — DATA-DRIVEN, do not freelance

The bot **auto-reverts** new engine1 entries to shadow + fires a CRITICAL
Telegram alert when ANY of these breach (engine1_kill_switch.py):
- cumulative drawdown > **10R** ($15), OR
- **7 consecutive losses**, OR
- rolling-20 PF < **1.2** after ≥ 20 trades.

When the alert fires, make it permanent:
```
# config/.env
ENGINE1_LIVE_GATED_ENABLED=false
```
`docker compose up -d --build bot` → back to pure shadow.

**Below 10R = normal variance. Let it ride. Do NOT stop early.** (grill Q6)

Emergency (unrelated to kill thresholds — any "get me out now"):
`TRADING_HALTED=true` env or `/emergency` Telegram → freezes new execution,
`close_all_positions()` available. Runbook: `docs/OPERATIONS.md`.

---

## Exit criteria → Phase 4 (scale)
Over first ~15–20 live trades ALL of:
- fill rate ≥ 80%, slippage within tolerance
- live top-tercile WR ≥ 45%
- rolling-20 PF ≥ 1.2

Then: top-up (+$100 held ready), raise `ENGINE1_RISK_USD` proportionally, same
cutoff, same kill rules in R. Do NOT top up before live ≈ shadow is confirmed.

## Changelog hook (on flip)
Append to `docs/SYSTEM_BASELINE.md` §9 the date + the fact that engine1
top-tercile is live at $1.5 risk behind `ENGINE1_LIVE_GATED_ENABLED`.
