#!/bin/bash
# Install versioned git hooks into .git/hooks/.
# Run from repo root: ./scripts/hooks/install.sh
# Re-run after pulling new hook versions.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
if [ -z "$REPO_ROOT" ]; then
    echo "ERROR: not inside a git repo." >&2
    exit 1
fi

SRC_DIR="${REPO_ROOT}/scripts/hooks"
DST_DIR="${REPO_ROOT}/.git/hooks"

if [ ! -d "$SRC_DIR" ]; then
    echo "ERROR: ${SRC_DIR} not found." >&2
    exit 1
fi

mkdir -p "$DST_DIR"

installed=0
for hook in pre-push pre-commit post-merge commit-msg prepare-commit-msg; do
    src="${SRC_DIR}/${hook}"
    [ -f "$src" ] || continue
    dst="${DST_DIR}/${hook}"
    cp "$src" "$dst"
    chmod +x "$dst"
    echo "installed: ${hook}"
    installed=$((installed + 1))
done

if [ "$installed" -eq 0 ]; then
    echo "No hooks found in ${SRC_DIR}."
    exit 0
fi

echo ""
echo "${installed} hook(s) installed into .git/hooks/."
echo "Run again after pulling new hook versions."
