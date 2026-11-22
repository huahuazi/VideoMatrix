"""vmagent.http_server — HTTP/JSON 服务(零依赖)。

把同一份工具注册表暴露成 REST:
  GET  /                   服务与连接状态
  GET  /tools              工具清单(OpenAI function-calling 可直接用)
  GET  /openapi.json       本服务自身的 OpenAPI(便于通用客户端接入)
  POST /call               调用工具 {"name": "...", "arguments": {...}}
  POST /tools/<name>       同上,工具名走路径
  GET  /health             存活检查

只监听 127.0.0.1,不接受外部连接;可选 token 鉴权。
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlparse

from . import __version__
from .core import VMError, discover
from .tools import call_tool, list_tools

TOKEN = os.environ.get("VMAGENT_TOKEN", "")


def _openai_schema() -> Dict[str, Any]:
    """转成 OpenAI / 通用 function-calling 格式。"""
    return {"tools": [{
        "type": "function",
        "function": {
            "name": item["name"],
            "description": item["description"],
            "parameters": item["inputSchema"],
        },
    } for item in list_tools()]}


def _openapi_doc() -> Dict[str, Any]:
    return {
        "openapi": "3.0.0",
        "info": {"title": "VideoMatrix Agent Bridge",
                 "version": __version__,
                 "description": "把 VideoMatrix 桌面程序的能力暴露给 Agent"},
        "paths": {
            "/health": {"get": {"summary": "存活检查", "responses": {"200": {"description": "ok"}}}},
            "/tools": {"get": {"summary": "工具清单", "responses": {"200": {"description": "ok"}}}},
            "/call": {"post": {"summary": "调用工具",
                               "requestBody": {"required": True, "content": {
                                   "application/json": {"schema": {
                                       "type": "object",
                                       "properties": {
                                           "name": {"type": "string"},
                                           "arguments": {"type": "object"},
                                       },
                                       "required": ["name"]}}}},
                               "responses": {"200": {"description": "ok"}}}},
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "vmagent/" + __version__
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默:agent 环境不需要访问日志
        pass

    def _send(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _authorized(self) -> bool:
        if not TOKEN:
            return True
        header = self.headers.get("Authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        return token == TOKEN or self.headers.get("X-VMAGENT-TOKEN") == TOKEN

    def _body(self) -> Optional[Dict[str, Any]]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0:
            return None
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _dispatch(self, name: str, arguments: Dict[str, Any]) -> None:
        # 直接返回工具原始结果,与 CLI / MCP 完全同构(都带 ok 字段)
        try:
            self._send(200, call_tool(name, arguments))
        except VMError as exc:
            self._send(200, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._send(500, {"ok": False, "error": "内部错误: %s" % exc})

    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        if not self._authorized():
            return self._send(401, {"ok": False, "error": "unauthorized"})

        if path == "/health":
            return self._send(200, {"ok": True, "service": "vmagent", "version": __version__})
        if path == "/tools":
            return self._send(200, {"ok": True, "tools": list_tools()})
        if path == "/openai.json":
            return self._send(200, _openai_schema())
        if path == "/openapi.json":
            return self._send(200, _openapi_doc())
        if path == "/":
            try:
                info = discover(force=True).describe()
            except VMError as exc:
                info = {"ok": False, "error": str(exc)}
            info["service"] = "vmagent"
            info["version"] = __version__
            info["tool_count"] = len(list_tools())
            return self._send(200, info)
        return self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        if not self._authorized():
            return self._send(401, {"ok": False, "error": "unauthorized"})

        payload = self._body() or {}
        if path == "/call":
            name = payload.get("name") or ""
            arguments = payload.get("arguments") or payload.get("args") or {}
            if not name:
                return self._send(400, {"ok": False, "error": "缺少 name"})
            return self._dispatch(name, arguments)

        if path.startswith("/tools/"):
            name = unquote(path[len("/tools/"):])
            arguments = payload.get("arguments") or payload
            if "arguments" in arguments:
                arguments = arguments["arguments"]
            return self._dispatch(name, arguments)

        return self._send(404, {"ok": False, "error": "not found"})


def serve(host: str = "127.0.0.1", port: int = 8770) -> int:
    server = ThreadingHTTPServer((host, port), Handler)
    sys_out = "[vmagent] HTTP 服务已启动 http://%s:%d  (Ctrl+C 停止)\n" % (host, port)
    import sys as _sys
    _sys.stderr.write(sys_out)
    _sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _sys.stderr.write("\n[vmagent] 已停止\n")
    finally:
        server.server_close()
    return 0

