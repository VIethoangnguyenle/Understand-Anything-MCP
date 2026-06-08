# KG MCP Server

Multi-project MCP Server for [Understand-Anything](https://github.com/understand-anything) knowledge graphs.

Loads both `knowledge-graph.json` (code-level) and `domain-graph.json` (business-level) per project. Supports N projects simultaneously via `PROJECT_ROOTS` env var.

## Quick Start

```bash
# Install dependencies
cd kg-mcp-server
uv sync

# Run with MCP Inspector (for testing)
PROJECT_ROOTS=/path/to/your-project npx @modelcontextprotocol/inspector uv run server.py

# Run dev server
PROJECT_ROOTS=/path/to/your-project mcp dev server.py
```

## Tools (11)

| Tool | Description |
|---|---|
| `list_projects` | List registered projects + stats |
| `get_graph_stats` | Node/edge counts, type distributions |
| `query_nodes` | Fuzzy search nodes by keyword |
| `get_node_detail` | Full details of a node by ID |
| `get_relationships` | Connected nodes + relation types |
| `trace_call_chain` | BFS call tree from a function |
| `get_layer_info` | Architectural layers |
| `get_domain_overview` | All business domains + flows |
| `get_domain_detail` | Domain entities, rules, flows, steps |
| `find_entry_points` | Functions not called by others |
| `find_impact` | Blast radius analysis |

## Antigravity / Gemini CLI Config

In `~/.gemini/antigravity/mcp_config.json`:

```json
{
  "understand-anything": {
    "command": "uv",
    "args": ["--directory", "/absolute/path/to/kg-mcp-server", "run", "server.py"],
    "env": {
      "PROJECT_ROOTS": "/path/to/project-a,/path/to/project-b"
    }
  }
}
```

## Multi-project

Set `PROJECT_ROOTS` to comma-separated project root paths. Each tool accepts an optional `project` parameter — AI auto-detects based on workspace context.
# Understand-Anything-MCP
