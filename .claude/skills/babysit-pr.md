---
name: babysit-pr
description: Monitor an open GitHub pull request until it is merged or closed. Polls CI checks, review comments, and mergeability via gh CLI. Surfaces failures with log excerpts, retries flaky checks up to 3 times, fixes branch-related CI errors, and waits for explicit user authorization before merging. Never auto-merges. Never auto-replies to human review threads. Use when user says "babysit PR <n>", "watch the PR", "/babysit-pr", or asks to monitor CI and review feedback on an open PR.
---

# babysit-pr — Persistent PR Watcher (gh CLI version)

Monitor a PR until terminal state. Use `gh` CLI only. Pair with `/loop` for the polling cadence.

## Inputs

Accept any of:
- No argument → infer PR from current branch (`gh pr view --json number`)
- PR number
- PR URL

If no PR found on current branch and no arg given, stop and ask user.

## Strict rules

- **Never** run `gh pr merge`. Merging requires explicit user instruction in the chat.
- **Never** post replies to human-authored review comments. Surface them with a suggested response and wait.
- **Never** push to `main` directly.
- **Never** rerun green checks. Only rerun failed checks classified as flaky.
- **Never** force-push.

## Initial snapshot

Run in parallel:

```bash
gh pr view <n> --json number,title,state,mergeable,mergeStateStatus,headRefName,headRefOid,reviewDecision,statusCheckRollup,reviews,comments
gh pr checks <n>
```

Report a one-screen status:

```
PR #<n>: <title>
State: <open|merged|closed>  Mergeable: <yes|no|unknown>  Review: <APPROVED|REVIEW_REQUIRED|CHANGES_REQUESTED|none>
Checks: <passed>/<total>  Failed: <names>
SHA: <short>
Open review threads: <count>
```

If state ≠ open, stop immediately and report terminal state.

## Polling loop

Use `/loop` with these cadences:
- CI red or pending → poll every 60s
- CI green + open review threads → poll every 5min
- CI green + clean reviews + waiting on approval → poll every 5min
- After any change (new SHA, check flip, new comment) → reset to 60s

On each poll, re-read the same json fields. Diff against last snapshot. Only emit a chat update on:
- New failed check
- New passed check that flips overall to green (one-time celebratory line)
- New review comment from human or trusted bot
- Mergeable state change
- New SHA

Heartbeat update every 10 polls if nothing changed: one line like `still watching PR #N — <pass>/<total> green, no new comments`.

## Diagnose failed check

When a check fails:

```bash
gh run view <run-id> --json jobs,conclusion,workflowName --log-failed
```

Read the log tail (last 200 lines). Classify:

- **Branch-related** (compile error, test failure in touched files, lint, type error): patch locally → commit → push → reset poll cadence to 60s.
- **Flaky** (network timeout, runner provisioning, transient external service, snapshot mismatch on retry-stable test): rerun once via `gh run rerun <run-id> --failed`. Track retry count. Max 3 retries per run-id, ever.
- **Infra / dependency outage** (registry down, Github Actions infra error): stop, tell user, wait.
- **Ambiguous**: do one more diagnostic look (read job logs in detail). If still unclear, surface to user with suggested classification and wait.

Commit message for branch-related fixes:
```
ci: fix <short reason> on PR #<n>
```

## Handle review comments

Surface every NEW comment from:
- Human reviewer (any user)
- Trusted review bot (Claude, Codex, etc.)

For each surfaced comment:
1. Quote the comment + author + file:line
2. State whether it is actionable
3. If actionable and you agree:
   - Patch locally
   - Commit: `fix: address PR review feedback (#<n>)`
   - Push
   - Resolve the thread via `gh api graphql` mutation `resolveReviewThread`
   - Reset polling
4. If you disagree or it needs a written response:
   - Show user the comment + your suggested response
   - **Wait** for user to say "post it" before any GitHub write

## Mergeability

If `mergeStateStatus` is `DIRTY` (merge conflict):
- Do not auto-rebase. Surface to user.
- Show: `gh pr view <n> --json mergeStateStatus,mergeable` output.
- Wait for user direction.

If `mergeStateStatus` is `BLOCKED` due to required review:
- Continue watching at slow cadence.
- Note in heartbeat: "blocked on review approval".

## Stop conditions (only)

- PR merged or closed → report final state, stop.
- Retry budget exhausted on a flaky check → stop, ask user.
- Merge conflict → stop, ask user.
- Infra outage → stop, ask user.
- Ambiguous failure after 1 diagnostic pass → stop, ask user.
- User says "stop" or interrupts.

A green + mergeable PR is **not** a stop condition. Keep watching for late review comments until merge.

## Final summary (when stopping for terminal state)

```
PR #<n> — <merged|closed|blocked>
Final SHA: <full>
CI: <pass>/<total>
Fixes pushed during watch: <count> (commits: <shas>)
Flaky retries used: <n>/3 per run
Unresolved review threads: <count> + list
```

## Authorization to merge

When the PR is green + reviewed + mergeable, report:

```
PR #<n> ready to merge.
- CI: all green
- Reviews: approved
- Mergeable: clean
- Open threads: <count>

Reply "merge" to run `gh pr merge <n> --squash` (or specify --merge / --rebase).
Reply anything else and I keep watching.
```

Do **not** merge without that explicit reply.
