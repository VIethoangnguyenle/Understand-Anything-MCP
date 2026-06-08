# 🧠 Understand-Anything MCP Server

**MCP Server giúp trợ lý AI hiểu sâu bất kỳ codebase nào thông qua Knowledge Graph.**

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-tương_thích-green.svg)](https://modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

</div>

---

## Giới thiệu

MCP Server này tải các Knowledge Graph được tạo bởi [Understand-Anything](https://github.com/understand-anything) và cung cấp chúng dưới dạng các tool có thể truy vấn cho bất kỳ trợ lý AI tương thích MCP nào (Gemini CLI, Claude Desktop, Cursor, v.v.).

Server hỗ trợ **hai loại đồ thị** đồng thời cho mỗi dự án:

| Đồ thị | Tệp | Nội dung |
|---|---|---|
| **Code Graph** | `knowledge-graph.json` | Files, functions, classes, imports, chuỗi gọi hàm, các tầng kiến trúc |
| **Domain Graph** | `domain-graph.json` | Nghiệp vụ (domains), luồng xử lý (flows), bước (steps), thực thể, quy tắc nghiệp vụ |

**Hỗ trợ đa dự án** — Tải N dự án cùng lúc và truy vấn bất kỳ dự án nào. AI tự động nhận diện dự án phù hợp dựa trên ngữ cảnh workspace.

### Tính năng nổi bật

- 🔍 **Tìm kiếm mờ (Fuzzy search)** — Tìm kiếm có trọng số (tên 3x > mô tả 1.5x > tags 1x) sử dụng `rapidfuzz`
- 🏗️ **Tầng kiến trúc** — Truy vấn theo layer (controller, service, repository, v.v.)
- 🌊 **Truy vết chuỗi gọi hàm** — Duyệt BFS theo các lời gọi hàm
- 💥 **Phân tích vùng ảnh hưởng** — Tìm tất cả node bị ảnh hưởng khi thay đổi một node (BFS ngược)
- 🎯 **Phát hiện entry point** — Nhận diện API endpoint và các hàm không được gọi bởi hàm khác
- 🏢 **Tri thức nghiệp vụ** — Domains, flows, steps, thực thể và quy tắc nghiệp vụ
- 📖 **Trích xuất mã nguồn đa ngôn ngữ** — Đọc source code thực tế của bất kỳ node nào, hỗ trợ trích xuất symbol-level cho **Java, Kotlin, TypeScript, JavaScript, Python, Go, Rust, C#**
- 🔗 **Tìm đường ngắn nhất** — BFS vô hướng giữa hai node bất kỳ trong đồ thị
- 🏛️ **Cây kế thừa** — Truy vết extends/implements lên và xuống toàn bộ hệ thống phân cấp class
- 📁 **Tìm kiếm theo đường dẫn** — Tìm tất cả node theo package/module/thư mục path
- 🔄 **Tự động tải lại** — Phát hiện khi file graph thay đổi trên đĩa và tự động reload
- ✅ **Phân tích độ mới** — So sánh commit hash của graph với HEAD hiện tại qua `git diff`
- ⚡ **Edge Resolution Layer** — Class và function node tự động kế thừa quan hệ từ file cha, tra cứu O(degree) qua edge index

---

## Bắt đầu nhanh

### Yêu cầu

- Python ≥ 3.12
- Trình quản lý package [`uv`](https://docs.astral.sh/uv/)
- Một dự án đã được tạo graph bởi [Understand-Anything](https://github.com/understand-anything) (thư mục `.understand-anything/`)

### Cài đặt & Chạy

```bash
# Clone repository
git clone https://github.com/VIethoangnguyenle/Understand-Anything-MCP.git
cd Understand-Anything-MCP

# Cài đặt dependencies
uv sync

# Chạy với MCP Inspector (để test/debug)
PROJECT_ROOTS=/đường/dẫn/tới/dự-án npx @modelcontextprotocol/inspector uv run server.py

# Chạy MCP dev server
PROJECT_ROOTS=/đường/dẫn/tới/dự-án mcp dev server.py
```

### Đa dự án

Đặt `PROJECT_ROOTS` là danh sách đường dẫn phân cách bằng dấu phẩy:

```bash
PROJECT_ROOTS=/đường/dẫn/dự-án-a,/đường/dẫn/dự-án-b uv run server.py
```

Mỗi tool đều nhận tham số `project` tùy chọn. Nếu chỉ có một dự án được tải, nó sẽ được sử dụng tự động.

---

## Cấu hình MCP Client

### Gemini CLI / Antigravity

Thêm vào `~/.gemini/antigravity/mcp_config.json`:

```json
{
  "understand-anything": {
    "command": "uv",
    "args": ["--directory", "/đường/dẫn/tuyệt/đối/tới/Understand-Anything-MCP", "run", "server.py"],
    "env": {
      "PROJECT_ROOTS": "/đường/dẫn/tới/dự-án-a,/đường/dẫn/tới/dự-án-b"
    }
  }
}
```

### Claude Desktop

Thêm vào `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "understand-anything": {
      "command": "uv",
      "args": ["--directory", "/đường/dẫn/tuyệt/đối/tới/Understand-Anything-MCP", "run", "server.py"],
      "env": {
        "PROJECT_ROOTS": "/đường/dẫn/tới/dự-án"
      }
    }
  }
}
```

### Cursor / Các MCP Client khác

Sử dụng cùng cấu trúc — đặt `command` là `uv`, truyền đường dẫn server qua `--directory`, và cấu hình `PROJECT_ROOTS` trong `env`.

---

## Danh sách Tools (15 tools)

### Khám phá & Tổng quan

| Tool | Mô tả |
|---|---|
| `list_projects` | Liệt kê tất cả dự án đã đăng ký kèm số lượng node/edge và thông tin domain |
| `get_graph_stats` | Thống kê toàn diện: phân bố type, layers, phân tích độ mới của graph |
| `get_tour` | Tour hướng dẫn dự án — các điểm dừng được chọn lọc giải thích các thành phần chính |

### Truy vấn Code Graph

| Tool | Mô tả |
|---|---|
| `query_nodes` | Tìm kiếm mờ có trọng số theo từ khóa. Hỗ trợ lọc `node_type` và phân trang |
| `get_node_detail` | Chi tiết đầy đủ của một node theo ID: đường dẫn, layer, độ phức tạp, tags, số lượng quan hệ |
| `get_node_source` | Đọc mã nguồn thực tế của node. Trích xuất symbol-level đa ngôn ngữ (Java/Kotlin/TS/Python/Go/...) |
| `get_relationships` | Tất cả node liên kết kèm loại quan hệ. Class/function tự động kế thừa edge từ file cha |
| `trace_call_chain` | Cây gọi hàm BFS từ một function (theo edge `calls`, độ sâu có thể cấu hình) |
| `get_layer_info` | Liệt kê các tầng kiến trúc hoặc lấy tất cả node trong một layer cụ thể |
| `find_entry_points` | Các function không được gọi bởi function khác — tiềm năng là API endpoint |
| `find_impact` | Vùng ảnh hưởng: tất cả node bị ảnh hưởng nếu node này thay đổi (BFS ngược) |

### Truy vấn nâng cao 🆕

| Tool | Mô tả |
|---|---|
| `find_path` | Tìm đường đi ngắn nhất giữa hai node (BFS vô hướng, tối đa 10 hop) |
| `get_class_hierarchy` | Cây kế thừa extends/implements — hỗ trợ hướng `up`/`down`/`both` |
| `search_by_file_path` | Tìm node theo pattern đường dẫn file (case-insensitive, lọc theo type) |

### Truy vấn Domain Graph

| Tool | Mô tả |
|---|---|
| `get_domain_overview` | Tổng quan tất cả domain nghiệp vụ kèm flows, thực thể, và mô tả |
| `get_domain_detail` | Chi tiết sâu về một domain: thực thể, quy tắc nghiệp vụ, flows, steps |

---

## Kiến trúc

```
┌─────────────────────────────────────────────────────┐
│                   MCP Client                        │
│          (Gemini CLI, Claude, Cursor...)             │
└────────────────────┬────────────────────────────────┘
                     │ stdio (MCP Protocol)
┌────────────────────▼────────────────────────────────┐
│                server.py                            │
│  ┌──────────────────────────────────────────────┐   │
│  │           FastMCP (15 tools)                 │   │
│  │  list_projects · query_nodes · find_impact   │   │
│  │  find_path · get_class_hierarchy · ...       │   │
│  └──────────────────┬───────────────────────────┘   │
│                     │                               │
│  ┌──────────────────▼───────────────────────────┐   │
│  │       Multi-Project Registry                 │   │
│  │   cache theo mtime · tự động reload · resolve│   │
│  └──────────────────┬───────────────────────────┘   │
└─────────────────────┼───────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────┐
│              kg_loader.py                           │
│  ┌────────────────────────────────────────────────┐ │
│  │  Tầng Dữ liệu (Data Layer)                    │ │
│  │  Node · Edge · LayerInfo · TourStop            │ │
│  │  DomainNode · DomainEdge · ProjectGraph        │ │
│  ├────────────────────────────────────────────────┤ │
│  │  Edge Resolution Layer                         │ │
│  │  class/function → file edge inheritance        │ │
│  │  O(1) node index · O(degree) edge index        │ │
│  ├────────────────────────────────────────────────┤ │
│  │  Query Engine                                  │ │
│  │  fuzzy search · BFS traversal · impact analysis│ │
│  │  shortest path · class hierarchy · path search │ │
│  ├────────────────────────────────────────────────┤ │
│  │  Source Extraction (đa ngôn ngữ)               │ │
│  │  brace-counting (Java/Kotlin/TS/JS/Go/Rust/C#) │ │
│  │  indent-tracking (Python)                      │ │
│  └────────────────────────────────────────────────┘ │
└─────────────────────┬───────────────────────────────┘
                      │ đọc JSON
┌─────────────────────▼───────────────────────────────┐
│     .understand-anything/                           │
│     ├── knowledge-graph.json  (đồ thị code-level)   │
│     ├── domain-graph.json     (đồ thị nghiệp vụ)    │
│     └── meta.json             (metadata phân tích)   │
└─────────────────────────────────────────────────────┘
```

### Cấu trúc tệp

```
Understand-Anything-MCP/
├── server.py          # MCP server — định nghĩa 15 tools, registry đa dự án
├── kg_loader.py       # Bộ tải graph & query engine — data models, search, traversal, resolution
├── pyproject.toml     # Cấu hình dự án — dependencies: mcp[cli], rapidfuzz
├── tests/             # Bộ test tự động
│   ├── test_kg_loader.py    # 46 unit tests cho core loader & query engine
│   └── fixtures/            # Dữ liệu test JSON mẫu
│       ├── knowledge-graph.json
│       └── domain-graph.json
├── uv.lock            # Dependencies đã khóa phiên bản
└── README.md
```

---

## Biến môi trường

| Biến | Bắt buộc | Mô tả |
|---|---|---|
| `PROJECT_ROOTS` | **Có** | Danh sách đường dẫn tuyệt đối phân cách bằng dấu phẩy tới các dự án có thư mục `.understand-anything/` |
| `UPSTREAM_ROOTS` | Không | Danh sách đường dẫn tới thư mục gốc của thư viện upstream/dùng chung (để resolve source code của upstream node) |

---

## Cách hoạt động

1. **Khi khởi động**, server quét `PROJECT_ROOTS` và tải `knowledge-graph.json` + `domain-graph.json` từ thư mục `.understand-anything/` của mỗi dự án.

2. **Index được xây dựng** trong bộ nhớ:
   - `_node_index`: tra cứu node theo ID — O(1)
   - `_edges_by_source` / `_edges_by_target`: tra cứu edge — O(degree)
   - `_domain_edges_by_source`: index riêng cho domain graph
   - Layer enrichment: gán `layer` vào từng node dựa trên ánh xạ layer

3. **Edge Resolution Layer** — Khi truy vấn quan hệ của class/function node:
   - Resolve tới parent file qua edge `contains`
   - Kế thừa outgoing edges từ file cha (imports, contains, v.v.)
   - Loại bỏ self-reference và deduplicate

4. **Khi một tool được gọi**, server kiểm tra mtime của file graph trên đĩa và tự động tải lại nếu cần.

5. **Tìm kiếm mờ** sử dụng `rapidfuzz` với điểm số có trọng số — kết quả khớp tên được đánh trọng số cao gấp 3 lần so với khớp mô tả, kèm bonus cho khớp chính xác chuỗi con.

6. **Trích xuất mã nguồn đa ngôn ngữ** — Tự động nhận diện ngôn ngữ qua extension và chọn chiến lược phù hợp:
   - **Brace-counting**: Java, Kotlin, TypeScript, JavaScript, Go, Rust, C#
   - **Indent-tracking**: Python

7. **Kiểm tra độ mới** chạy lệnh `git diff <commit_phân_tích>..HEAD` để phát hiện số lượng file code đã thay đổi kể từ lần tạo graph gần nhất.

---

## Ví dụ sử dụng

Sau khi kết nối với MCP client, AI có thể sử dụng các tool một cách tự nhiên:

```
Người dùng: "Luồng xác thực hoạt động như thế nào?"

AI sử dụng: query_nodes(query="authentication") → tìm các node liên quan
AI sử dụng: get_domain_detail(domain_name="authentication") → lấy thông tin domain đầy đủ
AI sử dụng: trace_call_chain(start_node_id="...loginUser") → truy vết cây gọi hàm
```

```
Người dùng: "Nếu tôi thay đổi PaymentService thì ảnh hưởng gì?"

AI sử dụng: query_nodes(query="PaymentService") → tìm node
AI sử dụng: find_impact(node_id="...PaymentService") → phân tích vùng ảnh hưởng
```

```
Người dùng: "PaymentService kế thừa từ class nào?"

AI sử dụng: query_nodes(query="PaymentService") → tìm node
AI sử dụng: get_class_hierarchy(class_id="class:PaymentService", direction="up") → cây kế thừa
```

```
Người dùng: "AuthService và PaymentGateway liên quan thế nào?"

AI sử dụng: find_path(source_id="class:AuthService", target_id="class:PaymentGateway") → đường đi ngắn nhất
```

```
Người dùng: "Tất cả file trong package transfer?"

AI sử dụng: search_by_file_path(path_pattern="transfer", node_type="file") → danh sách file
```

---

## Phát triển

```bash
# Cài đặt dependencies
uv sync

# Chạy unit tests
uv run pytest tests/ -v

# Chạy test với MCP Inspector
PROJECT_ROOTS=/đường/dẫn/tới/dự-án npx @modelcontextprotocol/inspector uv run server.py

# Log được ghi ra stderr (stdout được dành riêng cho MCP stdio protocol)
```

---

## Giấy phép

MIT

---

<div align="center">

**Được xây dựng cho hệ sinh thái [Understand-Anything](https://github.com/understand-anything)**

*Giúp trợ lý AI hiểu sâu bất kỳ codebase nào* 🚀

</div>
