---
name: phased-plan
description: Convert a grilled-and-approved feature spec into a phased implementation plan with explicit verification gates. Each phase has inputs, outputs, gate criteria. Phase 1 is always the tracer bullet (riskiest assumption first). Use after /grill-me returns a BUILD verdict, or when user says "make a phased plan for X".
---

# phased-plan — Sequenced Build Plan

Convert an approved spec into 2-4 phases that each fit one context window and end in a verifiable gate. The plan file is the contract for `/phased-implementation`.

## Inputs

Required:
- A grill doc at `docs/grill/<slug>.md` with verdict `BUILD`, OR
- An explicit one-paragraph spec from the user if they skipped grilling (note this in the plan as "ungrilled — proceed with caution")

If neither is available, ask user to either run `/grill-me` first or paste the spec.

## Pre-flight reading

In parallel:
- The grill doc (if exists)
- `docs/SYSTEM_BASELINE.md` §9 (active roadmap)
- `CLAUDE.md`
- Files the spec touches (search by keyword)

## Constructing the plan

1. **Identify the riskiest assumption.** This is the one that, if false, kills the whole feature. Examples: "the signal exists in our data", "OKX exposes this field via REST", "this gate doesn't double-block all setups". This becomes Phase 1 (tracer bullet). It must be cheap and fast, designed to fail loudly if the assumption is wrong.

2. **Split remaining work into 1-3 more phases.** Total 2-4 phases. Each phase must:
   - Fit in one context window (target ≤8 file edits, ≤500 LOC delta)
   - Have one clear outcome
   - End in a gate that produces evidence (test result, backtest number, shadow emission count, manual sign-off)

3. **Outputs of phase N = inputs of phase N+1.** No drift. If you cannot define outputs concretely, the phase boundary is wrong — split differently.

4. **Each gate must specify:**
   - Automated checks (which `/test`, `/backtest`, custom script)
   - Quantitative threshold (e.g. "≥30 shadow emissions", "WR within 5pp of baseline", "0 new test failures")
   - Manual checklist (what the user must visually confirm)
   - Rollback trigger (if X happens, revert this phase)

## Output file

Save to `docs/plans/<slug>.md`. Format:

```markdown
# Plan: <feature name>
**Slug:** <slug>
**Source grill:** docs/grill/<slug>.md
**Created:** <YYYY-MM-DD>
**Status:** pending | in-progress | done | abandoned
**Tracer bullet:** <one line: what assumption Phase 1 tests>

## Context summary
<3-5 lines: why this exists, what it changes, what it does NOT change>

## Phase 1 — <tracer bullet name>
**Status:** pending
**Inputs:** <data, files, decisions from grill>
**Outputs:** <concrete artifacts>
**Work:**
- <bullet>
- <bullet>

**Verification gate:**
- [ ] Automated: <command + threshold>
- [ ] Manual: <user-visible check>
- [ ] Rollback if: <condition>

**Evidence (filled by /phased-implementation):**
<empty until phase runs>

---

## Phase 2 — <name>
(same structure, inputs = phase 1 outputs verbatim)

---

## Phase 3 — <name>
(...)

## Out of scope (deliberately)
- <thing that might seem in scope but is NOT — defend with one line>

## Open questions (must resolve before starting)
- <question + who answers>

## Changelog hook
On completion, append to `docs/SYSTEM_BASELINE.md` §9 changelog:
- One line: `<date> — <feature> shipped (PR #N). Impact: <what changed for live/shadow/ML>.`
```

## After writing the plan

1. Show the user a one-screen summary: phase names + tracer bullet + open questions.
2. Add a one-line entry to `docs/SYSTEM_BASELINE.md` §9 active roadmap pointing to the plan doc.
3. Tell the user: "Run `/phased-implementation` to start Phase 1." Do not run it yourself.

## Quality bar

A good plan has:
- Phase 1 fails fast if the core assumption is wrong (≤1 day work)
- Each gate has a number, not a vibe
- Open questions are resolved BEFORE phase 1, not deferred
- Out-of-scope section is non-empty (proves you considered alternatives)

If you cannot meet that bar, tell the user the spec is not ready and recommend re-grilling.
