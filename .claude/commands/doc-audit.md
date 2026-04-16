Audit documentation truth against code, then fix only proven drift.

## Step 1: Run the deterministic checker

```bash
python3 scripts/check_docs_truth.py
```

If it passes, stop. Report "Docs truth check passed" and do not rewrite docs.

## Step 2: Fix reported drift

For each reported issue:
- Read only the affected section of the named doc.
- Read the code source only if the checker output is insufficient.
- Patch the smallest possible doc range.
- Keep current-state sections current-state only.
- Put history only in `docs/SYSTEM_BASELINE.md` changelog.

## Step 3: Re-run

```bash
python3 scripts/check_docs_truth.py
```

Stop only when the checker passes or when a code/doc ambiguity requires human judgment.

## Style

Use terse engineer prose:
- Problem first.
- Risk if stale.
- Fix made.
- No motivational filler.
