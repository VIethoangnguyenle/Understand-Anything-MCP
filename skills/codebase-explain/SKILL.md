---
name: codebase-explain
description: Use when cần giải thích sâu file, symbol, module, class, function hoặc business flow của codebase qua Understand-Anything MCP.
aliases:
  - /codebase-explain
tags:
  - codebase
  - explanation
  - ua-server
  - architecture
argument-hint: "[project-name] [file-path|symbol|module|business-flow]"
user-invocable: true
---

# Codebase Explain

Giải thích implementation hiện tại bằng evidence read-only từ Understand-Anything MCP
(`ua-server-mcp`). Output luôn bằng tiếng Việt và giữ nguyên code identifiers, tool
names, project names, node IDs và paths do server trả về.

## Nguyên tắc bắt buộc

1. Skill này UA-only. Không đọc checkout source local, local code graph hoặc dữ
   liệu implementation local để thay thế evidence của server.
2. Không dùng shell, HTTP client hoặc filesystem search để thay MCP.
3. Server chỉ phục vụ đọc. Không claim đã sửa source và không thực hiện write.
4. Main agent sở hữu session cache: reuse cached project list và graph stats;
   không ghi cache ra file.
5. Detect tool namespace theo semantic normalization dưới đây; không phụ thuộc
   riêng dạng hyphen hoặc underscore.

## Runtime Tool Detection

Runtime có thể expose tên server bằng dấu gạch ngang hoặc gạch dưới. Detect tool
namespace bằng cách normalize server identifier: bỏ runtime wrapper, đổi về chữ
thường, xóa `-` và `_`, rồi exact-compare với `uaservermcp`. Ví dụ hợp lệ gồm
`mcp__ua_server_mcp__*`, `mcp__ua-server-mcp__*`, `ua_server_mcp.*` và
`ua-server-mcp.*`. Không hardcode một namespace duy nhất trong output hoặc prompt.

Nếu không thấy bất kỳ tool nào của server, áp dụng Failure handling; không thử
shell/curl để thay MCP.

## Argument contract

Input:

```text
[project-name] [file-path|symbol|module|business-flow]
```

Parse theo thứ tự:

1. Lấy cached `list_projects` nếu session đã có. Nếu chưa có, main agent gọi
   `list_projects` đúng một lần và cache normalized project names.
2. Trim input. Nếu token đầu exact-match một cached project name sau normalize
   `trim + lowercase`, token đó là explicit project; phần còn lại là target.
3. Nếu không có explicit project, toàn bộ input là target. Chỉ chấp nhận omission
   khi `list_projects` trả về đúng một project.
4. Explicit project luôn thắng inferred project. Không fuzzy-select project gần
   giống.
5. Target rỗng, project chưa resolve, hoặc context còn từ hai project candidate
   trở lên là ambiguity. Tạo Living HTML review và hard stop.

## Freshness gate

Sau khi resolve exact project, gọi hoặc reuse cached `get_graph_stats` cho project
đó trước mọi claim quan trọng. Gán đúng một trạng thái:

| Freshness | Claim limit |
|---|---|
| `FRESH` | Claim trực tiếp trong đúng phạm vi evidence server trả về. |
| `STALE` | Claim là hiện trạng tại graph commit; không khẳng định source mới nhất. |
| `UNKNOWN` | Claim chỉ là evidence tham khảo; không khẳng định current implementation. |

Luôn nêu graph commit khi server trả về. Không dùng project list để claim graph
usable khi `get_graph_stats` lỗi.

## Target routing

Phân loại bằng intent người dùng và hình dạng target. Nếu không xác định chắc
route, tạo Living HTML review thay vì đoán.

| Target | Tool đầu tiên | Quy tắc chọn |
|---|---|---|
| File path | `search_by_file_path` | Dùng target path làm substring query; filter node type khi intent đã rõ. |
| Symbol, module, class, function | `query_nodes` | Query tên/qualified name; dùng node type filter khi input xác định loại. |
| Business flow | `get_domain_flow_detail` | Dùng flow name người dùng cung cấp; chỉ nhận kết quả khi flow resolve không mơ hồ. |

Kết quả không có candidate đi theo `No match`. Mọi result từ tool fuzzy-capable
chỉ là candidate cho tới khi pass validation bên dưới. Từ hai candidate hợp lý
trở lên đi theo `Living HTML ambiguity gate`; không tự chọn candidate đầu tiên.

### Candidate validation gate

`search_by_file_path`, `query_nodes` và `get_domain_flow_detail` hỗ trợ fuzzy
matching. Một result duy nhất không tự động chứng minh target đã resolve. Result
count, rank, score, substring hoặc semantic similarity không phải identity
evidence.

Validate trước khi gọi bất kỳ node deep-dive tool nào:

1. **File path:** Khi response cung cấp path/filePath canonical, chỉ accept
   `exact-match returned path` với target sau `trim` và normalize path separator
   về `/`. Không accept basename-only, suffix-only hoặc substring match.
2. **Symbol, module, class, function:** Chỉ accept khi response có
   exact-match returned `name` hoặc qualified name với target. Giữ comparison
   case-sensitive cho code identifier; không accept partial-name hoặc
   summary-only match.
3. **Business flow:** Chỉ accept exact returned flow name khi response cung cấp
   canonical name. Nếu canonical field không có, response phải có evidence trực
   tiếp và rõ ràng mapping target input tới đúng flow đó.
4. Nếu một single weak match không exact hoặc không có clear identity evidence,
   route tới `Living HTML ambiguity gate`. Không giải thích candidate đó và
   không gọi deep-dive tool để hợp thức hóa fuzzy selection.

No-fuzzy rule giữ nguyên cho cả một-result và nhiều-result response. Nếu field
cần để exact-validate không được server trả về và evidence còn yếu, coi target là
ambiguous, không coi là `No match`.

## Node deep dive

Khi route resolve một node ID, gọi theo thứ tự:

1. `get_node_detail` — metadata, path, layer, tags, complexity do server cung cấp.
2. `get_node_source` — source excerpt và extraction metadata.
3. `get_relationships` với direction phù hợp, mặc định `both` — incoming/outgoing
   dependencies.
4. `trace_call_chain` — execution path bắt đầu từ node đã resolve.

Với business flow, dùng ordered steps và linked code nodes từ
`get_domain_flow_detail`. Chỉ deep dive linked node cần cho claim. Nếu phải chọn
giữa nhiều linked node hợp lý, mở Living HTML review trước khi tiếp tục.

Chỉ gọi tool bổ sung khi câu hỏi cần đúng context đó:

- `get_layer_info`: xác nhận architectural layer.
- `get_tour`: đặt target vào guided architecture context.
- `find_path`: chứng minh connection giữa hai node.
- `find_impact`: phân tích impact trong cùng một project; không claim
  cross-project impact.
- `get_class_hierarchy`: giải thích extends/implements.

Không gọi tool bổ sung để tạo độ dài. Không suy ra path, dependency, call step
hoặc business behavior ngoài response của server.

## Remote source scope

- Chỉ mô tả source excerpt server trả về.
- Nếu `get_node_source` báo truncate, nêu rõ line scope/extraction scope đã nhận
  và ghi phần ngoài scope chưa được kiểm chứng.
- Không extrapolate nội dung trước hoặc sau excerpt.
- Chỉ ghi `Lines` khi server trả line number hoặc line range.

## Output contract

Viết output tiếng Việt theo đúng cấu trúc:

```markdown
# Giải thích: <target>

## 1. Phạm vi và freshness
- Project, target, target type, graph commit nếu có, `FRESH|STALE|UNKNOWN`.
- Giới hạn evidence hoặc source scope.

## 2. Vai trò kiến trúc
- Layer, trách nhiệm và vị trí trong execution context.

## 3. Cấu trúc nội bộ
- Class/function/module members và logic chính được evidence hỗ trợ.

## 4. Dependencies
- Incoming callers/importers và outgoing callees/imports/dependencies.

## 5. Data flow
- Input, processing steps, state/data transformations và output.

## 6. Business impact
- Business flow/entity/use case bị tác động trực tiếp theo evidence.

## 7. Error paths
- Validation, exception, failure branch, retry hoặc fallback được
  source/call-chain evidence hỗ trợ.

## 8. Complexity
- Complexity metadata, branching hoặc coupling server trả về; không tự chấm mức
  độ khi thiếu evidence.

## 9. Evidence
- Evidence entries theo contract bên dưới.
```

Nếu server không đủ evidence cho section nào, ghi rõ `Chưa đủ evidence` và tool
đã được gọi. Không lấp khoảng trống bằng kiến thức suy đoán.

### Evidence format

Mọi claim từ server phải map tới một evidence entry:

```text
Source: UA
Project: <project>
Freshness: <FRESH | STALE | UNKNOWN>
Tool: <tool-name>
Claim: <kết luận được response hỗ trợ>
```

Chỉ thêm field khi server thực sự trả giá trị:

```text
Node ID: <node-id>
Path: <file-path>
Lines: <line hoặc đoạn line>
```

Giữ evidence bucket riêng cho từng project. Không chuyển node ID, path, commit
hoặc claim từ project này sang project khác.

## Living HTML ambiguity gate

Áp dụng khi project hoặc node/flow ambiguous, target thiếu, hoặc target route
chưa xác định chắc. Không hỏi lựa chọn trong chat.

### File location

- Có artifact/spec Markdown hiện tại: tạo hoặc cập nhật
  `review-codebase-explain-<target-slug>.html` trong cùng thư mục artifact.
- Standalone invocation: tạo trong thư mục làm việc hiện tại.
- Tạo `<target-slug>` bằng lowercase, thay chuỗi ký tự không phải chữ/số bằng
  `-`, trim `-`; target rỗng dùng `target`.

### Append-only turn

1. Nếu file đã tồn tại, đọc section cuối, lấy `Turn N`, rồi dùng `Turn N+1`.
2. Không sửa hoặc xóa turn cũ. Append một `<section>` mới ở cuối turn container.
3. Header: `Turn N — Chọn project/target` kèm timestamp hiện tại.
4. Liệt kê candidate đánh số và evidence làm candidate ambiguous.
5. Mỗi nhóm câu hỏi có một `<textarea>` prefill sẵn recommendation và lý do ngắn.
6. Mỗi textarea có nút `Copy prompt`; turn có nút `Copy ALL` gộp mọi textarea
   trong turn đó.
7. HTML self-contained, CSS/JS inline, không CDN, theme warm monochrome sáng;
   code snippet dùng `<pre><code>`.
8. Khi mở file, chạy:

```javascript
document.querySelector('section:last-of-type')?.scrollIntoView();
```

Sau khi ghi HTML, chỉ báo path để người dùng mở. Hard stop toàn bộ routing phụ
thuộc lựa chọn cho tới khi người dùng paste selection vào chat.

## Failure handling

### Server unavailable hoặc call failure

Dừng, không fallback sang implementation local. Báo ngắn:

- tool/operation thất bại;
- project và target đang xử lý nếu đã resolve;
- không có codebase explanation vì thiếu evidence;
- cách khởi động lại MCP server client đang dùng.

Không in token, Authorization header hoặc secret-bearing diagnostic.

### No match

Báo đúng các field:

```text
Query: <target>
Project: <resolved-project>
Freshness: <FRESH | STALE | UNKNOWN>
Route: <path | symbol | business-flow>
Tool: <tool-name>
Result: No match
```

Không phát minh node ID, path hoặc lines. Không đổi sang project khác và không
fuzzy-select một target khác để tiếp tục.

## Completion checklist

- [ ] Tool namespace được detect theo normalization rules.
- [ ] Project exact-resolved và thuộc cached project list.
- [ ] `get_graph_stats` đã gọi hoặc reuse trước claim quan trọng.
- [ ] Target đi đúng path/symbol/business-flow route.
- [ ] Candidate pass exact/clearly-evidenced identity validation; single weak
  match đã đi Living HTML thay vì deep dive.
- [ ] Node deep dive dùng đủ detail/source/relationships/call chain khi có node.
- [ ] Output đủ chín section tiếng Việt.
- [ ] Mọi claim có Source/Project/Freshness/Tool/Claim.
- [ ] Node ID/Path/Lines chỉ xuất hiện khi server trả.
- [ ] Truncated source có scope warning, không extrapolate.
- [ ] Ambiguity đã tạo Living HTML và hard stop.
- [ ] Server failure đã dừng và nêu cách restart.
