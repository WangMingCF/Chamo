#!/usr/bin/env bash
# Run tests on GitHub's Windows/macOS/Linux runners from any machine with gh,
# wait for the result, and print the per-platform summary. Meant for agents
# working on one OS that must verify behaviour on the others.
#
#   scripts/ci_remote_test.sh [--platforms core|extended|linux|windows|macos]
#                             [--repeat N] [--filter EXPR] [--ref BRANCH] [TESTS...]
#
# The branch must be pushed first: the runners test what is on GitHub, not the
# local working tree. Exit status is the run's verdict (0 only if every platform
# passed with no failures, flaky tests or unregistered skips).
set -euo pipefail
platforms=core repeat=1 filter="" ref=$(git rev-parse --abbrev-ref HEAD)
while [ $# -gt 0 ]; do
    case "$1" in
        --platforms) platforms=$2; shift 2 ;;
        --repeat) repeat=$2; shift 2 ;;
        --filter) filter=$2; shift 2 ;;
        --ref) ref=$2; shift 2 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) break ;;
    esac
done
tests=${*:-tests}

if [ -n "$(git status --porcelain --untracked-files=no)" ] || \
   [ "$(git rev-parse HEAD)" != "$(git rev-parse "origin/$ref" 2>/dev/null || echo none)" ]; then
    echo "note: local changes or unpushed commits are NOT tested; runners use origin/$ref" >&2
fi

request_id=$(python3 -c 'import uuid; print(uuid.uuid4())')
gh workflow run test-matrix.yml --ref "$ref" -f platforms="$platforms" -f tests="$tests" \
    -f filter="$filter" -f repeat="$repeat" -f request_id="$request_id"

# The dispatch call returns no run id; find the run it created.
run=""
for _ in $(seq 1 30); do
    run=$(gh run list --workflow test-matrix.yml --branch "$ref" --event workflow_dispatch -L 100 \
        --json databaseId,displayTitle -q "[.[] | select(.displayTitle == \"test-matrix $request_id\")][0].databaseId // empty")
    [ -n "$run" ] && break
    sleep 4
done
[ -n "$run" ] || { echo "could not find the dispatched run" >&2; exit 2; }
echo "run: $(gh run view "$run" --json url -q .url)" >&2

status=0
gh run watch "$run" --interval 30 --exit-status > /dev/null || status=$?

dir=$(mktemp -d)
if gh run download "$run" -n summary -D "$dir" 2>/dev/null; then
    cat "$dir/summary.md"
else
    gh run view "$run" --log-failed | tail -60
fi
exit "$status"
