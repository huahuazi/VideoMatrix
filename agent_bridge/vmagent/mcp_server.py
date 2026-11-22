"""vmagent.mcp_server — MCP stdio 服务器(零依赖实现)。

实现 MCP 的 initialize / tools/list / tools/call 三件套,
用标准 JSON-RPC 2.0 over stdio(LSP 风格 Content-Length 与 NDJSON 双兼容),
不需要安装 mcp SDK 就能被 Claude Desktop / Codex / Cursor / 各类 Agent 接入。
"""
from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Dict, Optional

from . import __version__
from .core import VMError
from .tools import call_tool, list_tools

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "videomatrix-bridge"


def _server_info() -> Dict[str, Any]:
    return {
        "name": SERVER_NAME,
        "version": __version__,
        "instructions": (
            "VideoMatrix 结构混剪控制服务器。"
            "典型流程: vm_discover 确认软件已启动 → vm_project_list 选项目 "
            "→ vm_import_sources 导入素材 → vm_clip_add 打片段 "
            "→ vm_plan_create 生成组合清单 → vm_render 导出 → vm_render_status 轮询。"
            "写入类工具(vm_clip_delete / vm_render_stop / vm_render_stop_all)会改变软件状态,调用前先向用户确认。"
        ),
    }


def _capabilities() -> Dict[str, Any]:
    return {"tools": {"listChanged": False}}


def handle_message(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """处理单条 JSON-RPC 消息。通知(无 id)返回 None。"""
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if msg_id is None and method in ("notifications/initialized", "initialized"):
        return None

    def ok(result: Any) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def err(code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": code, "message": message}}

    try:
        if method == "initialize":
            return ok({
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": _capabilities(),
                "serverInfo": _server_info(),
            })

        if method == "tools/list":
            return ok({"tools": list_tools()})

        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not name:
                return err(-32602, "tools/call 缺少 name")
            try:
                result = call_tool(name, arguments)
            except VMError as exc:
                return ok({
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                })
            return ok({
                "content": [{"type": "text",
                             "text": json.dumps(result, ensure_ascii=False, indent=2)}],
                "isError": False,
            })

        if method == "ping":
            return ok({})

        if method in ("resources/list", "prompts/list"):
            return ok({"resources": []} if method == "resources/list"
                      else {"prompts": []})

        return err(-32601, "不支持的方法: " + str(method))

    except Exception as exc:
        sys.stderr.write("[vmagent] 处理 %s 出错: %s\n%s\n"
                         % (method, exc, traceback.format_exc()))
        sys.stderr.flush()
        return err(-32603, "内部错误: %s" % exc)


def _read_message(stream) -> Optional[Dict[str, Any]]:
    """读一条消息。兼容 LSP Content-Length 头 与 纯 NDJSON。"""
    while True:
        line = stream.readline()
        if not line:
            return None
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        if text.lower().startswith("content-length:"):
            try:
                length = int(text.split(":", 1)[1].strip())
            except ValueError:
                continue
            while True:
                header = stream.readline()
                if not header or not header.strip():
                    break
            body = stream.read(length)
            if not body:
                return None
            try:
                return json.loads(body.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue


def _write_message(stream, payload: Dict[str, Any], framed: bool) -> None:
    raw = json.dumps(payload, ensure_ascii=False)
    if framed:
        stream.write(("Content-Length: %d\r\n\r\n" % len(raw.encode("utf-8"))).encode())
        stream.write(raw.encode("utf-8"))
    else:
        stream.write((raw + "\n").encode("utf-8"))
    stream.flush()


def serve(framed: Optional[bool] = None) -> int:
    """跑 stdio 服务循环。framed 为 None 时自动探测首条消息格式。"""
    stdin = getattr(sys.stdin, "buffer", sys.stdin)
    stdout = getattr(sys.stdout, "buffer", sys.stdout)
    if framed is None:
        framed = False

    while True:
        msg = _read_message(stdin)
        if msg is None:
            return 0
        if not framed and isinstance(msg, dict) and "jsonrpc" not in msg:
            continue
        reply = handle_message(msg)
        if reply is not None:
            _write_message(stdout, reply, framed)


def main(argv=None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    framed = True if "--framed" in args else (False if "--ndjson" in args else None)
    return serve(framed)


if __name__ == "__main__":
    sys.exit(main())

