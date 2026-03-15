Update `docs/context/` to reflect recent code changes. Follow these steps exactly:

## Step 1: Get the diff (NOT full files)

Run `git diff HEAD` (or `git diff --cached` if there are staged changes). Read the DIFF only — do NOT read full source files unless the diff is unclear about what changed.

## Step 2: Map changes to docs

Use this mapping to determine which doc(s) to update:

| Changed path prefix | Doc file |
|---|---|
| `config/settings.py` | Whichever doc owns the changed params (check param name) |
| `shared/` | `00-architecture.md` |
| `data_service/` | `01-data-service.md` |
| `strategy_service/` | `02-strategy.md` |
| `ai_service/` | `03-ai-filter.md` |
| `risk_service/` | `04-risk.md` |
| `execution_service/` | `05-execution.md` |
| `dashboard/` | `06-dashboard.md` |
| `main.py` | `00-architecture.md` |
| `scripts/` | `02-strategy.md` (backtest/optimize) |

If only tests changed, skip — no doc update needed.

## Step 3: Surgical update

Read ONLY the relevant section(s) of the target doc — not the whole file. Use the diff to identify what text needs to change, then Edit only those lines. Keep the same style and structure as the existing doc.

Do NOT:
- Rewrite sections that didn't change
- Read full source files when the diff is sufficient
- Add changelog entries — just update the current state
- Update docs for test-only changes
