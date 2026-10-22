#!/usr/bin/env bash
# For the maintainer, run on your own machine with your own gh login after you
# have read the PR yourself. Marks the PR's head commit as reviewed so a PR the
# automatic reviewer refuses (protected paths) can be merged.
# Usage: approve_pr.sh PR_NUMBER [REPO]
set -euo pipefail
pr=${1:?usage: approve_pr.sh PR_NUMBER [REPO]}
repo=${2:-WangMingCF/chamo}
sha=$(gh pr view "$pr" -R "$repo" --json headRefOid -q .headRefOid)
gh pr diff "$pr" -R "$repo" --name-only
read -r -p "确认已亲自审阅 #${pr}（${sha:0:12}）并放行？输入 yes：" answer
[ "$answer" = yes ] || { echo "已取消"; exit 1; }
gh api -X POST "repos/$repo/statuses/$sha" -f state=success -f context=agent-review \
    -f description="维护者亲自审核通过" >/dev/null
echo "已放行 #${pr}；合并仍需其余检查全绿。"
