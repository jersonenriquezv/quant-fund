# docs/plans/ — Implementation plans

`/phased-plan` output: the phased build plan for an approved idea. Status markers
inside each doc (done / in-review / pending) track per-phase progress.

## Active (top level)
Only plans with pending phases or current system-of-record status:

| File | Why it's active |
|---|---|
| `bybit-journal-v2-2026-05-30.md` | Current Bybit journal system of record (7 phases COMPLETE, #46–53). |
| `chart-replay-2026-06-01.md` | Backend shipped (#55/#56); frontend phases still pending. |
| `scalp_shadow_v1.md` | Open shadow experiment; `liq_reclaim` survivor under 2026-06-08 review. |
| `smc-inducement-pullback-fixes-2026-06-01.md` | Parked until 2026-06-08; W1–W3 pending. |

## `_archive/`
DONE (all phases shipped) or STALE (superseded / NO-EDGE). Kept for rationale.
The `/topdown` v2 cluster (PRs #37–42, never merged, NO EDGE) carries an
ABANDONED banner at the top of each file.
