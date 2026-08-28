#!/usr/bin/env bash
#
# Index lại knowledge-graph cho các repo đã đổi so với graph hiện có.
#
# KHÔNG gọi `/understand`. Plugin có sẵn cơ chế incremental ở
# `hooks/auto-update-prompt.md`, phân tầng theo mức thay đổi:
#   SKIP                — đổi cosmetic (format, logic nội bộ) -> 0 token LLM
#   PARTIAL_UPDATE      — chỉ phân tích lại file đổi cấu trúc
#   ARCHITECTURE_UPDATE — thêm dựng lại layer/tour
#   FULL_UPDATE         — đổi quá lớn, prompt DỪNG và đòi `/understand --full`
# Chạy `/understand` mỗi lần là ném bỏ toàn bộ tầng fingerprint zero-token đó.
#
# Mốc staleness lấy đúng theo cái plugin tự dùng (hooks/hooks.json):
#   .understand-anything/meta.json .gitCommitHash  !=  git rev-parse HEAD
#
# Agent runtime chạy trong một container docker được xây sẵn (VM không có
# node/npm). Model có thể là bất kỳ LLM nào sau proxy (ANTHROPIC_BASE_URL) —
# Qwen, Claude, v.v. Script không quan tâm model nào, chỉ cần CLI tương thích
# `claude -p --output-format stream-json`.
#
# MẶC ĐỊNH DRY-RUN. Phải truyền --apply mới thực sự chạy index.
#
#   ./sync-graph.sh                      # xem repo nào cần index
#   ./sync-graph.sh --apply              # index thật
#   ./sync-graph.sh --apply --repo=abc   # chỉ một repo
#   ./sync-graph.sh --apply --baseline   # dựng graph lần đầu cho repo chưa có
#   ./sync-graph.sh --apply --full       # cho phép rebuild khi tier = FULL_UPDATE
#   ./sync-graph.sh --apply --partial    # vá từng phần, KHÔNG bao giờ rebuild
#   ./sync-graph.sh --apply --partial --domain  # cập nhật cả domain-graph.json
#   ./sync-graph.sh --apply --domain-only # CHỈ dựng domain, kệ knowledge-graph
#   ./sync-graph.sh --apply --partial --max-changed=60   # bỏ qua repo diff quá lớn
#   ./sync-graph.sh --apply --rebuild    # ép /understand --full, kệ staleness
#
# stdout : JSON report  |  stderr : log người đọc
# exit   : 0 ổn   1 có repo cần can thiệp   2 lỗi dùng sai
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SYNC_GRAPH_ROOT:-/srv/ua-data}"
IMAGE="${UA_INDEXER_IMAGE:-ua-indexer:latest}"
NET="${UA_INDEXER_NETWORK:-bridge}"
BASE_URL="${ANTHROPIC_BASE_URL:-http://localhost:4000}"
API_KEY="${ANTHROPIC_API_KEY:-EMPTY}"
MODEL="${ANTHROPIC_MODEL:-claude-sonnet-4-5}"
TIMEOUT="${SYNC_GRAPH_TIMEOUT:-1800}"
# Rebuild toàn bộ quét lại mọi file và chạy nhiều subagent, lâu hơn hẳn
# incremental — đo thực tế rồi hãy siết con số này.
FULL_TIMEOUT="${SYNC_GRAPH_FULL_TIMEOUT:-7200}"
LOG_DIR="${SYNC_GRAPH_LOG_DIR:-/var/log/ua-deploy/sync-graph}"
# Qwen tu chon ngon ngu theo noi dung repo: 1 repo ra tieng Viet, repo khac ra
# tieng Anh. Graph lan hai thu tieng lam tim kiem ngu nghia cua ua-mcp yeu di —
# hoi tieng Anh kho khop node tieng Viet va nguoc lai. Mac dinh "English" vi
# phan lon graph hien co la tieng Anh.
SUMMARY_LANG="${SYNC_GRAPH_LANG:-English}"
# Giu bao nhieu snapshot moi repo. Snapshot la duong lui duy nhat (graph bi
# gitignore) nhung khong can giu vo han.
KEEP_SNAPSHOTS="${SYNC_GRAPH_KEEP_SNAPSHOTS:-2}"
APPLY=0
BASELINE=0
FULL=0
PARTIAL=0
DOMAIN=0
DOMAIN_ONLY=0
REBUILD=0
# 0 = khong gioi han. Dat >0 de bo qua repo co qua nhieu file doi.
MAX_CHANGED="${SYNC_GRAPH_MAX_CHANGED:-0}"
ONLY=""

for a in "$@"; do
    case "$a" in
        --apply)     APPLY=1 ;;
        --baseline)  BASELINE=1 ;;
        --full)      FULL=1 ;;
        --partial)   PARTIAL=1 ;;
        --domain)    DOMAIN=1 ;;
        --domain-only) DOMAIN_ONLY=1; DOMAIN=1 ;;
        --max-changed=*) MAX_CHANGED="${a#--max-changed=}" ;;
        --rebuild)   REBUILD=1 ;;
        --full-timeout=*) FULL_TIMEOUT="${a#--full-timeout=}" ;;
        --repo=*)    ONLY="${a#--repo=}" ;;
        --timeout=*) TIMEOUT="${a#--timeout=}" ;;
        -*) echo "tham so la: $a" >&2; exit 2 ;;
        *)  REPO_ROOT="$a" ;;
    esac
done

# Buoc domain la mot lan chay agent RIENG, khong lien quan gi toi do lon cua
# diff. Truoc day no dung chung $TIMEOUT voi buoc index, va vi con retry mot lan
# nua nen tran that su la 2x TIMEOUT = 3600s — gap doi cai ai cung tuong.
DOMAIN_TIMEOUT="${SYNC_GRAPH_DOMAIN_TIMEOUT:-$TIMEOUT}"

command -v jq >/dev/null     || { echo '{"error":"thieu jq"}' >&2; exit 2; }
command -v docker >/dev/null || { echo '{"error":"thieu docker"}' >&2; exit 2; }
docker image inspect "$IMAGE" >/dev/null 2>&1 \
    || { echo "{\"error\":\"chua co image $IMAGE — xay image indexer truoc\"}" >&2; exit 2; }
[[ -d "$REPO_ROOT" ]] || { echo "{\"error\":\"khong thay $REPO_ROOT\"}" >&2; exit 2; }

log_line() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >&2; }
RESULTS="[]"
# $4 = tong giay cua ca repo (index + domain), $6 = phan cua rieng domain.
# Thoi gian index = seconds - seconds_domain.
add_result() {
    RESULTS=$(echo "$RESULTS" | jq --arg r "$1" --arg s "$2" --arg m "$3" \
        --argjson d "${4:-0}" --arg t "${5:-}" --argjson dd "${6:-0}" \
        '. + [{"repo":$r,"status":$s,"detail":$m,"seconds":$d,
               "seconds_domain":$dd,"tier":$t}]')
}

# Chốt chặn: `meta.json` nhảy sang HEAD KHÔNG chứng minh graph đã được phân tích
# lại — agent có thể ghi hash mà không làm gì. Đối chiếu cả nội dung graph mới đủ tin.
graph_sum() { md5sum "$1" 2>/dev/null | cut -d' ' -f1 || cksum "$1" 2>/dev/null | cut -d' ' -f1; }

# domain-graph.json chua tung duoc dung: file khong co, hoac chi la stub
# 1 domain / 1 flow / 1 step (~1.5-2.2KB) do plugin tao san.
#
# CAU TRUC THAT: domain/flow/step nam trong .nodes[].type, KHONG co key
# `.domains` o top-level. `jq '.domains|length'` luon tra 0 ke ca voi graph day
# du — da tung chan doan sai vi cho nay.
domain_count() {  # domain_count <domain_graph.json> -> so node type=domain
    [[ -f "$1" ]] || { echo 0; return; }
    jq '[.nodes[]? | select(.type=="domain")] | length' "$1" 2>/dev/null || echo 0
}
domain_stub() { [[ "$(domain_count "$1")" -le 1 ]]; }

# Ty le file node mang summary template ("java code file (227 lines)") — dau hieu
# file chi duoc trich xuat cau truc, KHONG duoc phan tich LLM.
# Da gap that: /understand --full len lich 102 batch nhung chi chay 3, roi merge
# vao ban cau truc tho -> 1122/1170 file mat summary do Claude viet. md5 van doi
# nen moi chot "noi dung co doi" khong bat duoc. Day la chot thu hai.
template_ratio() {
    jq -r '[.nodes[]? | select(.type=="file") | .summary // ""] as $s
           | if ($s | length) == 0 then 0
             else (([$s[] | select(test("code file \\([0-9]+ lines\\)"))] | length) * 100
                   / ($s | length) | floor)
             end' "$1" 2>/dev/null || echo 0
}

# Khoi phuc graph tu snapshot. Dung khi container bi giet giua chung (timeout):
# merge o Phase 3 co the da ghi dang do, va graph hong thi ua-mcp bo luon project.
restore_snapshot() {  # restore_snapshot <repo_dir> <snapshot_dir>
    local d="$1" snap="$2"
    [[ -n "$snap" && -d "$snap" ]] || return 1
    rm -rf "$d/.understand-anything" && mv "$snap" "$d/.understand-anything"
}

prune_snapshots() {  # prune_snapshots <repo_dir>
    ls -dt "$1"/.understand-anything.bak-* 2>/dev/null \
        | tail -n "+$((KEEP_SNAPSHOTS + 1))" \
        | while IFS= read -r old; do rm -rf "$old"; done
}

# Commit hash duoc luu O HAI CHO va auto-update chi cap nhat mot:
#   meta.json .gitCommitHash          <- auto-update ghi
#   knowledge-graph.json .project.gitCommitHash  <- chi /understand --full ghi
# Hau qua: sau khi sync bang --partial, /understand-domain va ua-mcp doc truong
# trong graph nen bao "graph cu hon HEAD" du da cap nhat, con sync-graph.sh doc
# meta.json nen bao up-to-date. Hai cong cu nhin hai nguon khac nhau.
sync_project_hash() {  # sync_project_hash <graph_file> <hash>
    local g="$1" h="$2" tmp
    tmp="$(mktemp "${g}.XXXXXX")" || return 1
    if jq --arg h "$h" '.project.gitCommitHash = $h' "$g" > "$tmp" 2>/dev/null \
       && [[ -s "$tmp" ]]; then
        mv "$tmp" "$g"
    else
        rm -f "$tmp"; return 1
    fi
}

# Chup .understand-anything truoc khi ghi de. Graph bi gitignore nen khong co
# duong lui nao khac, ma mot lan rebuild hong la mat cong Claude dung tu dau.
snapshot() {
    local src="$1/.understand-anything" dst="$1/.understand-anything.bak-$(date +%Y%m%dT%H%M%S)"
    [[ -d "$src" ]] || return 0
    cp -a "$src" "$dst" 2>/dev/null && echo "$dst"
}

mkdir -p "$LOG_DIR" || { echo "{\"error\":\"khong tao duoc $LOG_DIR\"}" >&2; exit 2; }
AGENT_LOG="$LOG_DIR/.unused.log"

log_line "=== sync-graph: $REPO_ROOT (apply=$APPLY baseline=$BASELINE) ==="
log_line "    log agent: $LOG_DIR"
log_line "    image=$IMAGE net=$NET model=$MODEL timeout=${TIMEOUT}s"
[[ "$APPLY" -eq 0 ]] && log_line "    DRY-RUN — khong chay index. Them --apply de thuc thi."

# Prompt cho agent. Đúng cách hook của plugin kích hoạt: bảo nó đọc và thi hành
# auto-update-prompt.md, không tự chế lại quy trình 3 phase.
# Ghi cứng /opt/ua-plugin thay vì $CLAUDE_PLUGIN_ROOT: agent đọc prompt như văn
# bản, không expand biến shell. Symlink này do Dockerfile tạo, luôn cố định.
PROMPT='Doc file /opt/ua-plugin/hooks/auto-update-prompt.md va thuc hien
day du cac buoc trong do cho project o thu muc lam viec hien tai.
Khong hoi xac nhan. Ket thuc bang mot dong duy nhat co dang:
SYNC_RESULT=<SKIP|PARTIAL_UPDATE|ARCHITECTURE_UPDATE|FULL_UPDATE|STOPPED>
Neu dung lai vi thay doi cau truc qua lon (tier FULL_UPDATE), phai bao
SYNC_RESULT=FULL_UPDATE chu KHONG phai STOPPED. STOPPED chi danh cho cac ly do
dung khac (thieu graph, thieu meta.json, khong tim thay plugin).'

# Ep di tiep theo duong PARTIAL_UPDATE ke ca khi tier ra FULL_UPDATE.
#
# Nguong 30 file cua plugin duoc thiet ke cho truong hop rebuild re. Moi
# model thuong chi tot o phan tich file DOI, rebuild toan bo la viec dat
# va de bi dung dau. Nen voi repo da co graph, va tung phan luon dung hon rebuild.
PARTIAL_PROMPT='Doc file /opt/ua-plugin/hooks/auto-update-prompt.md va thuc hien
cho project o thu muc lam viec hien tai, VOI HAI NGOAI LE BAT BUOC:

1. Neu Phase 1 phan loai la FULL_UPDATE, KHONG duoc dung lai va KHONG duoc
   khuyen rebuild. Van tiep tuc Phase 2 va Phase 3 theo duong PARTIAL_UPDATE.
2. TUYET DOI khong quet lai toan bo project va khong ghi de node cua file KHONG
   thay doi. Chi phan tich lai cac file trong filesToReanalyze roi va vao graph
   dang co. Summary cua file khong doi phai giu nguyen.

Moi summary va tag phai viet bang __LANG__, ke ca khi code hoac comment dung
ngon ngu khac. Graph lan nhieu ngon ngu lam tim kiem ngu nghia kem chinh xac.

Khong hoi xac nhan. Ket thuc bang mot dong duy nhat co dang:
SYNC_RESULT=<SKIP|PARTIAL_UPDATE|ARCHITECTURE_UPDATE|STOPPED>'

BASELINE_PROMPT='/understand .'

FULL_PROMPT='/understand --full'

# auto-update-prompt.md KHONG dung toi domain — no chi cap nhat knowledge-graph,
# layer va tour. Nen domain-graph.json cu dan sau moi lan sync, va
# get_domain_overview cua ua-mcp se khong thay flow nghiep vu moi.
# /understand-domain suy ra tu knowledge graph co san (SKILL.md: "cheap, no file
# scanning") nen chay sau khi sync la re, khong quet lai repo.
DOMAIN_PROMPT="/understand-domain

Moi mo ta domain, flow va step phai viet bang $SUMMARY_LANG."

# Chay /understand-domain, tra ve mo ta ket qua de ghep vao detail.
run_domain() {  # run_domain <repo_dir>
    local d="$1" dg="$1/.understand-anything/domain-graph.json" before after
    before="$(graph_sum "$dg")"
    # Log rieng cho buoc domain. Truoc do nuot het output (>/dev/null) nen khi
    # domain-analyzer timeout, ket qua chi la chu "khong-doi" mo ho — mat hoan
    # toan dau vet de chan doan.
    local saved="$AGENT_LOG" t0=$SECONDS
    AGENT_LOG="${saved%.log}-domain.log"
    run_agent "$d" "$DOMAIN_PROMPT" "$DOMAIN_TIMEOUT" >/dev/null
    local rc=$?
    log_line "        domain lan 1: ${rc} sau $((SECONDS - t0))s"
    after="$(graph_sum "$dg")"

    # Thu lai DUNG MOT LAN khi that bai. Bang chung: co repo bi "API Error: The
    # operation timed out", chay lai y nguyen thi xong sau 392s. Loi nhat thoi
    # phia server, khong phai gioi han kien truc — nen retry re hon nhieu so voi
    # chia nho dau vao.
    if [[ $rc -ne 0 || "$after" == "$before" ]] \
       && grep -qiE 'API Error|timed out|timeout' "$AGENT_LOG" "$(agent_err_path)" 2>/dev/null; then
        log_line "        domain that bai (rc=$rc) — thu lai lan cuoi..."
        local t1=$SECONDS
        run_agent "$d" "$DOMAIN_PROMPT" "$DOMAIN_TIMEOUT" >/dev/null
        rc=$?
        after="$(graph_sum "$dg")"
        log_line "        domain lan 2: ${rc} sau $((SECONDS - t1))s"
    fi

    local err=""
    grep -qiE 'API Error|timed out|timeout' "$AGENT_LOG" "$(agent_err_path)" 2>/dev/null && err=" (co loi/timeout trong log)"
    AGENT_LOG="$saved"
    # Tra ve "<giay>|<thong diep>". Ham nay luon chay trong $( ) nen khong the
    # set bien toan cuc — phai day so giay ra stdout cung thong diep.
    local el=$((SECONDS - t0)) msg
    if [[ $rc -eq 124 ]]; then msg="domain=timeout"
    elif [[ ! -f "$dg" ]]; then msg="domain=khong-tao-duoc$err"
    elif [[ "$after" != "$before" ]]; then msg="domain=da-cap-nhat"
    else msg="domain=khong-doi$err"; fi
    echo "${el}|${msg}"
}

# Tach ket qua run_domain. Dat DOMAIN_SECONDS va DOMAIN_MSG o pham vi goi.
read_domain() {  # read_domain <output cua run_domain>
    DOMAIN_SECONDS="${1%%|*}"; DOMAIN_MSG="${1#*|}"
}

# In tien do ra stderr ngay khi agent lam viec.
# `claude -p` thuong chi in ket qua MOT LAN luc ket thuc, nen `tee` khong giup gi.
# `--output-format stream-json` phat moi tool call thanh mot dong JSON ngay lap
# tuc — do la thu duy nhat cho biet no dang lam gi trong suot hang gio chay.
progress_filter() {
    local n=0
    while IFS= read -r line; do
        msg="$(printf '%s' "$line" | jq -r '
            if .type == "assistant" then
                (.message.content[]? | select(.type == "tool_use")
                 | .name + " " + ((.input.command // .input.file_path // .input.pattern
                                   // .input.description // "") | tostring))
            elif .type == "result" then
                "KET THUC: " + ((.subtype // "?") | tostring)
            else empty end' 2>/dev/null)"
        [[ -z "$msg" ]] && continue
        while IFS= read -r m; do
            [[ -z "$m" ]] && continue
            n=$((n + 1))
            printf '      %3d  %.100s\n' "$n" "$m" >&2
        done <<< "$msg"
    done
}

# Lay phan text agent tra ve tu log stream-json (de doc tier + chan doan loi).
agent_text() { jq -r 'select(.type=="assistant") | .message.content[]?
                      | select(.type=="text") | .text' "$AGENT_LOG" 2>/dev/null; }
agent_tail()  { local t; t="$(agent_text)"; [[ -z "$t" ]] && t="$(cat "$AGENT_LOG")"
                echo "$t" | tail -n "${1:-8}"; }

# stderr cua CLI di ra file rieng canh $AGENT_LOG. Xem ghi chu trong run_agent.
agent_err_path() { echo "${AGENT_LOG%.log}.stderr.log"; }
# CLI bao dong nay ra stderr roi thoat 0 khi background task chua xong. Nhin tu
# ngoai giong het mot lan chay thanh cong nhung khong ghi gi — phai bat rieng.
bg_killed() { grep -q 'Background tasks still running' "$(agent_err_path)" 2>/dev/null; }

run_agent() {  # run_agent <repo_dir> <prompt> [timeout]  -> log vao $AGENT_LOG
    set -- "$1" "${2//__LANG__/$SUMMARY_LANG}" "${3:-}"
    # GIT_CONFIG_*: repo mount vao container co the khac uid -> git bao
    # "dubious ownership" va moi lenh rev-parse/diff deu fail im lang.
    #
    # stderr PHAI ra file rieng, KHONG duoc `2>&1`. Gop vao stream-json thi mot
    # dong stderr duy nhat cua CLI o giua file cung lam `jq` dung han tai do:
    # agent_text() tra ve van ban giua chung, `tier` luon rong, va guard
    # FULL_UPDATE o cuoi vong lap khong bao gio kich hoat.
    #
    # CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 = cho background task chay het.
    # Mac dinh cua `claude -p` la 600s roi GIET task va thoat 0 — dong ngan
    # no de moi lan chay duoc xet het theo `timeout` ben ngoai.
    # `timeout` ben ngoai van la chan tren, nen bo tran o day khong noi long
    # gioi han nao ca.
    : >"$(agent_err_path)"
    timeout "${3:-$TIMEOUT}" docker run --rm --network "$NET" \
        --user "$(id -u):$(id -g)" \
        -v "$1:/repo" -w /repo \
        -e GIT_CONFIG_COUNT=1 \
        -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0=/repo \
        -e ANTHROPIC_BASE_URL="$BASE_URL" -e ANTHROPIC_API_KEY="$API_KEY" \
        -e ANTHROPIC_MODEL="$MODEL" \
        -e ANTHROPIC_DEFAULT_SONNET_MODEL="$MODEL" \
        -e ANTHROPIC_DEFAULT_OPUS_MODEL="$MODEL" \
        -e ANTHROPIC_DEFAULT_HAIKU_MODEL="$MODEL" \
        -e ANTHROPIC_SMALL_FAST_MODEL="$MODEL" \
        -e CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 \
        "$IMAGE" -p --permission-mode bypassPermissions \
        --output-format stream-json --verbose "$2" 2>"$(agent_err_path)" \
        | tee "$AGENT_LOG" | progress_filter
    # rc cua docker, khong phai cua tee/progress_filter
    return "${PIPESTATUS[0]}"
}

for dir in "$REPO_ROOT"/*/; do
    name="$(basename "$dir")"
    dir="${dir%/}"
    [[ -n "$ONLY" && "$name" != "$ONLY" ]] && continue
    [[ "$name" == .tmp-clone-* ]] && continue

    # Log raw stream-json cua tung repo: chan doan duoc sau khi chay xong, va
    # tail -f duoc tu terminal khac trong luc dang chay.
    AGENT_LOG="$LOG_DIR/${name}-$(date +%Y%m%dT%H%M%S).log"
    DOMAIN_SECONDS=0; DOMAIN_MSG=""

    if [[ ! -d "$dir/.git" ]]; then
        add_result "$name" "not-a-repo" "khong phai git repo, bo qua"
        continue
    fi

    head_hash="$(git -C "$dir" rev-parse HEAD 2>/dev/null)"
    if [[ -z "$head_hash" ]]; then
        add_result "$name" "failed" "khong doc duoc HEAD (repo rong?)"
        log_line "  [FAIL] $name — khong doc duoc HEAD"
        continue
    fi

    meta="$dir/.understand-anything/meta.json"
    graph="$dir/.understand-anything/knowledge-graph.json"

    # Chưa có graph -> auto-update KHÔNG chạy được, phải dựng baseline bằng
    # /understand. Đây là việc đắt (quét toàn bộ file) nên không tự làm lén.
    if [[ ! -f "$meta" || ! -f "$graph" ]]; then
        if [[ "$BASELINE" -eq 0 ]]; then
            add_result "$name" "no-baseline" "chua co graph — chay lai voi --baseline"
            log_line "  [NO-BASELINE] $name — chua co graph"
            continue
        fi
        if [[ "$APPLY" -eq 0 ]]; then
            add_result "$name" "would-baseline" "se chay /understand de dung graph lan dau"
            log_line "  [DRY] $name — se dung baseline"
            continue
        fi
        log_line "  [BASELINE] $name — dang chay /understand (co the rat lau)..."
        t0=$SECONDS
        run_agent "$dir" "$BASELINE_PROMPT"; rc=$?
        el=$((SECONDS - t0))
        if [[ -f "$meta" ]]; then
            add_result "$name" "baselined" "da dung graph lan dau" "$el" "BASELINE"
            log_line "  [OK] $name — baseline xong sau ${el}s"
        else
            add_result "$name" "failed" "$(agent_tail 5)" "$el" ""
            log_line "  [FAIL] $name — baseline that bai (rc=$rc, ${el}s)"
        fi
        continue
    fi

    dgraph="$dir/.understand-anything/domain-graph.json"

    # --domain-only: bo qua hoan toan viec sync knowledge-graph.
    # Dung khi domain-graph thieu o repo KHONG co code doi — truong hop nay
    # vong lap thuong duoi khong bao gio cham toi (xem ghi chu o nhanh
    # up-to-date ben duoi).
    if [[ "$DOMAIN_ONLY" -eq 1 ]]; then
        dn="$(domain_count "$dgraph")"
        if [[ -z "$ONLY" ]] && ! domain_stub "$dgraph"; then
            add_result "$name" "up-to-date" "domain da co ($dn domain), bo qua"
            continue
        fi
        if [[ "$APPLY" -eq 0 ]]; then
            add_result "$name" "would-domain" "se dung domain-graph (dang co $dn domain)"
            log_line "  [DRY] $name — se dung domain-graph (dang co $dn domain)"
            continue
        fi
        log_line "  [DOMAIN] $name — dang dung domain-graph (dang co $dn domain)..."
        t0=$SECONDS
        read_domain "$(run_domain "$dir")"
        el=$((SECONDS - t0))
        dn_after="$(domain_count "$dgraph")"
        if [[ "$dn_after" -gt 1 ]]; then
            add_result "$name" "updated" "domain $dn -> $dn_after ($DOMAIN_MSG)" "$el" "DOMAIN" "$DOMAIN_SECONDS"
            log_line "  [OK] $name — domain $dn -> $dn_after sau ${el}s"
        else
            add_result "$name" "failed" "domain van la stub sau khi chay ($DOMAIN_MSG)" "$el" "DOMAIN" "$DOMAIN_SECONDS"
            log_line "  [FAIL] $name — domain van stub sau ${el}s ($DOMAIN_MSG)"
        fi
        continue
    fi

    graph_hash="$(jq -r '.gitCommitHash // empty' "$meta" 2>/dev/null)"
    if [[ -z "$graph_hash" ]]; then
        add_result "$name" "failed" "meta.json khong co gitCommitHash"
        log_line "  [FAIL] $name — meta.json hong"
        continue
    fi

    if [[ "$graph_hash" == "$head_hash" && "$REBUILD" -eq 0 ]]; then
        # `run_domain` chi duoc goi trong nhanh "updated" o cuoi vong lap, nen
        # repo KHONG co code doi khong bao gio chay toi buoc domain. Do la ly do
        # mot so repo ket o stub 1/1/1: chung up-to-date tu lau, `--domain` chua
        # tung cham toi. Vot lai o day, chi khi domain con la stub.
        if [[ "$DOMAIN" -eq 1 ]] && domain_stub "$dgraph"; then
            if [[ "$APPLY" -eq 0 ]]; then
                add_result "$name" "would-domain" "graph khop HEAD nhung domain con stub"
                log_line "  [DRY] $name — se dung domain-graph (con stub)"
                continue
            fi
            log_line "  [DOMAIN] $name — graph khop HEAD, dung domain con thieu..."
            t0=$SECONDS
            read_domain "$(run_domain "$dir")"
            el=$((SECONDS - t0))
            dn_after="$(domain_count "$dgraph")"
            if [[ "$dn_after" -gt 1 ]]; then
                add_result "$name" "updated" "chi dung domain: -> $dn_after domain ($DOMAIN_MSG)" "$el" "DOMAIN" "$DOMAIN_SECONDS"
                log_line "  [OK] $name — domain -> $dn_after sau ${el}s"
            else
                add_result "$name" "failed" "domain van la stub ($DOMAIN_MSG)" "$el" "DOMAIN" "$DOMAIN_SECONDS"
                log_line "  [FAIL] $name — domain van stub sau ${el}s"
            fi
            continue
        fi
        add_result "$name" "up-to-date" "graph khop HEAD ${head_hash:0:8}"
        continue
    fi

    sum_before="$(graph_sum "$graph")"

    # --rebuild: bo qua ca staleness lan auto-update, chay thang /understand --full.
    # Can khi meta.json bi day len HEAD ma graph chua he duoc phan tich lai —
    # luc do auto-update se bao "already up to date" va STOP, khong cuu duoc.
    if [[ "$REBUILD" -eq 1 ]]; then
        if [[ "$APPLY" -eq 0 ]]; then
            add_result "$name" "would-rebuild" "se chay /understand --full (ep buoc)"
            log_line "  [DRY] $name — se rebuild toan bo (ep buoc)"
            continue
        fi
        log_line "  [REBUILD] $name — ep /understand --full (timeout ${FULL_TIMEOUT}s)..."
        tpl_before="$(template_ratio "$graph")"
        snap="$(snapshot "$dir")"
        [[ -n "$snap" ]] && log_line "        da chup: $(basename "$snap") (template ${tpl_before}%)"
        t0=$SECONDS
        run_agent "$dir" "$FULL_PROMPT" "$FULL_TIMEOUT"; rc=$?
        el=$((SECONDS - t0))
        new_hash="$(jq -r '.gitCommitHash // empty' "$meta" 2>/dev/null)"
        sum_after="$(graph_sum "$graph")"
        if [[ $rc -eq 124 ]]; then
            add_result "$name" "timeout" "rebuild qua ${FULL_TIMEOUT}s, da huy" "$el" "FORCED"
            log_line "  [TIMEOUT] $name — qua ${FULL_TIMEOUT}s"
        elif [[ "$new_hash" == "$head_hash" && "$sum_after" != "$sum_before" ]]; then
            tpl_after="$(template_ratio "$graph")"
            if [[ "$tpl_after" -gt $((tpl_before + 20)) ]]; then
                # Ghi that nhung ghi TE hon: phan lon file mat summary. Giu lai
                # ban cu — graph ngheo hon ban dang co la buoc lui, khong phai
                # cap nhat.
                add_result "$name" "degraded" \
                    "summary template ${tpl_before}% -> ${tpl_after}%, da khoi phuc ban cu" "$el" "FORCED"
                log_line "  [DEGRADED] $name — template ${tpl_before}%->${tpl_after}%, KHOI PHUC ban cu"
                restore_snapshot "$dir" "$snap" \
                    && log_line "        da khoi phuc tu snapshot" \
                    || log_line "        KHOI PHUC THAT BAI — ban cu con o $snap"
            else
                sync_project_hash "$graph" "$head_hash" \
                    || log_line "        canh bao: khong dong bo duoc project.gitCommitHash"
                add_result "$name" "rebuilt" "graph doi noi dung, hash -> ${head_hash:0:8} (template ${tpl_after}%)" "$el" "FORCED"
                log_line "  [OK] $name — rebuild xong sau ${el}s (template ${tpl_after}%)"
            fi
        elif [[ "$sum_after" == "$sum_before" ]]; then
            add_result "$name" "suspect" "hash=${new_hash:0:8} nhung noi dung graph KHONG doi" "$el" "FORCED"
            log_line "  [SUSPECT] $name — graph khong doi sau ${el}s, khong tin duoc"
        else
            add_result "$name" "failed" "$(agent_tail 8)" "$el" "FORCED"
            log_line "  [FAIL] $name — rc=$rc sau ${el}s"
            restore_snapshot "$dir" "$snap" \
                && log_line "        da khoi phuc snapshot" \
                || log_line "        KHOI PHUC THAT BAI — ban cu con o $snap"
            prune_snapshots "$dir"
        fi
        continue
    fi

    n_changed="$(git -C "$dir" diff --name-only "$graph_hash..$head_hash" 2>/dev/null | wc -l | tr -d ' ')"
    # Repo diff qua lon -> qua nhieu batch. Co model lam tot tung batch nhung
    # khong di het vong lap dai (da gap: len lich 102 batch, chi chay 3).
    # Chan truoc con hon chay 1 tieng roi bi guard rollback.
    if [[ "$MAX_CHANGED" -gt 0 && "$n_changed" -gt "$MAX_CHANGED" ]]; then
        add_result "$name" "too-big" "$n_changed file doi, vuot nguong $MAX_CHANGED — can batch driver"
        log_line "  [TOO-BIG] $name — $n_changed file doi (nguong $MAX_CHANGED), bo qua"
        continue
    fi

    if [[ "$APPLY" -eq 0 ]]; then
        add_result "$name" "would-update" "graph@${graph_hash:0:8} -> HEAD@${head_hash:0:8}, $n_changed file doi"
        log_line "  [DRY] $name — ${graph_hash:0:8}..${head_hash:0:8} ($n_changed file)"
        continue
    fi

    log_line "  [SYNC] $name — ${graph_hash:0:8}..${head_hash:0:8} ($n_changed file)..."
    tpl_before="$(template_ratio "$graph")"
    snap="$(snapshot "$dir")"
    [[ -n "$snap" ]] && log_line "        da chup: $(basename "$snap") (template ${tpl_before}%)"
    t0=$SECONDS
    if [[ "$PARTIAL" -eq 1 ]]; then
        run_agent "$dir" "$PARTIAL_PROMPT"; rc=$?
    else
        run_agent "$dir" "$PROMPT"; rc=$?
    fi
    el=$((SECONDS - t0))

    tier="$(agent_text | grep -o 'SYNC_RESULT=[A-Z_]*' | tail -1 | cut -d= -f2)"
    new_hash="$(jq -r '.gitCommitHash // empty' "$meta" 2>/dev/null)"

    if [[ $rc -eq 124 ]]; then
        # Bi giet giua chung -> graph co the ghi dang do. Tra ve ban truoc khi chay.
        if restore_snapshot "$dir" "$snap"; then
            add_result "$name" "timeout" "qua ${TIMEOUT}s, da huy va khoi phuc ban cu" "$el" "$tier"
            log_line "  [TIMEOUT] $name — qua ${TIMEOUT}s, da khoi phuc snapshot"
            prune_snapshots "$dir"
        else
            add_result "$name" "timeout" "qua ${TIMEOUT}s, da huy — KHONG khoi phuc duoc" "$el" "$tier"
            log_line "  [TIMEOUT] $name — qua ${TIMEOUT}s, KHONG khoi phuc duoc"
        fi
    # Agent hay bao STOPPED thay vi FULL_UPDATE khi no dung lai vi thay doi qua
    # lon — dung theo nghia den ("da dung") nhung sai tier, va cai gia la co
    # --full khong bao gio duoc dung toi. Doi chieu them cau bao cao dac trung
    # cua tier FULL_UPDATE trong phan text.
    elif [[ "$PARTIAL" -eq 0 ]] && { [[ "$tier" == "FULL_UPDATE" ]] \
         || { [[ "$tier" == "STOPPED" || -z "$tier" ]] \
              && agent_text | grep -q 'Recommend running .*understand --full'; }; }; then
        # Prompt cố ý DỪNG ở tier này — đổi quá lớn thì update từng phần cho ra
        # graph sai lệch còn tệ hơn là không update.
        #
        # Không có --full thì repo đổi nhiều sẽ kẹt vĩnh viễn ở đây, không bao
        # giờ đuổi kịp HEAD. Nhưng rebuild là việc đắt nên phải xin phép rõ ràng.
        if [[ "$FULL" -eq 0 ]]; then
            add_result "$name" "needs-full" "doi qua lon — chay lai voi --full de rebuild" "$el" "$tier"
            log_line "  [NEEDS-FULL] $name — can rebuild toan bo (them --full)"
        else
            log_line "  [FULL] $name — dang rebuild toan bo (timeout ${FULL_TIMEOUT}s)..."
            t1=$SECONDS
            run_agent "$dir" "$FULL_PROMPT" "$FULL_TIMEOUT"; rc2=$?
            el2=$((SECONDS - t1))
            new_hash="$(jq -r '.gitCommitHash // empty' "$meta" 2>/dev/null)"
            if [[ $rc2 -eq 124 ]]; then
                restore_snapshot "$dir" "$snap" \
                    && log_line "        da khoi phuc snapshot" \
                    || log_line "        KHONG khoi phuc duoc snapshot"
                add_result "$name" "timeout" "rebuild qua ${FULL_TIMEOUT}s, da huy" "$((el + el2))" "$tier"
                log_line "  [TIMEOUT] $name — rebuild qua ${FULL_TIMEOUT}s"
            elif [[ "$new_hash" == "$head_hash" ]]; then
                add_result "$name" "rebuilt" "rebuild toan bo -> ${head_hash:0:8}" "$((el + el2))" "$tier"
                log_line "  [OK] $name — rebuild xong sau ${el2}s"
            else
                add_result "$name" "failed" "$(agent_tail 8)" "$((el + el2))" "$tier"
                log_line "  [FAIL] $name — rebuild xong ma hash khong doi (rc=$rc2, ${el2}s)"
                restore_snapshot "$dir" "$snap" \
                    && log_line "        da khoi phuc snapshot" \
                    || log_line "        KHOI PHUC THAT BAI — ban cu con o $snap"
                prune_snapshots "$dir"
            fi
        fi
    elif [[ "$new_hash" == "$head_hash" ]]; then
        sum_after="$(graph_sum "$graph")"
        if [[ "$tier" == "SKIP" ]]; then
            # SKIP là hợp lệ: đổi cosmetic, graph không cần đổi, chỉ dời mốc.
            add_result "$name" "no-change" "doi cosmetic, chi doi moc -> ${head_hash:0:8}" "$el" "$tier"
            log_line "  [SKIP] $name — doi cosmetic, khong can index lai"
        elif [[ "$sum_after" != "$sum_before" ]]; then
            tpl_after="$(template_ratio "$graph")"
            if [[ "$tpl_after" -gt $((tpl_before + 20)) ]]; then
                add_result "$name" "degraded" \
                    "summary template ${tpl_before}% -> ${tpl_after}%, da khoi phuc ban cu" "$el" "${tier:-UNKNOWN}"
                log_line "  [DEGRADED] $name — template ${tpl_before}%->${tpl_after}%, KHOI PHUC ban cu"
                restore_snapshot "$dir" "$snap" \
                    && log_line "        da khoi phuc tu snapshot" \
                    || log_line "        KHOI PHUC THAT BAI — ban cu con o $snap"
            else
                sync_project_hash "$graph" "$head_hash" \
                    || log_line "        canh bao: khong dong bo duoc project.gitCommitHash"
                prune_snapshots "$dir"
                dmsg=""
                if [[ "$DOMAIN" -eq 1 ]]; then
                    log_line "        cap nhat domain-graph..."
                    read_domain "$(run_domain "$dir")"
                    dmsg=" $DOMAIN_MSG"
                fi
                # `el` chi la buoc index. Tong moi la cai nguoi doc bao cao can.
                add_result "$name" "updated" "graph -> ${head_hash:0:8} (template ${tpl_after}%)${dmsg}" \
                    "$((el + DOMAIN_SECONDS))" "${tier:-UNKNOWN}" "$DOMAIN_SECONDS"
                log_line "  [OK] $name — xong sau $((el + DOMAIN_SECONDS))s (index ${el}s + domain ${DOMAIN_SECONDS}s, tier=${tier:-?}, template ${tpl_after}%)${dmsg}"
            fi
        else
            # Hash nhảy mà nội dung y nguyên = graph cũ bị dán nhãn mới. Im lặng
            # bỏ qua là để lại graph sai vĩnh viễn, vì lần sau se thay up-to-date.
            add_result "$name" "suspect" "hash -> ${head_hash:0:8} nhung noi dung graph KHONG doi" "$el" "${tier:-UNKNOWN}"
            log_line "  [SUSPECT] $name — hash nhay ma graph khong doi, can --rebuild"
        fi
    else
        # Agent thoat 0 nhung khong ghi meta.json van la mot lan ghi DANG DO:
        # Phase 2 xoa node cu truoc roi moi them node moi, va phan Error Handling
        # cua auto-update-prompt.md con dan agent "ALWAYS save partial results".
        # Them nua, Phase 0 chi `mkdir -p intermediate` chu khong xoa, con Phase 3d
        # moi don — nen batch-*.json dang do nam lai va lan chay SAU se merge nham
        # chung vao (Phase 2: "read each batch-<N>.json and merge results").
        # restore_snapshot() xoa ca .understand-anything nen don luon dong rac do.
        bg=""
        bg_killed && bg="CLI giet background task sau 600s roi thoat 0 — "
        add_result "$name" "failed" "${bg}$(agent_tail 8)" "$el" "$tier"
        log_line "  [FAIL] $name — hash khong doi sau ${el}s (rc=$rc, tier=${tier:-?})"
        [[ -n "$bg" ]] && log_line "        ${bg%— }(xem $(agent_err_path))"
        restore_snapshot "$dir" "$snap" \
            && log_line "        da khoi phuc snapshot, don ca intermediate/ dang do" \
            || log_line "        KHOI PHUC THAT BAI — ban cu con o $snap"
        prune_snapshots "$dir"
    fi
done

echo "$RESULTS" | jq --arg ts "$(date -Iseconds)" --argjson ap "$APPLY" '{
  timestamp: $ts, applied: ($ap == 1), results: .,
  summary: {
    total:          (. | length),
    up_to_date:     ([.[] | select(.status=="up-to-date")]     | length),
    updated:        ([.[] | select(.status=="updated")]        | length),
    rebuilt:        ([.[] | select(.status=="rebuilt")]        | length),
    degraded:       ([.[] | select(.status=="degraded")]       | length),
    too_big:        ([.[] | select(.status=="too-big")]        | length),
    no_change:      ([.[] | select(.status=="no-change")]      | length),
    suspect:        ([.[] | select(.status=="suspect")]        | length),
    would_rebuild:  ([.[] | select(.status=="would-rebuild")]  | length),
    baselined:      ([.[] | select(.status=="baselined")]      | length),
    would_update:   ([.[] | select(.status=="would-update")]   | length),
    would_domain:   ([.[] | select(.status=="would-domain")]   | length),
    would_baseline: ([.[] | select(.status=="would-baseline")] | length),
    no_baseline:    ([.[] | select(.status=="no-baseline")]    | length),
    needs_full:     ([.[] | select(.status=="needs-full")]     | length),
    timeout:        ([.[] | select(.status=="timeout")]        | length),
    not_a_repo:     ([.[] | select(.status=="not-a-repo")]     | length),
    failed:         ([.[] | select(.status=="failed")]         | length),
    seconds_total:  ([.[] | .seconds]                          | add),
    seconds_domain: ([.[] | .seconds_domain // 0]              | add)
  }
}'

log_line "=== xong ==="
bad=$(echo "$RESULTS" | jq '[.[] | select(.status=="failed" or .status=="timeout" or .status=="needs-full" or .status=="suspect" or .status=="degraded")] | length')
[[ "$bad" -gt 0 ]] && { log_line "$bad repo can can thiep"; exit 1; }
exit 0
