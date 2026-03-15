Review uncommitted changes. Act as @reviewer (see `.claude/agents/reviewer.md`).

## Steps

1. Run `git diff` and `git diff --cached` to get the full diff. This IS your input — do NOT read unchanged files.
2. Only read additional files if the diff references fields/functions you can't verify from context:
   - Model fields → read `shared/models.py`
   - Settings thresholds → read `config/settings.py`
   - Called functions → read only the specific function, not the whole file
3. Review through both lenses (correctness + operational impact) per the reviewer checklist.

## Output

```
## Verdict: APPROVE | NEEDS CHANGES | REJECT

### Issues
1. [CRITICAL/WARNING/INFO] [file:line] Description → Fix: ...

### Operational Impact
[Any concerns about trade frequency, rejection paths, complexity]

### Good
- ...
```

Do NOT read files that aren't touched by the diff or directly referenced by changed code.
