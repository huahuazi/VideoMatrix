#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vmagent.install — 安装、自检、注册到 Agent 客户端。

用法:
  python -m vmagent.install              交互式安装向导(推荐)
  python -m vmagent.install check        只检查环境
  python -m vmagent.install claude       注册到 Claude Desktop
  python -m vmagent.install cursor       注册到 Cursor
  python -m vmagent.install codex        注册到 Codex CLI
  python -m vmagent.install print        打印配置片段
  python -m vmagent.install doctor       检查已注册情况
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SERVER_KEY = "videomatrix-bridge"
BACKUP_SUFFIX = ".vmagent.bak"
MIN_PYTHON = (3, 8)
DRY_RUN = False


# ---------------------------------------------------------------------------
# 路径与命令
# ---------------------------------------------------------------------------

def package_root() -> Path:
    """agent_bridge/ 目录本身 —— MCP 进程的 cwd 必须是它,才能 import vmagent。"""
    return Path(__file__).resolve().parents[1]


def python_exe() -> str:
    """返回可用的 Python 解释器绝对路径。"""
    return sys.executable


def entry_command() -> Dict[str, Any]:
    """MCP server 启动命令(绝对路径,避免客户端 PATH 差异)。"""
    return {
        "command": python_exe(),
        "args": ["-m", "vmagent", "serve-mcp", "--ndjson"],
        "cwd": str(package_root()),
        "env": {"PYTHONPATH": str(package_root())},
    }


def _override_root() -> Optional[Path]:
    """VMAGENT_CONFIG_ROOT 可把所有客户端配置重定向到临时目录(测试用)。"""
    value = os.environ.get("VMAGENT_CONFIG_ROOT")
    return Path(value) if value else None


def claude_config_path() -> Optional[Path]:
    override = _override_root()
    if override:
        return override / "Claude" / "claude_desktop_config.json"
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        return Path(appdata) / "Claude" / "claude_desktop_config.json" if appdata else None
    return Path.home() / ".config/Claude/claude_desktop_config.json"


def cursor_config_path() -> Optional[Path]:
    override = _override_root()
    if override:
        return override / "cursor" / "mcp.json"
    return Path.home() / ".cursor" / "mcp.json"


def codex_config_path() -> Optional[Path]:
    override = _override_root()
    if override:
        return override / "codex" / "config.toml"
    return Path.home() / ".codex" / "config.toml"


# ---------------------------------------------------------------------------
# 环境检查
# ---------------------------------------------------------------------------

def run_checks() -> List[Tuple[str, bool, str]]:
    checks: List[Tuple[str, bool, str]] = []

    py_ok = sys.version_info >= MIN_PYTHON
    checks.append(("Python 版本", py_ok,
                   "%d.%d.%d (需要 >= %d.%d)"
                   % (sys.version_info[0], sys.version_info[1],
                      sys.version_info[2], MIN_PYTHON[0], MIN_PYTHON[1])))

    try:
        sys.path.insert(0, str(package_root()))
        from vmagent.tools import REGISTRY
        checks.append(("工具注册", len(REGISTRY) > 0, "%d 个" % len(REGISTRY)))
    except Exception as exc:
        checks.append(("工具注册", False, str(exc)))

    try:
        from vmagent.core import VMError, discover
        try:
            backend = discover(force=True)
            checks.append(("VideoMatrix 后端", True,
                           "%s (v%s)" % (backend.base,
                                         backend.health.get("version"))))
            checks.append(("结构混剪能力", backend.structured,
                           "可用" if backend.structured else "仅开源 /api"))
        except VMError:
            checks.append(("VideoMatrix 后端", False,
                           "未连接 —— 请先打开 VideoMatrix 桌面程序"))
    except Exception as exc:
        checks.append(("后端探测", False, str(exc)))

    return checks


def print_checks(checks: List[Tuple[str, bool, str]]) -> None:
    width = max(len(c[0]) for c in checks)
    for label, ok, detail in checks:
        mark = "OK  " if ok else "注意"
        print("  [%s] %-*s  %s" % (mark, width, label, detail))


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

def _backup(path: Path) -> Optional[Path]:
    if not path.is_file():
        return None
    backup = Path(str(path) + BACKUP_SUFFIX)
    shutil.copy2(path, backup)
    return backup


def install_json_client(path: Path, label: str) -> int:
    entry = entry_command()
    data: Dict[str, Any] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError as exc:
            print("  ✗ 配置文件不是合法 JSON,未修改: %s" % path)
            print("    %s" % exc)
            return 2
    servers = data.setdefault("mcpServers", {})
    servers[SERVER_KEY] = entry
    if DRY_RUN:
        print("  [演练] 将注册到 %s" % label)
        print("        配置路径: %s" % path)
        print("        写入内容:")
        for line in json.dumps(data, ensure_ascii=False, indent=2).splitlines():
            print("          " + line)
        return 0
    backup = _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("  ✓ 已注册到 %s" % label)
    print("    配置: %s" % path)
    if backup:
        print("    原文件备份: %s" % backup)
    print("    → 重启 %s,工具列表会出现 %d 个 vm_* 工具" % (label, _tool_count()))
    return 0


def install_codex(path: Path) -> int:
    entry = entry_command()
    if path.is_file():
        text = path.read_text(encoding="utf-8", errors="replace")
        if "[mcp_servers.%s]" % SERVER_KEY in text:
            print("  ✓ Codex 已注册过,跳过(如需更新请先删掉 [mcp_servers.%s] 段)" % SERVER_KEY)
            return 0
        backup = _backup(path)
    else:
        backup = None

    block = (
        "\n[mcp_servers.%s]\n"
        'command = "%s"\n'
        'args = ["-m", "vmagent", "serve-mcp", "--ndjson"]\n'
        'cwd = "%s"\n'
        "\n[mcp_servers.%s.env]\n"
        'PYTHONPATH = "%s"\n'
        % (SERVER_KEY, entry["command"].replace("\\", "\\\\"),
           entry["cwd"].replace("\\", "\\\\"),
           SERVER_KEY, entry["cwd"].replace("\\", "\\\\"))
    )
    if DRY_RUN:
        print("  [演练] 将追加到 %s:" % path)
        for line in block.strip().splitlines():
            print("          " + line)
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(block)
    print("  ✓ 已注册到 Codex CLI")
    print("    配置: %s" % path)
    if backup:
        print("    原文件备份: %s" % backup)
    return 0


def _tool_count() -> int:
    try:
        sys.path.insert(0, str(package_root()))
        from vmagent.tools import REGISTRY
        return len(REGISTRY)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# 输出片段
# ---------------------------------------------------------------------------

def print_snippets() -> int:
    entry = entry_command()
    print("\n=== Claude Desktop / Cursor(JSON 配置)===")
    print(json.dumps({"mcpServers": {SERVER_KEY: entry}}, ensure_ascii=False, indent=2))

    print("\n=== Codex CLI(config.toml)===")
    print("[mcp_servers.%s]" % SERVER_KEY)
    print('command = "%s"' % entry["command"])
    print('args = ["-m", "vmagent", "serve-mcp", "--ndjson"]')
    print('cwd = "%s"' % entry["cwd"])
    print("\n[mcp_servers.%s.env]" % SERVER_KEY)
    print('PYTHONPATH = "%s"' % entry["cwd"])

    print("\n=== HTTP 客户端(不需要 MCP)===")
    print("  启动: python -m vmagent serve-http --port 8770")
    print("  工具清单: GET  http://127.0.0.1:8770/tools")
    print("  调用工具: POST http://127.0.0.1:8770/call")
    print('            {"name":"vm_discover","arguments":{}}')
    print("  OpenAI 格式: GET http://127.0.0.1:8770/openai.json")

    print("\n=== 命令行 ===")
    print("  python -m vmagent doctor")
    print("  python -m vmagent run vm_project_list")
    return 0


# ---------------------------------------------------------------------------
# 交互向导
# ---------------------------------------------------------------------------

def wizard() -> int:
    print("=" * 64)
    print("  vmagent 安装向导 — 让 Agent 直接控制 VideoMatrix")
    print("=" * 64)

    print("\n[1/3] 检查环境\n")
    checks = run_checks()
    print_checks(checks)
    py_ok = checks[0][1]
    tools_ok = checks[1][1] if len(checks) > 1 else False
    if not py_ok or not tools_ok:
        print("\n环境不满足,先解决上面的问题再安装。")
        return 2

    backend_ok = any(c[0] == "VideoMatrix 后端" and c[1] for c in checks)
    if not backend_ok:
        print("\n  ⚠ VideoMatrix 没在运行。这不影响安装,但装完要打开它才能用。")

    print("\n[2/3] 检测 Agent 客户端\n")
    targets = []
    claude = claude_config_path()
    cursor = cursor_config_path()
    codex = codex_config_path()

    if claude:
        targets.append(("claude", "Claude Desktop", claude))
    if cursor:
        targets.append(("cursor", "Cursor", cursor))
    if codex:
        targets.append(("codex", "Codex CLI", codex))

    if not targets:
        print("  没找到已知客户端,用 print 手动配置。")
        print_snippets()
        return 0

    for index, (key, label, path) in enumerate(targets, 1):
        exists = path.is_file()
        state = "已存在" if exists else "未安装(将创建)"
        print("  %d. %-16s %s" % (index, label, state))

    print("\n[3/3] 选择要注册的客户端")
    print("  输入编号(多个用空格分隔),或直接回车注册全部,输入 n 跳过:")
    try:
        answer = input("  > ").strip()
    except EOFError:
        answer = ""

    if answer.lower() in ("n", "no"):
        print("\n已跳过注册。可用 print 命令手动配置。")
        return 0

    if not answer:
        chosen = targets
    else:
        chosen = []
        for token in answer.replace(",", " ").split():
            if token.isdigit() and 1 <= int(token) <= len(targets):
                chosen.append(targets[int(token) - 1])

    if not chosen:
        print("  没有有效选择,跳过。")
        return 0

    print()
    for key, label, path in chosen:
        if key == "codex":
            install_codex(path)
        else:
            install_json_client(path, label)

    print("\n" + "=" * 64)
    print("  安装完成")
    print("=" * 64)
    print("\n下一步:")
    print("  1. 打开 VideoMatrix 桌面程序")
    print("  2. 重启你的 Agent 客户端(Claude Desktop / Cursor / Codex)")
    print("  3. 在对话里让它调用 vm_discover 验证连接")
    print("\n自检: python -m vmagent doctor")
    print("演示: python -m vmagent run vm_project_list")
    return 0


# ---------------------------------------------------------------------------

def cmd_doctor() -> int:
    print("环境与注册状态:\n")
    checks = run_checks()
    print_checks(checks)

    print("\n客户端注册:")
    for label, path in (("Claude Desktop", claude_config_path()),
                        ("Cursor", cursor_config_path())):
        if not path:
            print("  [--] %-16s 当前平台无固定路径" % label)
            continue
        registered = False
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                registered = SERVER_KEY in (data.get("mcpServers") or {})
            except Exception:
                pass
        state = "已注册" if registered else ("文件存在但未注册" if path.is_file() else "未安装")
        print("  [%s] %-16s %s" % ("OK" if registered else "--", label, state))

    codex = codex_config_path()
    if codex and codex.is_file():
        text = codex.read_text(encoding="utf-8", errors="replace")
        has = ("[mcp_servers.%s]" % SERVER_KEY) in text
        print("  [%s] %-16s %s" % ("OK" if has else "--", "Codex CLI",
                                   "已注册" if has else "文件存在但未注册"))
    else:
        print("  [--] %-16s 未安装" % "Codex CLI")

    core_ok = all(ok for _, ok, _ in checks[:2])
    return 0 if core_ok else 1


def main(argv=None) -> int:
    global DRY_RUN
    args = list(sys.argv[1:] if argv is None else argv)
    if "--dry-run" in args:
        DRY_RUN = True
        args = [a for a in args if a != "--dry-run"]
    target = args[0] if args else ""

    if not target:
        return wizard()
    if target == "check":
        print_checks(run_checks())
        return 0
    if target == "doctor":
        return cmd_doctor()
    if target == "print":
        return print_snippets()
    if target == "claude":
        path = claude_config_path()
        if not path:
            print("找不到 Claude Desktop 路径,请用 print 手动配置。")
            return 2
        return install_json_client(path, "Claude Desktop")
    if target == "cursor":
        path = cursor_config_path()
        if not path:
            print("找不到 Cursor 路径,请用 print 手动配置。")
            return 2
        return install_json_client(path, "Cursor")
    if target == "codex":
        path = codex_config_path()
        if not path:
            print("找不到 Codex 路径,请用 print 手动配置。")
            return 2
        return install_codex(path)

    print("用法: python -m vmagent.install [check|doctor|print|claude|cursor|codex]")
    print("      不带参数运行交互式向导")
    return 2


if __name__ == "__main__":
    sys.exit(main())
