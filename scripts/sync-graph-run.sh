#!/usr/bin/env bash
#
# Wrapper cho cron: pull code rồi index lại graph cho repo có thay đổi.
#
# Chạy HẰNG NGÀY, không phải hằng tuần. Số liệu thực đo: 34 file đổi mất 26
# phút, còn 791 file thì vượt khả năng của một lượt. Sync càng thưa, delta càng
# lớn, càng dễ vượt ngưỡng batch mà model đi hết được. Đây là điểm ngược trực
# giác nhất của hệ thống này.
#
# LUÔN dùng --partial, KHÔNG BAO GIỜ --full. Ngưỡng 30 file của plugin giả
# định rebuild rẻ; với đa số model thì rebuild vừa đắt (~54 phút) vừa bỏ dở
# (lên lịch 102 batch, chỉ chạy 3) và ghi đè summary tốt bằng template.
#
# exit 0 : mọi repo ổn
# exit 1 : có repo cần can thiệp (degraded / timeout / failed / too-big)
# exit 2 : script không chạy được / JSON hỏng
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SYNC_GRAPH_RUN_LOG_DIR:-/var/log/ua-deploy}"
KEEP="${SYNC_GRAPH_KEEP_RUNS:-14}"
MAX_CHANGED="${SYNC_GRAPH_RUN_MAX_CHANGED:-250}"
ALERT_WEBHOOK="${SYNC_GRAPH_ALERT_WEBHOOK:-}"
SKIP_PULL="${SYNC_GRAPH_SKIP_PULL:-0}"

mkdir -p "$LOG_DIR" || exit 2
stamp="$(date '+%Y%m%dT%H%M%S')"
json_file="$LOG_DIR/sync-graph-$stamp.json"
log_file="$LOG_DIR/sync-graph-$stamp.log"

{
    echo "=== sync-graph-run $stamp ==="

    # Pull trước: index code cũ thì graph mới cũng cũ theo.
    if [[ "$SKIP_PULL" -eq 0 ]]; then
        echo "--- git-pull ---"
        "$SCRIPT_DIR/git-pull.sh" 2>&1 | tail -40
    fi
    echo "--- sync-graph ---"
} >"$log_file" 2>&1

"$SCRIPT_DIR/sync-graph.sh" --apply --partial --domain \
    --max-changed="$MAX_CHANGED" >"$json_file" 2>>"$log_file"

if ! jq -e . "$json_file" >/dev/null 2>&1; then
    echo "sync-graph.sh không trả JSON hợp lệ — xem $log_file" >&2
    tail -20 "$log_file" >&2
    exit 2
fi

ln -sfn "$json_file" "$LOG_DIR/sync-graph-latest.json"
ln -sfn "$log_file"  "$LOG_DIR/sync-graph-latest.log"

# Tên file có timestamp nên sort theo tên là đủ — tránh `find -printf` (GNU-only).
prune() {
    ls -1 "$LOG_DIR"/sync-graph-????????T??????."$1" 2>/dev/null | sort -r \
        | tail -n "+$((KEEP + 1))" | while IFS= read -r old; do rm -f "$old"; done
}
prune json
prune log

summary=$(jq -c '.summary' "$json_file")
echo "sync-graph summary: $summary"

bad=$(jq '[.summary.degraded, .summary.timeout, .summary.failed, .summary.too_big]
          | map(. // 0) | add' "$json_file")

if [[ "$bad" -gt 0 ]]; then
    detail=$(jq -r '.results[]
        | select(.status=="degraded" or .status=="timeout"
                 or .status=="failed" or .status=="too-big")
        | "  - \(.repo) [\(.status)]: \(.detail | split("\n") | .[0:2] | join(" / "))"' "$json_file")
    echo "sync-graph có $bad repo cần can thiệp:" >&2
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
