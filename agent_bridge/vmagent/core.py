"""vmagent.core — VideoMatrix Agent 接入层核心

单一事实源: 所有工具在 TOOLS 注册表里定义一次,
自动导出为 MCP tools / HTTP 路由 / CLI 子命令三种形态。

设计约束:
  * 仅用 Python 标准库(3.9+),目标机器无需 pip install 任何东西
  * 只走 VideoMatrix 自带的本机 HTTP API,不注入进程、不改软件文件
  * 所有工具返回 dict,统一 {"ok": bool, ...} 结构
"""
from __future__ import annotations

import glob
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

VERSION = "1.0.0"
DEFAULT_TIMEOUT = float(os.environ.get("VMAGENT_TIMEOUT", "60"))


# ===========================================================================
# 1. 端口发现
# ===========================================================================

RUNTIME_GLOBS = [
    # 结构混剪本地定制版(2.8.4.x):userData 被重定向到 VideoMatrix-Structured-*
    "%APPDATA%/VideoMatrix-Structured*/desktop/desktop-runtime.json",
    "%APPDATA%/VideoMatrix-Structured*/desktop-runtime.json",
    # 开源版 / 2.8.4 宿主
    "%APPDATA%/VideoMatrix/desktop-runtime.json",
    "%APPDATA%/VideoMatrix*/desktop-runtime.json",
    # macOS / Linux
    "~/Library/Application Support/VideoMatrix*/desktop-runtime.json",
    "~/Library/Application Support/VideoMatrix*/desktop/desktop-runtime.json",
    "~/.config/VideoMatrix*/desktop-runtime.json",
]

LOCALAPPDATA_GLOBS = [
    "%LOCALAPPDATA%/Programs/VideoMatrix*/resources/backend/desktop-runtime.json",
]

# 仓库内直接跑后端(开发态)时,运行时文件落在 backend/ 旁边
REPO_RUNTIME_GLOBS = [
    "desktop-runtime.json",
    "backend/desktop-runtime.json",
    "../desktop-runtime.json",
]


class VMError(RuntimeError):
    """面向 agent 的可读错误。"""


def _expand(pattern: str) -> List[str]:
    resolved = pattern
    for var in ("APPDATA", "LOCALAPPDATA"):
        token = "%" + var + "%"
        if token in resolved:
            value = os.environ.get(var)
            if not value:
                return []
            resolved = resolved.replace(token, value)
    resolved = os.path.expanduser(resolved)
    return sorted(glob.glob(resolved))


def project_root() -> Path:
    """仓库根目录(agent_bridge/ 的上一级)。

    用于在开发态发现 run_backend.py 直接生成的 desktop-runtime.json。
    """
    return Path(__file__).resolve().parents[2]


def candidate_runtime_files() -> List[Path]:
    found: List[Path] = []
    for pattern in RUNTIME_GLOBS + LOCALAPPDATA_GLOBS:
        for hit in _expand(pattern):
            path = Path(hit)
            if path.is_file() and path not in found:
                found.append(path)

    # 开发态:仓库根目录下可能由 run_backend.py 直接生成
    root = project_root()
    for relative in REPO_RUNTIME_GLOBS:
        candidate = (root / relative).resolve()
        if candidate.is_file() and candidate not in found:
            found.append(candidate)
    return found


def _http_json(url: str, timeout: float, method: str = "GET",
               payload: Any = None, headers: Optional[Dict[str, str]] = None) -> Any:
    body = None
    hdrs = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json; charset=utf-8"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            if not raw:
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        detail: Any = raw
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                detail = parsed.get("detail", parsed)
        except json.JSONDecodeError:
            pass
        raise VMError("%s %s -> HTTP %s: %s" % (method.upper(), url, exc.code, _flatten(detail)))
    except urllib.error.URLError as exc:
        raise VMError("%s %s 连接失败: %s" % (method.upper(), url, exc.reason))
    except socket.timeout:
        raise VMError("%s %s 超时 (%.0fs)" % (method.upper(), url, timeout))


def _flatten(detail: Any) -> str:
    """把 FastAPI 的 422 校验错误压成一行可读文本。"""
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        parts = []
        for item in detail[:6]:
            if isinstance(item, dict):
                loc = ".".join(str(x) for x in item.get("loc", []) if x != "body")
                msg = item.get("msg", "参数错误")
                parts.append(loc + ": " + msg if loc else str(msg))
            else:
                parts.append(str(item))
        return "; ".join(parts)
    return json.dumps(detail, ensure_ascii=False)


class Backend:
    """一个已发现并验证过的 VideoMatrix 后端连接。"""

    def __init__(self, base: str, health: Dict[str, Any], source: str, structured: bool):
        self.base = base.rstrip("/")
        self.health = health
        self.source = source
        self.structured = structured

    @property
    def api(self) -> str:
        return self.base + "/api"

    def request(self, method: str, path: str, payload: Any = None,
                timeout: float = DEFAULT_TIMEOUT) -> Any:
        url = self.api + "/" + path.lstrip("/")
        return _http_json(url, timeout, method, payload)

    def describe(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "base": self.base,
            "api": self.api,
            "flavor": "structured" if self.structured else "oss",
            "version": self.health.get("version"),
            "instance": self.health.get("instance"),
            "source": self.source,
        }


_CACHE: Dict[str, Any] = {"backend": None, "at": 0.0}
CACHE_TTL = float(os.environ.get("VMAGENT_CACHE_TTL", "5"))


def _probe_structured(base: str) -> bool:
    try:
        _http_json(base + "/api/structured/projects", 1.5)
        return True
    except urllib.error.HTTPError as exc:
        return exc.code in (405, 422)
    except Exception:
        return False


def discover(force: bool = False) -> Backend:
    """按 4 级回退定位后端。结果短时缓存,避免每个工具调用都扫盘。"""
    now = time.time()
    if not force and _CACHE["backend"] is not None and now - _CACHE["at"] < CACHE_TTL:
        return _CACHE["backend"]

    explicit = os.environ.get("VMAGENT_BASE") or os.environ.get("VIDEOMATRIX_API_URL")
    if explicit:
        base = explicit.rstrip("/")
        if base.endswith("/api"):
            base = base[:-4]
        try:
            health = _http_json(base + "/api/health", 3.0)
        except Exception as exc:
            raise VMError("环境变量指定的后端 %s 不可用: %s" % (base, exc))
        backend = Backend(base, health, "env VMAGENT_BASE/VIDEOMATRIX_API_URL", _probe_structured(base))
        _CACHE.update(backend=backend, at=now)
        return backend

    errors: List[str] = []
    for runtime_file in candidate_runtime_files():
        try:
            data = json.loads(runtime_file.read_text(encoding="utf-8"))
            port = data.get("port")
            if not isinstance(port, int) or not 1 <= port <= 65535:
                errors.append("%s: port 字段无效" % runtime_file)
                continue
        except Exception as exc:
            errors.append("%s: %s" % (runtime_file, exc))
            continue
        base = "http://127.0.0.1:%d" % port
        try:
            health = _http_json(base + "/api/health", 2.0)
        except Exception as exc:
            errors.append("%s: 记录的端口 %d 无响应 (%s)" % (runtime_file, port, exc))
            continue
        expected = data.get("instance")
        if expected and health.get("instance") != expected:
            errors.append("%s: instance 不匹配(软件可能已重启)" % runtime_file)
            continue
        backend = Backend(base, health, str(runtime_file), _probe_structured(base))
        _CACHE.update(backend=backend, at=now)
        return backend

    try:
        health = _http_json("http://127.0.0.1:8765/api/health", 1.5)
        backend = Backend("http://127.0.0.1:8765", health, "默认端口 8765", _probe_structured("http://127.0.0.1:8765"))
        _CACHE.update(backend=backend, at=now)
        return backend
    except Exception:
        pass

    raise VMError(
        "找不到运行中的 VideoMatrix 后端。请先打开 VideoMatrix 桌面程序。\n"
        + ("已尝试:\n  " + "\n  ".join(errors) if errors else "未发现任何 desktop-runtime.json。")
    )


def _b() -> Backend:
    return discover()


def _require_structured(b: Backend) -> None:
    if not b.structured:
        raise VMError("当前连接的是开源版后端(无 /api/structured)。结构混剪工具需要 2.8.4.x 定制版。")


def _proj(project_id: str) -> str:
    return "structured/projects/" + urllib.parse.quote(project_id, safe="")


def unwrap(payload: Any, *keys: str) -> Any:
    """从后端响应里取出真正的实体。

    2.8.4.1 各端点包装不一致:有的返回 {"project": {...}},有的直接返回 {...}。
    这里按优先级逐层剥,取第一个命中的 key;都没命中就原样返回。
    """
    current = payload
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            break
    return current


def passthrough(data: Any) -> Dict[str, Any]:
    """把后端响应透传为工具结果:dict 直接合并并补 ok 字段。

    这样三种协议(CLI/HTTP/MCP)返回结构与后端契约一一对应,
    Agent 不必理解额外的 {result: ...} 包装层。
    """
    if isinstance(data, dict):
        out = dict(data)
        out.setdefault("ok", True)
        return out
    return {"ok": True, "result": data}
