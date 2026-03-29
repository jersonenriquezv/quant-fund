#!/bin/bash
# Claude Code pre-commit hook: warn when committing changes to critical services
# Used by .claude/settings.json hooks to trigger /review before commit

CRITICAL_PATHS="execution_service/ risk_service/"

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

exit 0
