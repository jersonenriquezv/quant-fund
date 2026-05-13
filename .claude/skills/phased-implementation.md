---
name: phased-implementation
description: Execute exactly ONE phase of an existing /phased-plan, hit its verification gate, then HARD STOP. Will not advance to the next phase without explicit user "advance" instruction. Use when user says "/phased-implementation", "run next phase", "execute phase N of <feature>", or "advance the plan".
---

# phased-implementation — One Phase, One Gate, Stop

Execute a single phase from `docs/plans/<slug>.md` and stop at the gate. The user is the only entity that can authorize advancing.

## Step 1 — Locate the plan and phase

If user provided slug → read `docs/plans/<slug>.md`.
If not → list `docs/plans/*.md` with status, ask which.

Find the next phase with status `pending`. If none, tell user the plan is complete and exit.

If the user says "advance" but the previous phase status is still `in-review`:
- Read the evidence section
- Confirm gate criteria are met (re-run automated checks if quick)
- Mark previous phase `done`, then start the next pending phase

## Step 2 — Pre-flight for the phase

Mark phase status `in-progress` in the plan file.

Read in parallel:
- The plan file (full)
- The grill doc (if linked)
- All files the phase touches (search by spec keywords if not listed)
- `docs/SYSTEM_BASELINE.md` for current state

Confirm phase **inputs** match what the previous phase **outputs** declared. If they drifted, stop and tell the user the plan needs revision before code changes.

## Step 3 — Execute the work

Do exactly what the phase Work bullets specify. No scope creep. If you discover the phase is missing a step or the spec is wrong:
- Stop coding
- Edit the plan file to reflect reality (with a `Plan revision <date>:` note)
- Confirm with user before continuing

Use TaskCreate for the phase's work bullets so progress is visible.

## Step 4 — Hit the gate

Run every automated check listed in the phase's Verification gate. Capture:
- Command run
- Output (relevant excerpt)
- Pass / fail

Run any backtest, test suite, or custom script the gate names. Do not skip any.

Append results to the plan file under the phase's `Evidence` section:

```markdown
**Evidence (filled by /phased-implementation):**
- <date> — Automated checks:
  - `python -m pytest tests/ -v` → 47/47 passed
  - `python scripts/backtest.py --pair BTC --days 30` → +$X, PF Y, WR Z%
- Manual checklist:
  - [ ] User to confirm: <items>
- Rollback trigger fired: no | yes (<reason>)
- Files changed: <list>
- LOC delta: +X / -Y
```

Mark phase status `in-review`.

## Step 5 — HARD STOP

Tell the user, in this exact structure:

```
Phase N "<name>" complete.

Automated gate: PASS | FAIL — <one line>
Manual checks pending:
  - <item>
  - <item>

Files changed: <count>
Evidence appended to: docs/plans/<slug>.md

DO NOT proceed to phase N+1 without explicit "advance".
Reply with:
  - "advance" → I run phase N+1
  - "<feedback>" → I revise this phase
  - "abandon" → mark plan abandoned, stop
```

Do not start the next phase. Do not summarize what comes next. Do not offer to "continue with the rest" — the gate exists for a reason.

If the automated gate FAILED, do not even ask for advance. Report the failure, propose a fix, wait for user direction.

## Step 6 — On "advance"

Re-read the plan file (it may have been edited).
Mark current phase `done`.
Start step 1 again for the next pending phase.

## Step 7 — On final phase done

When the last phase moves to `done`:
- Mark plan status `done`
- Append the changelog line to `docs/SYSTEM_BASELINE.md` §9 (template is in the plan file)
- Tell the user: "Plan complete. Next: `/pr-creator` to bundle a PR."

Do not run /pr-creator automatically. PR creation is its own decision point.

## Failure modes to avoid

- **Do not** advance two phases in one turn even if the user says "do all of it." Reply: "I do one phase per invocation. Run /phased-implementation again after reviewing phase N."
- **Do not** edit plan structure to make a failing gate pass. If the gate is wrong, the plan is wrong — escalate, do not paper over.
- **Do not** invent evidence. If a check could not be run (missing data, blocked tool), say so explicitly and stop.
- **Do not** skip the rollback-trigger check. If the trigger condition is observed, revert the phase's commits and tell the user.
