# Git Hooks

Versioned git hooks. The `.git/hooks/` directory itself is not tracked by git, so hooks must be installed locally on each clone.

## Install

```bash
./scripts/hooks/install.sh
```

Re-run after pulling new hook versions.

## Hooks

| Hook | Purpose |
|---|---|
| `pre-push` | (1) Block direct pushes to `main` (merges only). (2) WARN when service code changed without updating the matching sub-CLAUDE.md. |

## Bypass in emergencies

```bash
git push --no-verify
```

Only use when the hook is broken or you've already triaged the warning. Direct pushes to `main` should never be bypassed without explicit user discussion.

## Adding a new hook

1. Drop the script into `scripts/hooks/<hook-name>`, executable.
2. Add the name to the loop in `scripts/hooks/install.sh`.
3. Document it in this README.
4. Notify users to re-run `install.sh`.
