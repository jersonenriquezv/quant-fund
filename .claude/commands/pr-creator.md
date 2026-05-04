Create a pull request for the current branch with docs sync. Follow these steps exactly.

## Goal

Before opening a PR:
1. Identify which modules the branch touches.
2. Load the sub-CLAUDE.md rules for those modules.
3. Detect whether code changes contradict (drift) the rules in those docs.
4. Sync sub-CLAUDE.md / SYSTEM_BASELINE / context docs if needed.
5. Generate a PR body with module-specific context.
6. Open the PR.

## Step 1 — Branch state

Run in parallel:
```bash
git status
git log main..HEAD --oneline
git diff main...HEAD --name-only
git diff main...HEAD --stat
```

Stop if no commits ahead of main — there is nothing to PR.

## Step 2 — Map touched paths to modules

| Path prefix | Module | Sub-CLAUDE.md to load | Context doc |
|---|---|---|---|
| `strategy_service/` | strategy | `strategy_service/CLAUDE.md` | `docs/context/02-strategy.md` |
| `risk_service/` | risk | `risk_service/CLAUDE.md` | `docs/context/04-risk.md` |
| `execution_service/` | execution | `execution_service/CLAUDE.md` | `docs/context/05-execution.md` |
| `data_service/` | data | `data_service/CLAUDE.md` | `docs/context/01-data-service.md` |
| `ai_service/` | ai | `ai_service/CLAUDE.md` | `docs/context/03-ai-filter.md` |
| `dashboard/` | dashboard | `dashboard/CLAUDE.md` | `docs/context/06-dashboard.md` |
| `shared/`, `main.py` | core | `CLAUDE.md` (root) | `docs/context/00-architecture.md` |
| `config/settings.py` | config | — | always update `docs/SYSTEM_BASELINE.md` |
| `data_service/data_store.py` | schema | — | always update `docs/OPERATIONS.md` §schema |
| `tests/` | tests | (skip docs sync) | — |
| `scripts/`, `backtest_results/` | tooling | (skip docs sync) | — |

## Step 3 — Load only what is needed

Read the sub-CLAUDE.md files identified in Step 2. Do NOT read context docs unless the diff is non-trivial (>50 lines changed in that module). Do NOT read the full source — the diff is your input.

Always read:
- `CLAUDE.md` (root) — global conventions
- `docs/SYSTEM_BASELINE.md` §1 (active config) and §setup-status if config/strategy changed
- The relevant sub-CLAUDE.md files

## Step 4 — Drift detection

For each touched module, read its sub-CLAUDE.md "Rules" and "Never" sections. Compare against the diff. Flag drift when:

- Code violates a rule (e.g., guardrail added with I/O; setup added directly to ENABLED_SETUPS skipping shadow; SL cancelled before new SL placed).
- Code matches a rule that the doc says is forbidden (genuine policy change — sub-CLAUDE.md needs an update, not the code).
- A threshold changed in `config/settings.py` but `docs/SYSTEM_BASELINE.md` still shows the old value.
- A new public function/class is added that the sub-CLAUDE.md "Files" table should mention.

For each drift, classify:
- **CODE BUG** — code violates a rule. Surface to user, do NOT auto-fix.
- **DOC DRIFT** — doc is stale relative to (legitimate) code change. Propose surgical doc edit.
- **POLICY CHANGE** — rule itself is being changed intentionally. Update the sub-CLAUDE.md rule and add a changelog entry.

## Step 5 — Truth checker

```bash
python3 scripts/check_docs_truth.py
```

If it fails, fix the smallest stale section first. Re-run until it passes. PR cannot proceed if it stays red.

## Step 6 — Propose doc syncs

Show the user a brief list of proposed doc edits before applying:

```
Doc syncs proposed:
- strategy_service/CLAUDE.md: update Files table — engines/engine1_trend_pullback.py mentioned
- docs/SYSTEM_BASELINE.md §1: SETUP_A_MAX_SWEEP_CHOCH_GAP 60 → 45
- docs/SYSTEM_BASELINE.md §changelog: add entry
```

Wait for approval. If approved, apply edits, then:

```bash
git add <only the doc files just edited>
git commit -m "docs: sync CLAUDE.md and SYSTEM_BASELINE with code changes"
```

Use a HEREDOC for the commit body if non-trivial. Never add `Co-Authored-By` lines.

## Step 7 — Generate PR title and body

Title: ≤70 chars, conventional commit style (`feat(strategy):`, `fix(risk):`, `docs:`, `refactor:`).

Body template:
```
## Summary
<1-3 bullet points covering ALL commits, not just the latest>

## Modules touched
- strategy_service/ — <one-line what changed>
- risk_service/ — <one-line>

## Docs synced
- strategy_service/CLAUDE.md
- docs/SYSTEM_BASELINE.md §1, §changelog

## Drift detected (and resolved)
- <item> → <how resolved>

(omit section if none)

## Test plan
- [ ] <relevant test command>
- [ ] <manual verification step if UI / live behavior>
```

## Step 8 — Push and open PR

Confirm with the user before pushing. Then:

```bash
git push -u origin <branch>
gh pr create --base main --title "<title>" --body "$(cat <<'EOF'
<body>
EOF
)"
```

Print the PR URL when done.

## Rules

- Never push without user confirmation.
- Never commit changes outside the doc sync — code changes must already be committed by the user.
- Never bypass `check_docs_truth.py`.
- Never add `Co-Authored-By: Claude` (per user memory).
- Never use `--no-verify` to skip hooks.
- Skip docs sync entirely if the only changes are tests, scripts, or backtest_results.
- If the diff is huge (>500 lines), summarize per-module instead of attempting to read every line.
