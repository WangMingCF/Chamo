#!/usr/bin/env bash
# Run once when the agent's container starts (or as the first step of a round).
# Prepares git so every commit and push goes through the rails, clones the
# repository if needed, and checks that the tools the loop relies on exist.
# It changes nothing outside the container's own git config and $WORKDIR.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
: "${REPO:?REPO not set}" "${GH_TOKEN:?GH_TOKEN not set}" "${WORKDIR:?WORKDIR not set}"
problems=0
need() { command -v "$1" >/dev/null || { echo "missing tool: $1 ($2)"; problems=$((problems + 1)); }; }
need git "commits"
need python3 "the pre-push privacy scan"
need gh "PRs, CI results and remote test runs; the static gh binary can be mounted into PATH"
[ "$problems" = 0 ] || exit 1

# No real identity ever exists in this container.
git config --global user.name Chamo
git config --global user.email dev@chamo.invalid
git config --global commit.gpgsign false
git config --global core.hooksPath "$HERE/hooks"
git config --global credential.https://github.com.helper '!gh auth git-credential'

kw=${CHAMO_PRIVATE_KEYWORDS:-$HOME/.config/chamo-agent/private-keywords.txt}
if ! python3 "$HERE/privacy_scan.py" --keywords "$kw" text < /dev/null; then
    echo "privacy keyword file missing or invalid: pushes will be refused until it is mounted at $kw"
    exit 1
fi

if [ ! -d "$WORKDIR/.git" ]; then
    git clone -q "https://github.com/$REPO.git" "$WORKDIR"
fi
gh api "repos/$REPO" --jq '"repository reachable: " + .full_name' 2>/dev/null \
    || { echo "GH_TOKEN cannot read $REPO"; exit 1; }
echo "ready: commits as Chamo <dev@chamo.invalid>, pushes scanned by $HERE/hooks/pre-push"
