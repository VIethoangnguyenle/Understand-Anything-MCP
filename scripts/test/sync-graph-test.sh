#!/usr/bin/env bash
# Harness cho sync-graph.sh — dung repo git gia + `docker` gia de chay het cac
# nhanh ket qua ma khong can VM, container, hay model nao.
#
#   ./sync-graph-test.sh                    # test sync-graph.sh ben canh
#   ./sync-graph-test.sh /duong/dan/khac.sh # test mot ban khac
#
# Ba case tuong ung ba su co that (xem git log cua sync-graph.sh).
set -uo pipefail
S="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SG="${1:-$S/../sync-graph.sh}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sync-graph-test.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
ROOT="$WORK/sandbox"; LOGS="$WORK/logs"
rm -rf "$ROOT" "$LOGS"; mkdir -p "$ROOT" "$LOGS"

mkrepo() {  # mkrepo <name>
  local d="$ROOT/$1"; mkdir -p "$d/.understand-anything"
  git -C "$d" init -q 2>/dev/null
  git -C "$d" config user.email t@t; git -C "$d" config user.name t
  echo one >"$d/a.ts"; git -C "$d" add -A; git -C "$d" commit -qm one
  local old; old="$(git -C "$d" rev-parse HEAD)"
  printf '{"gitCommitHash":"%s","version":"1.0.0"}\n' "$old" >"$d/.understand-anything/meta.json"
  echo '{"nodes":[{"id":"old","type":"file","summary":"a real summary"}],"project":{}}' \
      >"$d/.understand-anything/knowledge-graph.json"
  echo two >>"$d/a.ts"; git -C "$d" add -A; git -C "$d" commit -qm two   # HEAD != meta
}

pass=0; fail=0
ck() { if eval "$2"; then echo "  PASS  $1"; pass=$((pass+1)); else echo "  FAIL  $1"; fail=$((fail+1)); fi; }

run() { local sc="$1"; shift; SCENARIO="$sc" PATH="$S/bin:$PATH" \
        SYNC_GRAPH_LOG_DIR="$LOGS" SYNC_GRAPH_TIMEOUT=30 \
        bash "$SG" --apply --partial "$@" "$ROOT" 2>"$LOGS/run-$sc.err"; }

echo "=== Case 1: agent thoat 0 nhung khong ghi meta.json ==="
mkrepo r1
J="$(run bgkill)"
UA="$ROOT/r1/.understand-anything"
ck "status = failed"                 '[[ "$(jq -r ".results[0].status" <<<"$J")" == failed ]]'
ck "detail neu ro CLI giet bg task"  'jq -r ".results[0].detail" <<<"$J" | grep -q "giet background task"'
ck "tier doc duoc (stream sach)"     '[[ "$(jq -r ".results[0].tier" <<<"$J")" == PARTIAL_UPDATE ]]'
ck "intermediate/ dang do da bi don" '[[ ! -e "$UA/intermediate" ]]'
ck "batch-1.json mo coi da bi don"   '[[ ! -e "$UA/intermediate/batch-1.json" ]]'
ck "graph khoi phuc ve ban cu"       '[[ "$(jq -r ".nodes[0].id" "$UA/knowledge-graph.json")" == old ]]'
ck "khong con snapshot mo coi"       '[[ -z "$(ls -d "$ROOT"/r1/.understand-anything.bak-* 2>/dev/null)" ]]'
ck "stderr ra file rieng"            'grep -q "Background tasks still running" "$LOGS"/r1-*.stderr.log'
ck "AGENT_LOG la JSON hop le 100%"   '( while read -r l; do jq -e . >/dev/null 2>&1 <<<"$l" || exit 1; done < <(cat "$LOGS"/r1-*[0-9].log) )'

echo "=== Case 2: agent chay thanh cong, stderr co nhieu ==="
mkrepo r2; rm -rf "$ROOT/r1"
J="$(run ok)"
ck "status = updated"                '[[ "$(jq -r ".results[0].status" <<<"$J")" == updated ]]'
ck "tier = PARTIAL_UPDATE"           '[[ "$(jq -r ".results[0].tier" <<<"$J")" == PARTIAL_UPDATE ]]'
ck "node moi duoc ghi"               'jq -e ".nodes[]|select(.id==\"new\")" "$ROOT/r2/.understand-anything/knowledge-graph.json" >/dev/null'

echo "=== Case 3: timeout (rc=124) van khoi phuc + don snapshot ==="
mkrepo r3; rm -rf "$ROOT/r2"
J="$(run slow)"
ck "status = timeout"                '[[ "$(jq -r ".results[0].status" <<<"$J")" == timeout ]]'
ck "graph khoi phuc ve ban cu"       '[[ "$(jq -r ".nodes[0].id" "$ROOT/r3/.understand-anything/knowledge-graph.json")" == old ]]'
ck "khong con snapshot mo coi"       '[[ -z "$(ls -d "$ROOT"/r3/.understand-anything.bak-* 2>/dev/null)" ]]'

echo "=== Case 4: --domain, thoi gian buoc domain phai vao bao cao ==="
mkrepo r4; rm -rf "$ROOT/r3"
J="$(run ok --domain)"
ck "status = updated"                '[[ "$(jq -r ".results[0].status" <<<"$J")" == updated ]]'
ck "detail co domain=da-cap-nhat"    'jq -r ".results[0].detail" <<<"$J" | grep -q "domain=da-cap-nhat"'
ck "seconds_domain > 0"              '[[ "$(jq -r ".results[0].seconds_domain" <<<"$J")" -gt 0 ]]'
ck "seconds >= seconds_domain"       '[[ "$(jq -r ".results[0].seconds" <<<"$J")" -ge "$(jq -r ".results[0].seconds_domain" <<<"$J")" ]]'
ck "summary co seconds_domain"       '[[ "$(jq -r ".summary.seconds_domain" <<<"$J")" -gt 0 ]]'

echo; echo "pass=$pass fail=$fail"; [[ $fail -eq 0 ]]
