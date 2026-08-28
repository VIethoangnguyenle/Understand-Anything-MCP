#!/usr/bin/env bash
#
# Pull tất cả repo nằm trực tiếp dưới $REPO_ROOT (maxdepth 2).
#
# stdout : JSON report (duy nhất) — parse được bằng jq
# stderr : log người đọc
# exit   : 0 luôn (kể cả có repo lỗi). Consumer đọc .summary để quyết định.
#          Xem git-pull-run.sh cho wrapper có alert + exit code.
#
set -uo pipefail

REPO_ROOT="${1:-/srv/ua-data}"
SKIP_FILE="${GIT_PULL_SKIP_FILE:-${HOME}/.git-pull-skip}"
RESULTS="[]"

if ! command -v jq &>/dev/null; then
    echo '{"error":"jq not installed"}' >&2
    exit 1
fi

# Skip list dạng chuỗi thay vì `declare -A`: bash 3.2 (macOS) không có
# associative array, giữ vậy để script test được ở mọi nơi.
# `|| [[ -n "$line" ]]` để không bỏ sót dòng cuối khi file thiếu newline.
SKIP_LIST=""
if [[ -f "$SKIP_FILE" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
        line=$(echo "$line" | xargs)
        [[ -z "$line" || "$line" == \#* ]] && continue
        SKIP_LIST="${SKIP_LIST}${line}"$'\n'
    done < "$SKIP_FILE"
fi
is_skipped() {
    [[ -n "$SKIP_LIST" ]] && printf '%s' "$SKIP_LIST" | grep -Fxq -- "$1"
}

# Log ra stderr để stdout giữ nguyên JSON thuần.
log_line() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >&2; }

# Mọi lệnh git chạm mạng phải đi qua đây — không chỉ `pull`. `ls-remote` dưới
# resolve_branch cũng cần proxy, thiếu là fail trên VM.
# ${var:-} bắt buộc: script chạy dưới set -u, systemd/cron có thể không set proxy.
git_net() {
    local d="$1"; shift
    git -C "$d" \
        -c credential.helper='store --file ~/.git-credentials' \
        -c http.proxy="${https_proxy:-}" \
        -c http.noProxy="${no_proxy:-}" \
        -c https.proxy="${https_proxy:-}" \
        -c https.noProxy="${no_proxy:-}" \
        "$@"
}

# Tên branch LOCAL không nhất thiết trùng tên trên remote, và nhiều clone trên VM
# không có upstream tracking. Thử lần lượt tới khi ra branch remote thật sự tồn tại:
#   1. upstream tracking      2. origin/HEAD local
#   3. hỏi thẳng remote       4. tên branch local (cuối cùng mới dùng)
resolve_branch() {
    local d="$1" b=""
    b=$(git -C "$d" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null) \
        && b="${b#*/}" || b=""
    [[ -z "$b" ]] && { b=$(git -C "$d" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null) \
        && b="${b#origin/}" || b=""; }
    [[ -z "$b" ]] && b=$(git_net "$d" ls-remote --symref origin HEAD 2>/dev/null \
        | awk '/^ref:/{sub("refs/heads/","",$2);print $2;exit}')
    [[ -z "$b" ]] && b=$(git -C "$d" symbolic-ref --short HEAD 2>/dev/null)
    # Verify branch có thật trên remote — chặn `couldn't find remote ref <x>`.
    [[ -n "$b" ]] && git_net "$d" ls-remote --exit-code --heads origin "$b" >/dev/null 2>&1 || return 1
    echo "$b"
}

add_result() {
    RESULTS=$(echo "$RESULTS" | jq --arg r "$1" --arg s "$2" --arg e "$3" --argjson c "$4" --arg b "${5:-}" \
        '. + [{"repo":$r,"status":$s,"branch":$b,"error":$e,"commits_pulled":$c}]')
}

log_line "=== git-pull started: $REPO_ROOT ==="

while IFS= read -r -d '' git_dir; do
    repo_dir="$(dirname "$git_dir")"
    repo_name="$(basename "$repo_dir")"
    commits=0

    if is_skipped "$repo_name"; then
        add_result "$repo_name" "skipped" "in skip list" 0 ""
        log_line "  [SKIP] $repo_name (skip list)"
        continue
    fi

    # Dọn state dở dang từ lần chạy trước.
    # git rev-parse KHÔNG có --is-inside-rebase/--is-inside-merge; phải check path.
    if [[ -d "$(git -C "$repo_dir" rev-parse --git-path rebase-merge 2>/dev/null)" ]] || \
       [[ -d "$(git -C "$repo_dir" rev-parse --git-path rebase-apply 2>/dev/null)" ]]; then
        git -C "$repo_dir" rebase --abort 2>/dev/null && log_line "  [ABORT] $repo_name (was in rebase)"
    fi
    if [[ -f "$(git -C "$repo_dir" rev-parse --git-path MERGE_HEAD 2>/dev/null)" ]]; then
        git -C "$repo_dir" merge --abort 2>/dev/null && log_line "  [ABORT] $repo_name (was in merge)"
    fi

    branch=$(resolve_branch "$repo_dir") || branch=""
    if [[ -z "$branch" ]]; then
        add_result "$repo_name" "skipped" "khong resolve duoc branch tren remote" 0 ""
        log_line "  [SKIP] $repo_name (khong resolve duoc branch tren remote)"
        continue
    fi

    # Working tree bẩn -> `pull --rebase` chắc chắn chết. Bắt TRƯỚC khi pull để
    # báo đúng nguyên nhân + liệt kê file, thay vì để nó lẫn vào nhánh `conflict`
    # (chuỗi "cannot pull with rebase" có chữ "rebase" nên grep bên dưới bắt trúng).
    dirty=$(git -C "$repo_dir" status --porcelain --untracked-files=no 2>/dev/null)
    if [[ -n "$dirty" ]]; then
        n=$(printf '%s\n' "$dirty" | wc -l | tr -d ' ')
        add_result "$repo_name" "dirty" \
            "$n file thay doi chua commit, pull --rebase se that bai:"$'\n'"$dirty" 0 "$branch"
        log_line "  [dirty] $repo_name @$branch — $n file thay doi chua commit, bo qua"
        continue
    fi

    before=$(git -C "$repo_dir" rev-parse HEAD 2>/dev/null || echo "")

    output=$(git_net "$repo_dir" pull --rebase origin "$branch" 2>&1)
    exit_code=$?

    if [[ $exit_code -eq 0 ]]; then
        after=$(git -C "$repo_dir" rev-parse HEAD 2>/dev/null || echo "")
        # Đếm commit bằng rev-list thay vì parse text output của git pull.
        if [[ -n "$before" && -n "$after" && "$before" != "$after" ]]; then
            commits=$(git -C "$repo_dir" rev-list --count "${before}..${after}" 2>/dev/null || echo 0)
            status="ok"
        else
            status="up-to-date"
        fi
    elif echo "$output" | grep -qi "conflict\|REBASE"; then
        status="conflict"
        git -C "$repo_dir" rebase --abort 2>/dev/null || true
    else
        status="failed"
    fi

    add_result "$repo_name" "$status" "$output" "${commits:-0}" "$branch"
    log_line "  [$status] $repo_name @$branch (${commits} commits)"
done < <(find "$REPO_ROOT" -maxdepth 2 -name ".git" -type d -print0)

echo "$RESULTS" | jq --arg ts "$(date -Iseconds)" '{
  timestamp: $ts,
  results: .,
  summary: {
    total: (. | length),
    ok: ([.[] | select(.status=="ok")] | length),
    up_to_date: ([.[] | select(.status=="up-to-date")] | length),
    skipped: ([.[] | select(.status=="skipped")] | length),
    dirty: ([.[] | select(.status=="dirty")] | length),
    conflict: ([.[] | select(.status=="conflict")] | length),
    failed: ([.[] | select(.status=="failed")] | length)
  }
}'

log_line "=== git-pull finished ==="
