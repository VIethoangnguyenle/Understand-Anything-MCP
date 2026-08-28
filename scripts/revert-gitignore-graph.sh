#!/usr/bin/env bash
#
# Hoàn tác các sửa tay `.gitignore` chỉ nhằm thêm `.understand-anything`, rồi
# chuyển quy tắc đó sang `.git/info/exclude`.
#
# Vì sao cần: thêm vào `.gitignore` có tác dụng ignore, nhưng để lại thay đổi
# chưa commit vĩnh viễn trong repo sản phẩm → `git pull --rebase` chết mãi với
# "cannot pull with rebase: You have unstaged changes". Dùng info/exclude cho
# kết quả ignore y hệt mà working tree vẫn sạch, không cần mở MR.
#
# MẶC ĐỊNH LÀ DRY-RUN. Phải truyền --apply mới thực sự ghi.
#
# Chỉ revert khi diff an toàn tuyệt đối:
#   1. Không dòng nào THỰC SỰ bị mất (removed - added = rỗng).
#      Điều kiện tập-hợp này nuốt được artifact "\ No newline at end of file",
#      thứ làm dòng cuối hiện thành 1 xoá + 1 thêm cùng nội dung.
#   2. Mọi dòng THỰC SỰ thêm vào đều là rỗng / comment / understand-anything.
# Repo không thoả -> bỏ qua và in rõ lý do. Không bao giờ revert đoán mò.
#
# stdout : JSON report  |  stderr : log người đọc
# exit   : 0 = xong     1 = có repo bị bỏ qua     2 = lỗi dùng sai
#
set -uo pipefail

REPO_ROOT=""
APPLY=0
for a in "$@"; do
    case "$a" in
        --apply) APPLY=1 ;;
        -*) echo "tham so la: $a" >&2; exit 2 ;;
        *) REPO_ROOT="$a" ;;
    esac
done
REPO_ROOT="${REPO_ROOT:-/srv/ua-data}"

command -v jq &>/dev/null || { echo '{"error":"jq not installed"}' >&2; exit 2; }
[[ -d "$REPO_ROOT" ]] || { echo "{\"error\":\"khong co $REPO_ROOT\"}" >&2; exit 2; }

log_line() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >&2; }
RESULTS="[]"
add_result() {
    RESULTS=$(echo "$RESULTS" | jq --arg r "$1" --arg s "$2" --arg m "$3" \
        '. + [{"repo":$r,"status":$s,"detail":$m}]')
}

# Trả 0 nếu an toàn; in lý do ra stdout nếu không.
safe_to_revert() {
    local d="$1" rm_l add_l gone new l
    rm_l=$(git -C "$d" diff -U0 -- .gitignore | grep '^-[^-]' | sed 's/^-//')
    add_l=$(git -C "$d" diff -U0 -- .gitignore | grep '^+[^+]' | sed 's/^+//')
    gone=$(comm -23 <(printf '%s\n' "$rm_l" | sort -u) <(printf '%s\n' "$add_l" | sort -u))
    if [[ -n "${gone// }" ]]; then
        echo "se mat dong: $(printf '%s' "$gone" | tr '\n' '|')"; return 1
    fi
    new=$(comm -13 <(printf '%s\n' "$rm_l" | sort -u) <(printf '%s\n' "$add_l" | sort -u))
    while IFS= read -r l; do
        [[ -z "${l// }" || "$l" == \#* || "$l" == *understand-anything* ]] && continue
        echo "co dong la: $l"; return 1
    done <<< "$new"
    return 0
}

log_line "=== revert-gitignore-graph: $REPO_ROOT (apply=$APPLY) ==="
[[ "$APPLY" -eq 0 ]] && log_line "    DRY-RUN — khong ghi gi. Them --apply de thuc thi."

while IFS= read -r -d '' git_dir; do
    repo_dir="$(dirname "$git_dir")"
    repo_name="$(basename "$repo_dir")"

    if git -C "$repo_dir" diff --quiet -- .gitignore 2>/dev/null; then
        continue   # .gitignore sạch, không phải việc của script này
    fi

    if ! reason=$(safe_to_revert "$repo_dir"); then
        add_result "$repo_name" "skipped" "$reason"
        log_line "  [SKIP] $repo_name — $reason"
        continue
    fi

    if [[ "$APPLY" -eq 0 ]]; then
        add_result "$repo_name" "would-revert" "diff chi them .understand-anything"
        log_line "  [DRY] $repo_name — se revert .gitignore + them vao info/exclude"
        continue
    fi

    git -C "$repo_dir" checkout -- .gitignore || {
        add_result "$repo_name" "failed" "checkout -- .gitignore that bai"
        log_line "  [FAIL] $repo_name — checkout that bai"; continue
    }
    # Bù lại quy tắc ignore ở info/exclude (per-clone, không cần commit).
    if ! git -C "$repo_dir" check-ignore -q .understand-anything/ 2>/dev/null; then
        excl="$(git -C "$repo_dir" rev-parse --git-path info/exclude)"
        [[ "$excl" != /* ]] && excl="$repo_dir/$excl"
        mkdir -p "$(dirname "$excl")"
        printf '\n# graph la runtime state — xem scripts/README.md\n.understand-anything/\n' >> "$excl"
    fi
    if git -C "$repo_dir" check-ignore -q .understand-anything/ 2>/dev/null \
       && git -C "$repo_dir" diff --quiet -- .gitignore 2>/dev/null; then
        add_result "$repo_name" "reverted" "gitignore sach + ignore qua info/exclude"
        log_line "  [OK] $repo_name — .gitignore sach, ignore chuyen sang info/exclude"
    else
        add_result "$repo_name" "failed" "sau khi revert van khong dat trang thai mong muon"
        log_line "  [FAIL] $repo_name — trang thai cuoi khong dung"
    fi
done < <(find "$REPO_ROOT" -maxdepth 2 -name ".git" -type d -print0)

echo "$RESULTS" | jq --arg ts "$(date -Iseconds)" --argjson ap "$APPLY" '{
  timestamp: $ts, applied: ($ap == 1), results: .,
  summary: {
    total:        (. | length),
    reverted:     ([.[] | select(.status=="reverted")]     | length),
    would_revert: ([.[] | select(.status=="would-revert")] | length),
    skipped:      ([.[] | select(.status=="skipped")]      | length),
    failed:       ([.[] | select(.status=="failed")]       | length)
  }
}'

log_line "=== xong ==="
[[ "$(echo "$RESULTS" | jq '[.[] | select(.status=="skipped" or .status=="failed")] | length')" -gt 0 ]] && exit 1
exit 0
