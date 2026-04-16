#!/bin/bash
# Claude Code pre-commit hook: warn when committing changes to critical services
# and block stale operational docs for code paths that define system truth.
# Used by .claude/settings.json hooks.

CRITICAL_PATHS="execution_service/ risk_service/"
DOC_TRUTH_PATHS="config/settings.py data_service/ main.py strategy_service/ risk_service/ execution_service/ docs/SYSTEM_BASELINE.md docs/OPERATIONS.md docs/context/ telegram_bot/"

# Get staged files
staged=$(git diff --cached --name-only 2>/dev/null)
if [ -z "$staged" ]; then
    # No staged files, check what would be committed (unstaged changes)
    staged=$(git diff --name-only 2>/dev/null)
fi

for path in $CRITICAL_PATHS; do
    if echo "$staged" | grep -q "^$path"; then
        echo "CRITICAL SERVICE CHANGE DETECTED: files in $path are being committed."
        echo "Run /review before committing changes to execution_service/ or risk_service/."
        echo "These services handle real money — review is mandatory."
        exit 1
    fi
done

for path in $DOC_TRUTH_PATHS; do
    if echo "$staged" | grep -q "^$path"; then
        echo "DOC TRUTH CHECK: code that defines system truth changed."
        python3 scripts/check_docs_truth.py || exit 1
        break
    fi
done

exit 0
