#!/usr/bin/env bash
# vmagent 安装脚本(macOS / Linux)
set -euo pipefail
cd "$(dirname "$0")"

echo
echo '============================================================'
echo '  vmagent installer — VideoMatrix Agent Bridge'
echo '============================================================'
echo

PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)' 2>/dev/null; then
      PY="$candidate"; break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo '  [X] 找不到 Python 3.8+'
  echo '      请先安装: https://www.python.org/downloads/'
  exit 2
fi

echo '[1] Python:'
"$PY" -c 'import sys; print("    " + sys.version.split()[0] + "  " + sys.executable)'
echo
echo '[2] 检查环境...'
"$PY" -m vmagent.install check || true
echo
echo '[3] 注册到 Agent 客户端...'
"$PY" -m vmagent.install

echo
echo '============================================================'
echo '  完成。下一步:'
echo '    1. 打开 VideoMatrix 桌面程序'
echo '    2. 重启 Agent 客户端(Claude Desktop / Cursor / Codex)'
echo '    3. 让它调用 vm_discover 验证连接'
echo '============================================================'
echo
echo "随时检查状态: $PY -m vmagent.install doctor"

