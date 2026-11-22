"""vmagent.cli — 命令行入口。

子命令由工具注册表自动生成,所以新增工具后 CLI 立即就能用,不用改这里。

  vmagent serve-mcp            启动 MCP stdio 服务器(给 Claude Desktop / Codex / Cursor)
  vmagent serve-http [--port]  启动 HTTP 服务
  vmagent tools [--group]      列出工具(含 JSON Schema)
  vmagent schema [--openai]    导出 OpenAI function-calling 格式
  vmagent call <tool> '<json>' 调用任意工具
  vmagent run <tool> --k v ... 用命令行参数调用工具
  vmagent discover             连接探测
  vmagent doctor               环境自检
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, Optional

from . import __version__
from .core import VMError, discover
from .tools import REGISTRY, call_tool, list_tools


def _emit(value: Any, compact: bool) -> None:
    if compact:
        print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))


def _coerce(raw: str, prop: Dict[str, Any]) -> Any:
    """把命令行字符串按 schema 类型转换。"""
    typ = prop.get("type")
    try:
        if typ == "integer":
            return int(raw)
        if typ == "number":
            return float(raw)
        if typ == "boolean":
            return raw.strip().lower() in ("1", "true", "yes", "y", "on")
        if typ == "array":
            if raw.strip().startswith("["):
                return json.loads(raw)
            return [part for part in raw.split(",") if part]
        if typ == "object":
            return json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return raw
    return raw


def cmd_tools(args: argparse.Namespace) -> int:
    _emit({"ok": True, "count": len(list_tools(args.group)),
           "tools": list_tools(args.group)}, args.compact)
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    if args.openai:
        from .http_server import _openai_schema
        _emit(_openai_schema(), args.compact)
        return 0
    _emit({"mcp": {"tools": list_tools()},
           "openai": _openai_schema()}, args.compact)
    return 0


def cmd_call(args: argparse.Namespace) -> int:
    try:
        arguments = json.loads(args.json) if args.json else {}
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": "JSON 解析失败: %s" % exc},
                         ensure_ascii=False))
        return 2
    return _run(lambda: call_tool(args.tool, arguments), args.compact)


def cmd_run(args: argparse.Namespace) -> int:
    entry = REGISTRY.get(args.tool)
    if entry is None:
        print(json.dumps({"ok": False,
                          "error": "未知工具: %s" % args.tool,
                          "available": sorted(REGISTRY)}, ensure_ascii=False))
        return 2
    props = entry["schema"]["properties"]
    arguments: Dict[str, Any] = {}
    unknown = []
    for pair in args.pairs:
        if "=" not in pair:
            print(json.dumps({"ok": False,
                              "error": "参数需写成 key=value,收到: %s" % pair},
                             ensure_ascii=False))
            return 2
        key, value = pair.split("=", 1)
        if key not in props:
            unknown.append(key)
            continue
        arguments[key] = _coerce(value, props[key])
    if unknown:
        print(json.dumps({"ok": False,
                          "error": "工具 %s 不接受参数: %s" % (args.tool, ", ".join(unknown)),
                          "accepted": sorted(props)}, ensure_ascii=False))
        return 2
    return _run(lambda: call_tool(args.tool, arguments), args.compact)


def _run(fn, compact: bool) -> int:
    try:
        result = fn()
    except VMError as exc:
        _emit({"ok": False, "error": str(exc)}, True)
        return 2
    except Exception as exc:
        _emit({"ok": False, "error": "内部错误: %s" % exc}, True)
        return 3
    _emit(result, compact)
    return 0 if result.get("ok", True) else 1


def cmd_discover(args: argparse.Namespace) -> int:
    try:
        backend = discover(force=True)
    except VMError as exc:
        _emit({"ok": False, "error": str(exc)}, args.compact)
        return 2
    info = backend.describe()
    info["tools"] = len(list_tools())
    _emit(info, args.compact)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = []
    checks.append({"check": "python", "ok": sys.version_info >= (3, 9),
                   "detail": sys.version.split()[0]})
    checks.append({"check": "service", "ok": True, "detail": "vmagent " + __version__})
    try:
        backend = discover(force=True)
        checks.append({"check": "backend", "ok": True,
                       "detail": "%s (%s)" % (backend.base, backend.health.get("version"))})
        checks.append({"check": "structured", "ok": backend.structured,
                       "detail": "支持结构混剪" if backend.structured else "仅开源 /api 混剪"})
    except VMError as exc:
        checks.append({"check": "backend", "ok": False, "detail": str(exc)})
    checks.append({"check": "tools", "ok": len(REGISTRY) > 0,
                   "detail": "%d 个工具" % len(REGISTRY)})
    ok = all(item["ok"] for item in checks)
    _emit({"ok": ok, "checks": checks}, args.compact)
    return 0 if ok else 1


def cmd_serve_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import serve
    try:
        return serve(True if args.framed else (False if args.ndjson else None))
    except KeyboardInterrupt:
        return 0


def cmd_serve_http(args: argparse.Namespace) -> int:
    from .http_server import serve
    return serve(args.host, args.port)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vmagent",
        description="VideoMatrix Agent 接入层: MCP / HTTP / CLI 三协议同源")
    p.add_argument("--compact", action="store_true", help="单行 JSON 输出")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("discover", help="连接探测")
    sub.add_parser("doctor", help="环境自检")

    t = sub.add_parser("tools", help="列出工具")
    t.add_argument("--group", default="")

    sc = sub.add_parser("schema", help="导出工具 schema")
    sc.add_argument("--openai", action="store_true", help="只输出 OpenAI function-calling 格式")

    c = sub.add_parser("call", help="用 JSON 调用工具")
    c.add_argument("tool")
    c.add_argument("json", nargs="?", default="{}")

    r = sub.add_parser("run", help="用 key=value 调用工具")
    r.add_argument("tool")
    r.add_argument("pairs", nargs="*")

    m = sub.add_parser("serve-mcp", help="启动 MCP stdio 服务器")
    m.add_argument("--framed", action="store_true", help="强制 LSP Content-Length 分帧")
    m.add_argument("--ndjson", action="store_true", help="强制 NDJSON 分帧")

    h = sub.add_parser("serve-http", help="启动 HTTP 服务")
    h.add_argument("--host", default="127.0.0.1")
    h.add_argument("--port", type=int, default=8770)
    return p


def main(argv=None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    compact = "--compact" in raw
    raw = [item for item in raw if item != "--compact"]
    args = build_parser().parse_args(raw)
    args.compact = compact or bool(getattr(args, "compact", False))
    if args.command == "discover":
        return cmd_discover(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "tools":
        return cmd_tools(args)
    if args.command == "schema":
        return cmd_schema(args)
    if args.command == "call":
        return cmd_call(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "serve-mcp":
        return cmd_serve_mcp(args)
    if args.command == "serve-http":
        return cmd_serve_http(args)
    return 2

