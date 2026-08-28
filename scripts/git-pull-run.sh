#!/usr/bin/env bash
#
# Wrapper cho systemd: chạy git-pull.sh, lưu JSON + log, alert khi có lỗi.
#
# exit 0 : mọi repo ok/up-to-date/skipped
# exit 1 : có repo conflict hoặc failed  → systemd mark service failed
# exit 2 : script không chạy được / JSON hỏng
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${GIT_PULL_REPO_ROOT:-/srv/ua-data}"
LOG_DIR="${GIT_PULL_LOG_DIR:-/var/log/ua-deploy}"
KEEP="${GIT_PULL_KEEP_RUNS:-30}"
ALERT_WEBHOOK="${GIT_PULL_ALERT_WEBHOOK:-}"

mkdir -p "$LOG_DIR"
stamp="$(date '+%Y%m%dT%H%M%S')"
json_file="$LOG_DIR/git-pull-$stamp.json"
log_file="$LOG_DIR/git-pull-$stamp.log"

"$SCRIPT_DIR/git-pull.sh" "$REPO_ROOT" >"$json_file" 2>"$log_file"

if ! jq -e . "$json_file" >/dev/null 2>&1; then
    echo "git-pull.sh không trả JSON hợp lệ — xem $log_file" >&2
    tail -20 "$log_file" >&2
    exit 2
fi

ln -sfn "$json_file" "$LOG_DIR/latest.json"
ln -sfn "$log_file"  "$LOG_DIR/latest.log"

# Prune: giữ $KEEP lần chạy gần nhất. Tên file có timestamp nên sort theo tên
# là đủ — tránh `find -printf` / `xargs -r` (GNU-only).
prune() {
    ls -1 "$LOG_DIR"/git-pull-*."$1" 2>/dev/null | sort -r | tail -n +$((KEEP + 1)) \
        | while IFS= read -r old; do rm -f "$old"; done
}
prune json
prune log

summary=$(jq -c '.summary' "$json_file")
bad=$(jq '.summary.conflict + .summary.failed + (.summary.dirty // 0)' "$json_file")
echo "git-pull summary: $summary"

if [[ "$bad" -gt 0 ]]; then
    detail=$(jq -r '.results[] | select(.status=="conflict" or .status=="failed" or .status=="dirty")
                    | "  - \(.repo) [\(.status)]: \(.error | split("\n") | .[0:3] | join(" / "))"' "$json_file")
    echo "git-pull có $bad repo lỗi:" >&2
    echo "$detail" >&2

    if [[ -n "$ALERT_WEBHOOK" ]]; then
        jq -n --argjson s "$summary" --arg d "$detail" --arg h "$(hostname)" \
            '{host:$h, summary:$s, detail:$d}' \
            | curl -sS -m 15 -X POST -H 'Content-Type: application/json' -d @- "$ALERT_WEBHOOK" >/dev/null \
            || echo "alert webhook thất bại" >&2
    fi
    exit 1
fi

exit 0
