#!/usr/bin/env bash
# Replace UI screenshot baselines with the ones a CI run produced.
#
#   scripts/ui_baselines.sh RUN_ID [PLATFORM...]
#
# Downloads the ui-* artifacts of a cross-platform run and copies every
# compared/<platform>/<name>.png into uitests/baselines/<platform>/. Only run it
# after looking at the screenshots: a baseline is a statement that this is what
# the interface should look like. Commit the result together with the change
# that altered the interface, and put before/after images in the PR.
set -euo pipefail
run=${1:?usage: scripts/ui_baselines.sh RUN_ID [PLATFORM...]}
shift
root=$(git rev-parse --show-toplevel)
# A screenshot from another revision is not evidence for this working tree.
head=$(git rev-parse HEAD)
run_head=$(gh run view "$run" --json headSha -q .headSha)
[ "$head" = "$run_head" ] || { echo "run SHA differs from HEAD; review the correct revision first" >&2; exit 2; }
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "commit or preserve pending changes before updating baselines" >&2; exit 2; }
dir=$(mktemp -d)
trap 'rm -rf "$dir"' EXIT
gh run download "$run" -p 'ui-*' -D "$dir"
count=0
while IFS= read -r -d '' shot; do
    platform=$(basename "$(dirname "$shot")")
    if [ $# -gt 0 ] && ! printf '%s\n' "$@" | grep -qx "$platform"; then continue; fi
    mkdir -p "$root/uitests/baselines/$platform"
    cp "$shot" "$root/uitests/baselines/$platform/"
    echo "baseline: $platform/$(basename "$shot")"
    count=$((count + 1))
done < <(find "$dir" -path '*/compared/*' -name '*.png' -print0)
[ "$count" -gt 0 ] || { echo "no compared screenshots in run $run" >&2; exit 1; }
