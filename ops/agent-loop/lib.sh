# shellcheck shell=bash
# Shared helpers for run_loop.sh. Sourced, not executed.
#
# Everything is configured through environment variables, which is how a Docker
# container (e.g. on a Synology NAS) passes settings. Optionally CHAMO_ENV names
# a file of KEY=value lines to load first. Required: REPO, GH_TOKEN.
# The rails (this folder) should be mounted read-only into the container, so an
# agent that edits its own clone cannot loosen the scripts that drive it.

CHAMO_HOME=${CHAMO_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }

load_config() {
    if [ -n "${CHAMO_ENV:-}" ]; then
        [ -r "$CHAMO_ENV" ] || { log "cannot read $CHAMO_ENV"; exit 2; }
        set -a
        # shellcheck disable=SC1090
        . "$CHAMO_ENV"
        set +a
    fi
    : "${REPO:?REPO not set}" "${GH_TOKEN:?GH_TOKEN not set}"
    export GH_TOKEN GH_PROMPT_DISABLED=1 GH_NO_UPDATE_NOTIFIER=1
}

# Two kill switches: a file (create it on the NAS in a folder mounted into the
# container) and an open issue labelled agent-stop (on a public repository only
# collaborators can set labels, so strangers cannot stop or start the loop).
# If GitHub cannot be asked, treat it as "stop for now" rather than run blind.
stop_requested() {
    [ -e "${STOP_FILE:-/config/STOP}" ] && { log "stop file present"; return 0; }
    [ "${DRY_RUN:-0}" = 1 ] && return 1
    local open
    if ! open=$(gh issue list -R "$REPO" --label agent-stop --state open --json number -q 'length' 2>/dev/null); then
        log "cannot reach GitHub to check the stop label; skipping this cycle"
        return 0
    fi
    [ "${open:-0}" != 0 ] && { log "agent-stop issue is open"; return 0; }
    return 1
}

# Refuse to publish text that carries a private keyword or any secret value from
# the environment. Prints nothing about what matched beyond the scanner's numbers.
safe_to_publish() {  # safe_to_publish FILE
    local file=$1 secret
    for secret in "${GH_TOKEN:-}" "${MODEL_API_KEY:-}"; do
        [ -n "$secret" ] && grep -qF -- "$secret" "$file" && { log "secret value found in $file"; return 1; }
    done
    python3 "$CHAMO_HOME/privacy_scan.py" text < "$file"
}

publish_comment() {  # publish_comment ISSUE_OR_PR FILE  (falls back to a fixed notice)
    local target=$1 file=$2 notice
    if ! safe_to_publish "$file"; then
        notice=$(mktemp)
        printf '本次内容未发布：隐私或密钥扫描命中，请在服务器上查看 %s。\n' "$file" > "$notice"
        file=$notice
    fi
    if [ "${DRY_RUN:-0}" = 1 ]; then
        log "DRY_RUN comment to #$target:"; cat "$file" >&2; return 0
    fi
    gh issue comment "$target" -R "$REPO" -F "$file" >/dev/null || log "could not comment on #$target"
}

# Replace {{NAME}} placeholders with the values of the same-named variables.
render() {  # render FILE...
    python3 - "$@" <<'PY'
import os, re, sys
out = "\n\n".join(open(p, encoding="utf-8").read() for p in sys.argv[1:])
sys.stdout.write(re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: os.environ.get(m.group(1), m.group(0)), out))
PY
}
