#!/usr/bin/env bash
#
# Clone các repo trong repos.csv vào $REPO_ROOT. Idempotent — chạy lại bao nhiêu
# lần cũng được, chỉ đụng vào repo chưa có.
#
# Luôn dùng `git clone`, KHÔNG BAO GIỜ `git init` + `remote add` + `fetch`.
# Cách sau không tạo upstream tracking và đặt tên branch theo init.defaultBranch
# của máy chứ không theo remote — nguồn gốc của cả loạt lỗi:
#   "no tracking information", "couldn't find remote ref master",
#   và clone bám nhầm branch làm lệch cả trăm file.
#
# Xử lý được ca khó: thư mục đã tồn tại nhưng CHỈ chứa `.understand-anything/`
# (graph đến từ repo cha, chưa từng có source). `git clone` từ chối thư mục không
# rỗng, nên clone ra temp rồi chuyển graph sang và hoán đổi — graph giữ nguyên.
#
# MẶC ĐỊNH DRY-RUN. Phải truyền --apply mới thực sự ghi.
#
# stdout : JSON report  |  stderr : log người đọc
# exit   : 0 = không có gì cần xử lý   1 = có repo lỗi/thiếu url   2 = lỗi dùng sai
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CSV="${CLONE_REPOS_CSV:-$SCRIPT_DIR/../repos.csv}"
REPO_ROOT="${CLONE_REPOS_ROOT:-/srv/ua-data}"
APPLY=0

for a in "$@"; do
    case "$a" in
        --apply) APPLY=1 ;;
        --csv=*) CSV="${a#--csv=}" ;;
        -*) echo "tham so la: $a" >&2; exit 2 ;;
        *) REPO_ROOT="$a" ;;
    esac
done

command -v jq &>/dev/null || { echo '{"error":"jq not installed"}' >&2; exit 2; }
[[ -f "$CSV" ]] || { echo "{\"error\":\"khong doc duoc CSV: $CSV\"}" >&2; exit 2; }
mkdir -p "$REPO_ROOT" || { echo "{\"error\":\"khong tao duoc $REPO_ROOT\"}" >&2; exit 2; }

log_line() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >&2; }
RESULTS="[]"
add_result() {
    RESULTS=$(echo "$RESULTS" | jq --arg r "$1" --arg s "$2" --arg m "$3" \
        '. + [{"repo":$r,"status":$s,"detail":$m}]')
}

# Proxy cho mọi lệnh chạm mạng — giống git_net trong git-pull.sh.
git_net() {
    git -c credential.helper='store --file ~/.git-credentials' \
        -c http.proxy="${https_proxy:-}" \
        -c http.noProxy="${no_proxy:-}" \
        -c https.proxy="${https_proxy:-}" \
        -c https.noProxy="${no_proxy:-}" \
        "$@"
}

log_line "=== clone-repos: $REPO_ROOT (apply=$APPLY) ==="
log_line "    manifest: $CSV"
[[ "$APPLY" -eq 0 ]] && log_line "    DRY-RUN — khong ghi gi. Them --apply de thuc thi."

# Hai thư mục trỏ cùng một URL gần như luôn là lỗi copy-paste, và hậu quả im lặng:
# graph của repo này bị gán source của repo kia, ua-mcp trả lời sai mà không báo gì.
# Chặn ngay, không clone dòng nào.
dups=$(grep -v '^#' "$CSV" | awk -F, 'NF>=2 && $1!="dir_name" && $2!="" {print $2}' \
       | tr -d '\r' | sort | uniq -d)
if [[ -n "$dups" ]]; then
    log_line "  [LOI] CSV co URL bi lap — sua truoc khi chay:"
    while IFS= read -r u; do
        [[ -z "$u" ]] && continue
        log_line "    $u"
        log_line "      dung boi: $(grep -F ",$u" "$CSV" | cut -d, -f1 | tr '\n' ' ')"
    done <<< "$dups"
    echo '{"error":"duplicate git_url trong CSV","urls":[]}' \
        | jq --arg d "$dups" '.urls = ($d | split("\n") | map(select(length>0)))'
    exit 2
fi

while IFS=, read -r name url _rest || [[ -n "${name:-}" ]]; do
    name="$(echo "${name:-}" | tr -d '\r' | xargs)"
    url="$(echo "${url:-}"  | tr -d '\r' | xargs)"
    [[ -z "$name" || "$name" == \#* || "$name" == "dir_name" ]] && continue

    dir="$REPO_ROOT/$name"

    if [[ -z "$url" ]]; then
        add_result "$name" "no-url" "chua dien git_url trong CSV"
        log_line "  [NO-URL] $name — chua dien url"
        continue
    fi

    # Đã là git repo -> chỉ đối chiếu remote, không đụng vào.
    if [[ -d "$dir/.git" ]]; then
        cur="$(git -C "$dir" remote get-url origin 2>/dev/null)"
        if [[ "${cur%.git}" == "${url%.git}" ]]; then
            add_result "$name" "exists" "da co, remote khop"
            log_line "  [OK] $name — da co, remote khop"
        else
            add_result "$name" "url-mismatch" "local=$cur csv=$url"
            log_line "  [MISMATCH] $name — local='$cur' vs csv='$url' (khong tu sua)"
        fi
        continue
    fi

    # Thư mục có sẵn nhưng không phải git repo: chỉ chấp nhận khi nó chỉ chứa
    # `.understand-anything`. Có thứ khác -> dừng, để người xem, không xoá bừa.
    if [[ -d "$dir" ]]; then
        extra="$(find "$dir" -mindepth 1 -maxdepth 1 ! -name '.understand-anything' 2>/dev/null | head -5)"
        if [[ -n "$extra" ]]; then
            add_result "$name" "skipped" "thu muc co noi dung la: $(echo "$extra" | tr '\n' ' ')"
            log_line "  [SKIP] $name — thu muc co noi dung khac ngoai .understand-anything"
            continue
        fi
    fi

    if [[ "$APPLY" -eq 0 ]]; then
        act=$([[ -d "$dir" ]] && echo "clone + giu lai .understand-anything" || echo "clone moi")
        add_result "$name" "would-clone" "$act"
        log_line "  [DRY] $name — $act"
        continue
    fi

    tmp="$REPO_ROOT/.tmp-clone-$name"
    rm -rf "$tmp"
    if ! out=$(git_net clone "$url" "$tmp" 2>&1); then
        rm -rf "$tmp"
        add_result "$name" "failed" "$out"
        log_line "  [FAIL] $name — clone that bai"
        continue
    fi

    # Clone xong mới đụng vào thư mục cũ -> clone hỏng thì không mất gì.
    if [[ -d "$dir/.understand-anything" ]]; then
        mv "$dir/.understand-anything" "$tmp/" || {
            rm -rf "$tmp"; add_result "$name" "failed" "khong chuyen duoc .understand-anything"
            log_line "  [FAIL] $name — khong chuyen duoc graph"; continue; }
    fi
    rm -rf "$dir" && mv "$tmp" "$dir" || {
        add_result "$name" "failed" "khong hoan doi duoc thu muc"
        log_line "  [FAIL] $name — khong hoan doi duoc thu muc"; continue; }

    # Graph là runtime state — ignore ngay để pull không vướng về sau.
    # Query có "/" cuối: pattern `.understand-anything/` chỉ khớp khi git biết
    # đường dẫn là thư mục, mà clone mới có thể chưa có thư mục đó.
    if ! git -C "$dir" check-ignore -q .understand-anything/ 2>/dev/null; then
        excl="$(git -C "$dir" rev-parse --git-path info/exclude)"
        [[ "$excl" != /* ]] && excl="$dir/$excl"
        mkdir -p "$(dirname "$excl")"
        printf '\n# graph la runtime state — xem scripts/README.md\n.understand-anything/\n' >> "$excl"
    fi

    b=$(git -C "$dir" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null)
    add_result "$name" "cloned" "upstream=${b:-KHONG CO}"
    log_line "  [CLONED] $name — upstream=${b:-KHONG CO}"
done < "$CSV"

echo "$RESULTS" | jq --arg ts "$(date -Iseconds)" --argjson ap "$APPLY" '{
  timestamp: $ts, applied: ($ap == 1), results: .,
  summary: {
    total:        (. | length),
    cloned:       ([.[] | select(.status=="cloned")]       | length),
    would_clone:  ([.[] | select(.status=="would-clone")]  | length),
    exists:       ([.[] | select(.status=="exists")]       | length),
    no_url:       ([.[] | select(.status=="no-url")]       | length),
    url_mismatch: ([.[] | select(.status=="url-mismatch")] | length),
    skipped:      ([.[] | select(.status=="skipped")]      | length),
    failed:       ([.[] | select(.status=="failed")]       | length)
  }
}'

log_line "=== xong ==="
bad=$(echo "$RESULTS" | jq '[.[] | select(.status=="failed" or .status=="no-url" or .status=="url-mismatch" or .status=="skipped")] | length')
[[ "$bad" -gt 0 ]] && { log_line "$bad repo can xu ly"; exit 1; }
exit 0
