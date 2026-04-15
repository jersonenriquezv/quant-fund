Use this skill when updating or reviewing project documentation, especially `CLAUDE.md`, `docs/SYSTEM_BASELINE.md`, `docs/OPERATIONS.md`, and `docs/context/*`.

## Goal

Docs must be short, current, and code-backed. The top of `SYSTEM_BASELINE.md` is operational truth, not a diary. History belongs in the changelog.

## Truth Hierarchy

1. Code constants and migrations are the source for machine-checkable facts.
2. `docs/SYSTEM_BASELINE.md` summarizes current trading state and strategy/risk config.
3. `docs/OPERATIONS.md` summarizes deploy, recovery, security, schema, and monitoring.
4. `docs/context/*` explains implementation details, not active thresholds.
5. `CLAUDE.md` contains durable architecture/rules only.

## Required Check

Before finishing any docs/config/schema/pipeline change, run:

```bash
python3 scripts/check_docs_truth.py
```

If it fails, fix the smallest stale doc section and run it again.

## Writing Style

- Use terse engineer prose.
- Prefer tables for current config.
- Avoid repeating the same fact across docs.
- Current-state sections say what is true now.
- Changelog entries say what changed, why, expected impact.
- Do not preserve stale historical notes in active tables.

## Automation

Use `/doc-audit` when the user asks whether docs are trustworthy, when docs feel stale, or after changing:
- `config/settings.py`
- `data_service/data_store.py`
- `main.py`
- `strategy_service/`
- `risk_service/`
- `execution_service/`
