# docs/grill/ — Pre-implementation challenge docs

`/grill-me` output: adversarial challenge of an idea **before** building it. Verdict
is usually KILL, PIVOT, or BUILD. These docs are the audit trail behind why a
feature was or wasn't built — keep them so dead ideas aren't re-litigated.

## Active (top level)
Only docs whose work is still pending or load-bearing live at top level:

| File | Why it's active |
|---|---|
| `bot-viability-2026-05-13.md` | Foundational PIVOT decision (freeze + Bybit-measure path). |
| `bybit-rules-taxonomy.md` | Live Bybit rules v3; N≥30 forward-test gate still open. |
| `liquidation-cascade-reversion-2026-05-25.md` | Shadow cascade detector shipped; N≥30 exit criteria pending. |
| `smc-inducement-pullback-2026-06-01.md` | Inducement work deferred behind the 2026-06-08 v0 freeze. |

## `_archive/`
DONE (work shipped) or STALE (KILL verdict / abandoned / NO-EDGE). Kept for
decision rationale. The `/topdown` v2 cluster (PRs #37–42, never merged, NO EDGE)
carries an ABANDONED banner at the top of each file.
