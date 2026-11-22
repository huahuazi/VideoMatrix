#!/usr/bin/env bash
# 启动 vmagent MCP stdio 服务器(给 Claude Desktop / Cursor / Codex 用)
cd "$(dirname "$0")"
exec python3 -m vmagent serve-mcp --ndjson "$@"
