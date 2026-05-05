#!/bin/bash
# Bundle ops signals into a single token-efficient snapshot for plain-ops-brief.
# Each section is fenced with === MARKERS === so the skill can parse cleanly.
# Failures are captured, never abort the run.
#
# Usage:
#   bash scripts/ops_brief.sh                    # full snapshot to stdout
#   bash scripts/ops_brief.sh > /tmp/brief.txt   # save then paste to Claude
#
# Sections emitted (in order):
#   META            — host, branch, commit, timestamp
#   DOCKER          — docker compose ps (services + state)
#   ENGINE1_SHADOW  — scripts/report_engine1_shadow.py output
#   DOCS_TRUTH      — scripts/check_docs_truth.py output + exit code
#   GIT_STATUS      — git status -sb (porcelain) + ahead/behind
#
# After running, paste the output to Claude and let plain-ops-brief summarize.

set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

MAX_LINES_REPORT=120   # truncate very long script outputs
MAX_LINES_DOCS=80
MAX_LINES_GIT=60

section() {
    printf '\n=== %s ===\n' "$1"
}

run_capture() {
    # $1 = label (printed if cmd absent), rest = command
    local label="$1"; shift
    if ! command -v "$1" >/dev/null 2>&1 && [ ! -x "$1" ]; then
        printf '[skip] %s not available: %s\n' "$label" "$1"
        return
    fi
    "$@" 2>&1
    local rc=$?
    printf '\n[exit=%d]\n' "$rc"
}

truncate_output() {
    # $1 = max lines
    local max=$1
    awk -v max="$max" '
        { buf[NR] = $0 }
        END {
            if (NR <= max) { for (i=1;i<=NR;i++) print buf[i]; exit }
            head = int(max*0.7); tail = max - head
            for (i=1;i<=head;i++) print buf[i]
            printf "... [truncated %d lines] ...\n", NR - head - tail
            for (i=NR-tail+1;i<=NR;i++) print buf[i]
        }'
}

# --- META ---
section META
printf 'host        : %s\n' "$(hostname)"
printf 'cwd         : %s\n' "$REPO_ROOT"
printf 'timestamp   : %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
printf 'branch      : %s\n' "$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
printf 'commit      : %s\n' "$(git rev-parse --short HEAD 2>/dev/null || echo '?')"

# --- DOCKER ---
section DOCKER
if command -v docker >/dev/null 2>&1; then
    docker compose ps 2>&1
    printf '\n[exit=%d]\n' $?
else
    printf '[skip] docker not installed\n'
fi

# --- ENGINE1_SHADOW ---
section ENGINE1_SHADOW
if [ -f scripts/report_engine1_shadow.py ]; then
    {
        python scripts/report_engine1_shadow.py 2>&1
        printf '\n[exit=%d]\n' $?
    } | truncate_output "$MAX_LINES_REPORT"
else
    printf '[skip] scripts/report_engine1_shadow.py not found\n'
fi

# --- DOCS_TRUTH ---
section DOCS_TRUTH
if [ -f scripts/check_docs_truth.py ]; then
    {
        python scripts/check_docs_truth.py 2>&1
        printf '\n[exit=%d]\n' $?
    } | truncate_output "$MAX_LINES_DOCS"
else
    printf '[skip] scripts/check_docs_truth.py not found\n'
fi

# --- GIT_STATUS ---
section GIT_STATUS
{
    git status -sb 2>&1
    printf '\n--- ahead/behind ---\n'
    git rev-list --left-right --count HEAD...@{u} 2>/dev/null \
        | awk '{printf "ahead=%s behind=%s\n", $1, $2}' \
        || printf 'no upstream\n'
    printf '\n[exit=%d]\n' $?
} | truncate_output "$MAX_LINES_GIT"

section END
printf 'Paste this output to Claude and request the plain-ops-brief skill.\n'
