#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Development loop for one agent on the VPS. Each cycle: check the kill switches
# and limits, run one bounded round of the harness with the goal prompt, then
# post the agent's round report to the status issue.
#
# Usage: run_loop.sh [--once]       DRY_RUN=1 skips every GitHub call.
set -uo pipefail
# shellcheck source=lib.sh
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"
load_config agent
: "${HARNESS_CMD:?HARNESS_CMD not set}" "${WORKDIR:?WORKDIR not set}"

ONCE=0; [ "${1:-}" = --once ] && ONCE=1
STATE=${CHAMO_STATE:-$HOME/.local/state/chamo-agent}
mkdir -p "$STATE/rounds"

pause() { [ "$ONCE" = 1 ] && exit 0; sleep "$1"; }

while :; do
    if stop_requested; then pause 600; continue; fi

    today=$(date -u +%F)
    count=$(cat "$STATE/count-$today" 2>/dev/null || echo 0)
    if [ "$count" -ge "${MAX_ROUNDS_PER_DAY:-6}" ]; then
        log "daily round limit reached ($count)"; pause 1800; continue
    fi

    # After repeated failures the loop parks itself until a person looks.
    failures=$(cat "$STATE/failures" 2>/dev/null || echo 0)
    if [ "$failures" -ge "${MAX_CONSECUTIVE_FAILURES:-3}" ]; then
        if [ ! -e "$STATE/paused-notified" ]; then
            note=$(mktemp)
            printf '自循环已暂停：连续 %s 轮失败。排查后在服务器上删除 %s 即可恢复。\n' "$failures" "$STATE/failures" > "$note"
            publish_comment "${STATUS_ISSUE:?}" "$note" && touch "$STATE/paused-notified"
        fi
        pause 1800; continue
    fi

    # A fresh directory per round, so a report left by an earlier round is never reused.
    dir=$(mktemp -d "$STATE/rounds/$(date -u +%Y%m%dT%H%M%SZ)-XXXX")
    export ROUND_ID; ROUND_ID=$(basename "$dir")
    export ROUND_REPORT="$dir/report.md" RELEASE_MODE="${RELEASE_MODE:-draft}" REPO WORKDIR
    echo $((count + 1)) > "$STATE/count-$today"   # counted up front so crashes count too

    render "$CHAMO_HOME/prompts/goal.md" "$CHAMO_HOME/prompts/round.md" > "$dir/prompt.md"
    log "round $ROUND_ID starting"
    if (cd "$WORKDIR" && timeout --kill-after=120 "${ROUND_TIMEOUT:-14400}" \
            bash -c "$HARNESS_CMD" < "$dir/prompt.md" > "$dir/harness.log" 2>&1); then rc=0; else rc=$?; fi

    if [ "$rc" = 0 ] && [ -s "$ROUND_REPORT" ]; then
        echo 0 > "$STATE/failures"; rm -f "$STATE/paused-notified"
        body=$ROUND_REPORT
    else
        echo $((failures + 1)) > "$STATE/failures"
        body="$dir/failure.md"
        # The harness log is never posted: it can hold anything the agent saw.
        printf '第 %s 轮失败：退出码 %s%s。日志在服务器 %s。\n' "$ROUND_ID" "$rc" \
            "$([ "$rc" = 124 ] && echo '（超时）')$([ -s "$ROUND_REPORT" ] || echo '，没有写出本轮报告')" \
            "$dir/harness.log" > "$body"
    fi
    log "round $ROUND_ID finished rc=$rc"
    publish_comment "${STATUS_ISSUE:?}" "$body"
    pause "${ROUND_INTERVAL:-900}"
done
