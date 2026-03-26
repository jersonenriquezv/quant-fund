Update project documentation to reflect recent code changes. Follow these steps exactly:

## Documentation Hierarchy

The project has 3 layers of documentation. Never duplicate content between them.

| Layer | File(s) | Purpose | Updated when... |
|-------|---------|---------|-----------------|
| **Instructions** | `CLAUDE.md` | Architecture, rules, conventions, project structure | Architecture changes, new rules/conventions |
| **Source of truth** | `docs/SYSTEM_BASELINE.md` | Active config, thresholds, setup status, gating logic, hypotheses, changelog | ANY material config/threshold/setup change |
| **Service details** | `docs/context/00-07` | Per-service implementation details (Spanish) | Behavior or interface changes in that service |

## Step 1: Get the diff

Run `git diff HEAD` (or `git diff --cached` if staged). Read the DIFF only — do NOT read full source files unless the diff references something you can't verify from context.

## Step 2: Determine what to update

### Always update SYSTEM_BASELINE when:
- Config values change in `settings.py` (thresholds, enabled setups, risk params)
- Setup status changes (enabled/disabled)
- ML_FEATURE_VERSION bumps
- Gating logic changes (pipeline order, signal hierarchy)
- New significant behavior is added

For SYSTEM_BASELINE: add a changelog entry (## 8. Changelog) with **What changed**, **Why**, **Expected impact**. Update the relevant section above with the new current state.

### Update docs/context/ when:
| Changed path | Doc file |
|---|---|
| `shared/` | `00-architecture.md` |
| `data_service/` | `01-data-service.md` |
| `strategy_service/` | `02-strategy.md` |
| `ai_service/` | `03-ai-filter.md` |
| `risk_service/` | `04-risk.md` |
| `execution_service/` | `05-execution.md` |
| `dashboard/` | `06-dashboard.md` |
| `main.py` | `00-architecture.md` |

Skip if only tests changed or the change is self-contained (internal refactor, new alert, loop optimization).

### Update CLAUDE.md only when:
- Project structure changes (new service, new directory)
- Architecture changes (new layer, different communication pattern)
- New rules or conventions are established

## Step 3: Surgical update

Read ONLY the relevant section(s) of the target doc. Edit only those lines.

### Writing rules:
- **State current behavior**, not history. "OB_MIN_VOLUME_RATIO = 1.3" not "was 1.0, changed to 1.3 in audit"
- **No duplication** — if it's in SYSTEM_BASELINE, don't repeat in docs/context/
- **Keep docs/context/ as implementation reference** — how the code works, not what the config values are
- **Changelog in SYSTEM_BASELINE only** — docs/context/ files describe current state, not history
- Update the `> Last updated:` header line in docs/context/ files

### Do NOT:
- Rewrite sections that didn't change
- Read full source files when the diff is sufficient
- Add the same information to multiple docs
- Update docs for test-only changes
