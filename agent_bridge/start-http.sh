#!/usr/bin/env bash
# 启动 vmagent HTTP 服务(给任意 agent 框架用)
cd "$(dirname "$0")"
exec python3 -m vmagent serve-http --port "${1:-8770}"
