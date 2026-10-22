#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Independent review, run as a separate Unix user with its own GitHub token (the
# only token allowed to write commit statuses). For every open, non-draft PR from
# a branch of this repository whose head commit has no review status yet, it:
#   1. refuses PRs that touch protected paths (the loop's own rails, CI, licenses):
#      those need the maintainer, who sets the status by hand (approve_pr.sh);
#   2. refuses oversized diffs and diffs whose added lines hit the privacy scan;
#   3. asks the review model for a verdict, posts the review as a PR comment and
#      sets the required "agent-review" status on the head commit.
# Fork PRs are skipped entirely: their text is untrusted and never reaches a model.
#
# Usage: review_prs.sh            DRY_RUN=1 prints instead of writing to GitHub.
set -uo pipefail
# shellcheck source=lib.sh
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"
load_config review
: "${REVIEW_CMD:?REVIEW_CMD not set}"
CONTEXT=${REVIEW_CONTEXT:-agent-review}
PROTECTED=${PROTECTED_PATHS:-'^(ops/agent-loop/|\.github/|LICENSE|docs/LICENSES\.txt|engine/THIRD_PARTY_NOTICES\.md)'}
STATE=${CHAMO_STATE:-$HOME/.local/state/chamo-review}
mkdir -p "$STATE"

stop_requested && exit 0

set_status() {  # set_status SHA STATE DESCRIPTION
    local desc=${3:0:139}
    if [ "${DRY_RUN:-0}" = 1 ]; then log "DRY_RUN status $1 $2: $desc"; return 0; fi
    gh api -X POST "repos/$REPO/statuses/$1" -f state="$2" -f context="$CONTEXT" \
        -f description="$desc" >/dev/null || log "could not set status on $1"
}

prs=$(gh pr list -R "$REPO" --state open --limit 30 \
    --json number,headRefOid,isDraft,isCrossRepository \
    -q '.[] | select((.isDraft | not) and (.isCrossRepository | not)) | "\(.number) \(.headRefOid)"') || {
    log "cannot list PRs"; exit 1; }

while read -r number sha; do
    [ -n "${number:-}" ] || continue
    seen=$(gh api "repos/$REPO/commits/$sha/statuses" \
        -q "[.[] | select(.context == \"$CONTEXT\")] | length" 2>/dev/null || echo error)
    [ "$seen" = 0 ] || continue   # already reviewed (or unknown): never review a commit twice

    work="$STATE/pr-$number-${sha:0:12}"; mkdir -p "$work"
    log "reviewing #$number at ${sha:0:12}"
    set_status "$sha" pending "评审中"

    gh pr diff "$number" -R "$REPO" --name-only > "$work/files.txt" || { set_status "$sha" error "无法读取改动文件列表"; continue; }
    if grep -Eq "$PROTECTED" "$work/files.txt"; then
        set_status "$sha" failure "改动受保护路径，需要维护者亲自审核"
        printf '这个 PR 改动了受保护路径（自循环规则、CI、许可证）。自动评审不放行，需要维护者亲自审核；确认后由维护者运行 approve_pr.sh。\n\n受保护文件：\n%s\n' \
            "$(grep -E "$PROTECTED" "$work/files.txt" | sed 's/^/- /')" > "$work/comment.md"
        publish_comment "$number" "$work/comment.md"
        continue
    fi

    gh pr diff "$number" -R "$REPO" > "$work/diff.patch" || { set_status "$sha" error "无法读取 diff"; continue; }
    if [ "$(wc -c < "$work/diff.patch")" -gt "${MAX_DIFF_BYTES:-400000}" ]; then
        set_status "$sha" failure "diff 过大，请拆成更聚焦的 PR"; continue
    fi
    grep -E '^\+' "$work/diff.patch" | grep -vE '^\+\+\+ ' > "$work/added.txt" || true
    gh pr view "$number" -R "$REPO" --json title,body -q '.title + "\n\n" + .body' > "$work/pr.txt"
    if ! cat "$work/added.txt" "$work/pr.txt" | python3 "$CHAMO_HOME/privacy_scan.py" text; then
        set_status "$sha" failure "隐私扫描命中，已拦截"; continue
    fi

    export PR_NUMBER=$number PR_SHA=$sha
    { render "$CHAMO_HOME/prompts/review.md"
      printf '\n\n## PR 标题与说明\n\n'; cat "$work/pr.txt"
      printf '\n\n## Diff\n\n```diff\n'; cat "$work/diff.patch"; printf '\n```\n'; } > "$work/prompt.md"
    if ! timeout --kill-after=60 "${REVIEW_TIMEOUT:-1800}" bash -c "$REVIEW_CMD" \
            < "$work/prompt.md" > "$work/review.md" 2> "$work/review.log"; then
        set_status "$sha" error "评审模型运行失败或超时"; continue
    fi
    verdict=$(grep -oE '^VERDICT: (APPROVE|REQUEST_CHANGES)\s*$' "$work/review.md" | tail -1 | awk '{print $2}')
    { printf '自动评审（评审身份与开发 Agent 分离，提交 %s）\n\n' "${sha:0:12}"; cat "$work/review.md"; } > "$work/comment.md"
    publish_comment "$number" "$work/comment.md"
    case "$verdict" in
        APPROVE) set_status "$sha" success "自动评审通过" ;;
        REQUEST_CHANGES) set_status "$sha" failure "自动评审要求修改，见 PR 评论" ;;
        *) set_status "$sha" error "评审输出缺少 VERDICT 行" ;;
    esac
done <<< "$prs"
