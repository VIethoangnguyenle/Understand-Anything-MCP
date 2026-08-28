#!/usr/bin/env bash
#
# Quét mọi repo dưới $REPO_ROOT, kiểm tra `.understand-anything/` có bị repo con
# bỏ qua đúng cách không — để `git pull --rebase` không lỗi sau mỗi lần re-index.
#
# Ba trạng thái, mức độ nghiêm trọng khác nhau:
#
#   ok          .understand-anything/ đã bị ignore (qua .gitignore hoặc
#               .git/info/exclude). git pull không bao giờ vướng.
#
#   untracked   Chưa ignore nhưng cũng chưa track. git pull VẪN CHẠY, nhưng:
#                 - `git status` đầy nhiễu
#                 - `git add -A` sẽ commit graph vào repo sản phẩm
#                 - `git clean -fd` sẽ XOÁ SẠCH graph
#                 - pull lỗi nếu upstream thêm file trùng đường dẫn
#
#   tracked     Graph đang được repo con track. NGHIÊM TRỌNG NHẤT: mỗi lần
#               re-index là một lần local modification, và
#               `git pull --rebase` sẽ từ chối với
#               "cannot pull with rebase: You have unstaged changes".
#               Đây chính là thứ làm git-pull.sh báo `failed`.
#
# --fix xử lý cả hai, đều KHÔNG commit gì vào repo sản phẩm:
#
#   not-ignored -> ghi `.understand-anything/` vào .git/info/exclude
#                  (per-clone, không phải .gitignore, sống qua mọi lần pull)
#
#   tracked     -> `git update-index --skip-worktree` trên đúng các file đó.
#                  KHÔNG dùng `git rm --cached`: nó để lại staged deletion và
#                  pull vẫn chết với "Your index contains uncommitted changes"
#                  (đã kiểm chứng). skip-worktree làm status sạch, pull chạy,
#                  bản graph vừa re-index còn nguyên.
#
#                  Giới hạn: nếu upstream sửa chính file .understand-anything đó,
#                  pull sẽ báo "Your local changes would be overwritten by merge"
#                  và cần người vào xử lý. Chấp nhận được — repo sản phẩm gần như
#                  không bao giờ commit graph, và nếu có thì đó là điều cần biết.
#
# stdout : JSON report  |  stderr : log người đọc
# exit   : 0 = mọi repo ok   1 = có repo cần xử lý   2 = lỗi dùng sai
#
set -uo pipefail

REPO_ROOT="${1:-/srv/ua-data}"
DO_FIX=0
[[ "${2:-}" == "--fix" || "${1:-}" == "--fix" ]] && DO_FIX=1
[[ "${1:-}" == "--fix" ]] && REPO_ROOT="${2:-/srv/ua-data}"

TARGET=".understand-anything"

if ! command -v jq &>/dev/null; then
    echo '{"error":"jq not installed"}' >&2
    exit 2
fi
if [[ ! -d "$REPO_ROOT" ]]; then
    echo "{\"error\":\"REPO_ROOT khong ton tai: $REPO_ROOT\"}" >&2
    exit 2
fi

log_line() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >&2; }

RESULTS="[]"
add_result() {
    RESULTS=$(echo "$RESULTS" | jq \
        --arg r "$1" --arg s "$2" --arg m "$3" --arg f "$4" \
        '. + [{"repo":$r,"status":$s,"detail":$m,"fixed":($f=="1")}]')
}

log_line "=== check-graph-ignore: $REPO_ROOT (fix=$DO_FIX) ==="

while IFS= read -r -d '' git_dir; do
    repo_dir="$(dirname "$git_dir")"
    repo_name="$(basename "$repo_dir")"
    fixed=0

    # 1. Repo con có đang TRACK graph không? Nguy hiểm nhất — kiểm trước tiên.
    #    `ls-files` trả rỗng nếu không track file nào dưới đường dẫn đó.
    tracked_count=$(git -C "$repo_dir" ls-files -- "$TARGET" 2>/dev/null | wc -l | tr -d ' ')
    if [[ "$tracked_count" -gt 0 ]]; then
        if [[ "$DO_FIX" -eq 1 ]]; then
            # skip-worktree, KHÔNG rm --cached (xem ghi chú đầu file).
            git -C "$repo_dir" ls-files -z -- "$TARGET" \
                | xargs -0 -r git -C "$repo_dir" update-index --skip-worktree
            if [[ -z "$(git -C "$repo_dir" status --porcelain -- "$TARGET")" ]]; then
                add_result "$repo_name" "ok" "$tracked_count file dat skip-worktree" 1
                log_line "  [FIXED] $repo_name — skip-worktree tren $tracked_count file"
            else
                add_result "$repo_name" "tracked" "skip-worktree khong an hieu" 0
                log_line "  [FAIL] $repo_name — skip-worktree khong an hieu"
            fi
        else
            add_result "$repo_name" "tracked" "$tracked_count file dang duoc track" 0
            log_line "  [TRACKED] $repo_name — $tracked_count file, git pull SE LOI sau re-index"
        fi
        continue
    fi

    # 2. Đã bị ignore chưa? check-ignore soi cả .gitignore, .git/info/exclude,
    #    core.excludesFile — nên đây mới là câu trả lời thật, không phải grep .gitignore.
    #    Query PHẢI có "/" cuối: pattern `.understand-anything/` chỉ khớp khi git
    #    biết đường dẫn là thư mục, mà thư mục có thể chưa tồn tại ở clone mới.
    if git -C "$repo_dir" check-ignore -q "$TARGET/" 2>/dev/null; then
        src=$(git -C "$repo_dir" check-ignore -v "$TARGET/" 2>/dev/null | cut -f1)
        add_result "$repo_name" "ok" "${src:-ignored}" 0
        log_line "  [OK] $repo_name (${src:-ignored})"
        continue
    fi

    # 3. Chưa ignore, chưa track.
    if [[ "$DO_FIX" -eq 1 ]]; then
        excl="$(git -C "$repo_dir" rev-parse --git-path info/exclude 2>/dev/null)"
        # rev-parse --git-path trả đường dẫn tương đối với repo_dir
        [[ "$excl" != /* ]] && excl="$repo_dir/$excl"
        mkdir -p "$(dirname "$excl")"
        printf '\n# them boi check-graph-ignore.sh — graph la runtime state, khong commit\n%s/\n' \
            "$TARGET" >> "$excl"
        if git -C "$repo_dir" check-ignore -q "$TARGET/" 2>/dev/null; then
            add_result "$repo_name" "ok" "vua them vao .git/info/exclude" 1
            log_line "  [FIXED] $repo_name — them vao .git/info/exclude"
            fixed=1
        else
            add_result "$repo_name" "not-ignored" "them vao info/exclude nhung van khong ignore" 0
            log_line "  [FAIL] $repo_name — fix khong an hieu"
        fi
    else
        has_dir="khong co thu muc $TARGET"
        [[ -d "$repo_dir/$TARGET" ]] && has_dir="co $TARGET nhung chua ignore"
        add_result "$repo_name" "not-ignored" "$has_dir" 0
        log_line "  [NOT-IGNORED] $repo_name — $has_dir"
    fi
done < <(find "$REPO_ROOT" -maxdepth 2 -name ".git" -type d -print0)

echo "$RESULTS" | jq --arg ts "$(date -Iseconds)" '{
  timestamp: $ts,
  results: .,
  summary: {
    total:       (. | length),
    ok:          ([.[] | select(.status=="ok")]          | length),
    not_ignored: ([.[] | select(.status=="not-ignored")] | length),
    tracked:     ([.[] | select(.status=="tracked")]     | length),
    fixed:       ([.[] | select(.fixed)]                 | length)
  }
}'

bad=$(echo "$RESULTS" | jq '[.[] | select(.status!="ok")] | length')
if [[ "$bad" -gt 0 ]]; then
    log_line "=== $bad repo can xu ly ==="
    echo "$RESULTS" | jq -r '.[] | select(.status=="not-ignored") | .repo' | while read -r r; do
        log_line "  chua ignore: $r   -> chay lai voi --fix"
    done
    echo "$RESULTS" | jq -r '.[] | select(.status=="tracked") | .repo' | while read -r r; do
        log_line "  dang track:  $r   -> chay lai voi --fix (skip-worktree)."
        log_line "                    KHONG dung 'git rm --cached': de lai staged deletion, pull van chet."
    done
    exit 1
fi

log_line "=== tat ca repo deu ok ==="
exit 0
