#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_conformance — vmagent 一致性自测。

起一个模拟后端,逐条验证:
  A. 工具注册表完整性
  B. CLI 层(discover/doctor/tools/schema/call/run)
  C. HTTP 层(/health /tools /call /tools/<name> /openai.json /openapi.json)
  D. MCP 层(initialize / tools/list / tools/call,NDJSON 与 Content-Length 双分帧)
  E. 端到端业务流(建项目→素材→片段→清单→渲染→轮询→停止)
  F. 传统混剪流
  G. 错误路径

用法: python3 tests/run_conformance.py
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

PORT_MOCK = 8811
PORT_HTTP = 8812
RESULTS: List[Tuple[str, bool, str]] = []


def check(label, ok, detail=""):
    RESULTS.append((label, ok, detail))
    print("  [%s] %s" % ("PASS" if ok else "FAIL", label))
    if detail and not ok:
        print("         " + detail.replace("\n", "\n         ")[:700])
    return ok


def wait_port(port, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def http_json(port, method, path, payload=None):
    import urllib.request
    url = "http://127.0.0.1:%d%s" % (port, path)
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def run_cli(args, env_extra=None, timeout=90.0):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run([sys.executable, "-m", "vmagent"] + args,
                          cwd=ROOT, env=env, capture_output=True,
                          text=True, timeout=timeout)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def last_json(text):
    """取输出里最后一行可解析的 JSON(容忍前置噪音)。"""
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {}


# ---------------------------------------------------------------------------

def test_registry():
    print("\n=== A. 工具注册表 ===")
    from vmagent.tools import list_tools
    items = list_tools()
    check("工具数量 = 33", len(items) == 33, "实际 %d" % len(items))

    problems = []
    for item in items:
        schema = item.get("inputSchema") or {}
        if schema.get("type") != "object":
            problems.append(item["name"] + ": type")
        props = schema.get("properties")
        if not isinstance(props, dict):
            problems.append(item["name"] + ": properties")
        req = schema.get("required")
        if not isinstance(req, list):
            problems.append(item["name"] + ": required")
        for key in req or []:
            if key not in (props or {}):
                problems.append("%s: required %s 不在 properties" % (item["name"], key))
        if not item.get("description"):
            problems.append(item["name"] + ": 缺 description")
        if not item["name"].startswith("vm_"):
            problems.append(item["name"] + ": 命名未加 vm_ 前缀")
    check("所有 schema 合法", not problems, "; ".join(problems))

    names = [i["name"] for i in items]
    check("工具名唯一", len(names) == len(set(names)))

    destructive = sorted(i["name"] for i in items if i["destructive"])
    check("破坏性工具已标注 4 个", len(destructive) == 4, str(destructive))

    groups = {}
    for item in items:
        groups[item["group"]] = groups.get(item["group"], 0) + 1
    check("分组覆盖 5 类", len(groups) == 5, str(groups))


def test_cli_layers(env):
    print("\n=== B. CLI 层 ===")
    code, out = run_cli(["discover", "--compact"], env)
    ok = code == 0 and '"flavor":"structured"' in out.replace(" ", "")
    check("discover 识别 structured 定制版", ok, out[:300])

    code, out = run_cli(["doctor", "--compact"], env)
    check("doctor 自检通过", code == 0 and '"ok":true' in out.replace(" ", ""), out[:500])

    code, out = run_cli(["tools", "--compact"], env)
    check("tools 列出 33 个", code == 0 and '"count":33' in out.replace(" ", ""), out[:200])

    code, out = run_cli(["schema", "--openai", "--compact"], env)
    ok = code == 0 and '"type":"function"' in out.replace(" ", "")
    check("schema --openai 可用", ok, out[:200])

    code, out = run_cli(["call", "vm_health", "{}", "--compact"], env)
    ok = code == 0 and last_json(out).get("ok") is True
    check("call vm_health", ok, out[:300])

    code, out = run_cli(["run", "vm_project_summary", "project_id=proj-1", "--compact"], env)
    ok = code == 0 and '"clips_total"' in out.replace(" ", "")
    check("run 用 key=value 传参", ok, out[:300])

    code, out = run_cli(["run", "vm_project_summary"], env)
    check("run 缺必填参数被拒", code == 2 and "缺少必填" in out, out[:250])

    code, out = run_cli(["run", "vm_health", "bogus=1"], env)
    check("run 未知参数被拒", code == 2 and "不接受参数" in out, out[:250])

    code, out = run_cli(["call", "no_such_tool", "{}"], env)
    check("call 未知工具被拒", code == 2 and "未知工具" in out, out[:250])

    code, out = run_cli(["run", "vm_render", "plan_id=p", "output_dir=D:/o",
                         "width=1080", "height=1920", "--compact"], env)
    ok = code == 0 and last_json(out).get("ok") is True
    check("run 数字参数按 schema 转型", ok, out[:300])


def test_http_layer(env):
    print("\n=== C. HTTP 层 ===")
    proc = subprocess.Popen([sys.executable, "-m", "vmagent", "serve-http",
                             "--port", str(PORT_HTTP)],
                            cwd=ROOT, env=dict(os.environ, **env),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not check("HTTP 服务启动", wait_port(PORT_HTTP),
                     "端口 %d 未监听" % PORT_HTTP):
            return

        health = http_json(PORT_HTTP, "GET", "/health")
        check("GET /health", health.get("ok") is True, str(health)[:200])

        tools = http_json(PORT_HTTP, "GET", "/tools")
        count = len(tools.get("tools") or [])
        check("GET /tools = 33", count == 33, "实际 %d" % count)

        openai = http_json(PORT_HTTP, "GET", "/openai.json")
        check("GET /openai.json", len(openai.get("tools") or []) == 33)

        doc = http_json(PORT_HTTP, "GET", "/openapi.json")
        check("GET /openapi.json", "paths" in doc and "/call" in doc["paths"])

        root = http_json(PORT_HTTP, "GET", "/")
        check("GET / 返回连接信息", root.get("flavor") == "structured", str(root)[:250])

        called = http_json(PORT_HTTP, "POST", "/call",
                           {"name": "vm_project_summary",
                            "arguments": {"project_id": "proj-1"}})
        check("POST /call 返回工具原始结果",
              called.get("ok") is True and "clips_total" in str(called),
              str(called)[:250])

        bypath = http_json(PORT_HTTP, "POST", "/tools/vm_health", {"arguments": {}})
        check("POST /tools/<name>", bypath.get("ok") is True, str(bypath)[:250])

        bad = http_json(PORT_HTTP, "POST", "/call", {"name": "nope", "arguments": {}})
        check("未知工具返回 ok=false", bad.get("ok") is False and "未知工具" in str(bad),
              str(bad)[:250])

        noargs = http_json(PORT_HTTP, "POST", "/call", {"name": "vm_health"})
        check("缺 arguments 时默认空对象",
              noargs.get("ok") is not False or "缺少" in str(noargs), str(noargs)[:250])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def mcp_session(env, framed):
    proc = subprocess.Popen([sys.executable, "-m", "vmagent", "serve-mcp",
                             "--framed" if framed else "--ndjson"],
                            cwd=ROOT, env=dict(os.environ, **env),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)

    def send(obj):
        raw = json.dumps(obj, ensure_ascii=False)
        if framed:
            frame = ("Content-Length: %d\r\n\r\n" % len(raw.encode())).encode() + raw.encode()
        else:
            frame = (raw + "\n").encode()
        proc.stdin.write(frame)
        proc.stdin.flush()

    def read():
        if framed:
            length = None
            while True:
                line = proc.stdout.readline()
                if not line:
                    return None
                text = line.decode().strip()
                if not text:
                    break
                if text.lower().startswith("content-length:"):
                    length = int(text.split(":", 1)[1].strip())
            if length is None:
                return None
            return json.loads(proc.stdout.read(length).decode())
        line = proc.stdout.readline()
        if not line:
            return None
        return json.loads(line.decode())

    replies = []
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "conformance", "version": "1"}}})
        replies.append(read())
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        replies.append(read())
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "vm_discover", "arguments": {}}})
        replies.append(read())
        send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
              "params": {"name": "vm_health", "arguments": {}}})
        replies.append(read())
        send({"jsonrpc": "2.0", "id": 5, "method": "bogus/method", "params": {}})
        replies.append(read())
        send({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
              "params": {"name": "vm_project_get",
                         "arguments": {"project_id": "不存在"}}})
        replies.append(read())
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return replies


def test_mcp_layer(env):
    print("\n=== D. MCP 层 ===")
    for framed in (False, True):
        label = "Content-Length" if framed else "NDJSON"
        try:
            replies = mcp_session(env, framed)
        except Exception as exc:
            check("MCP %s 会话" % label, False,
                  "%s\n%s" % (exc, traceback.format_exc()))
            continue

        init = replies[0] or {}
        result = init.get("result") or {}
        check("MCP %s initialize" % label,
              result.get("protocolVersion") == "2024-11-05"
              and (result.get("serverInfo") or {}).get("name") == "videomatrix-bridge",
              json.dumps(init, ensure_ascii=False)[:350])
        check("MCP %s 声明 tools 能力" % label,
              "tools" in (result.get("capabilities") or {}))

        listed = ((replies[1] or {}).get("result") or {}).get("tools") or []
        check("MCP %s tools/list = 33" % label, len(listed) == 33,
              "实际 %d" % len(listed))
        check("MCP %s 工具带 inputSchema" % label,
              bool(listed) and all("inputSchema" in t for t in listed))

        disc = (replies[2] or {}).get("result") or {}
        text = ((disc.get("content") or [{}])[0] or {}).get("text", "")
        check("MCP %s tools/call vm_discover" % label,
              '"ok": true' in text and "flavor" in text, text[:350])

        health = (replies[3] or {}).get("result") or {}
        check("MCP %s tools/call vm_health" % label,
              health.get("isError") is False, json.dumps(health, ensure_ascii=False)[:250])

        err = replies[4] or {}
        check("MCP %s 未知方法 -32601" % label,
              (err.get("error") or {}).get("code") == -32601,
              json.dumps(err, ensure_ascii=False)[:250])

        back_err = (replies[5] or {}).get("result") or {}
        check("MCP %s 后端错误转为 isError" % label,
              back_err.get("isError") is True, json.dumps(back_err, ensure_ascii=False)[:300])


def test_business_flow(env):
    print("\n=== E. 端到端业务流 ===")
    code, out = run_cli(["call", "vm_project_create",
                         json.dumps({"name": "一致性测试项目"}, ensure_ascii=False),
                         "--compact"], env)
    if not check("建项目", code == 0 and last_json(out).get("ok") is True, out[:300]):
        return
    project_id = last_json(out)["project"]["id"]

    code, out = run_cli(["call", "vm_product_create",
                         json.dumps({"project_id": project_id, "name": "秋季新品"},
                                    ensure_ascii=False), "--compact"], env)
    check("建产品", code == 0 and last_json(out).get("ok") is True, out[:250])

    code, out = run_cli(["call", "vm_folder_create",
                         json.dumps({"project_id": project_id, "name": "8月爆款"},
                                    ensure_ascii=False), "--compact"], env)
    check("建文件夹", code == 0 and last_json(out).get("ok") is True, out[:250])

    code, out = run_cli(["call", "vm_import_sources",
                         json.dumps({"project_id": project_id,
                                     "directory": "D:/素材/Hook",
                                     "recursive": True}, ensure_ascii=False),
                         "--compact"], env)
    body = last_json(out)
    check("整目录导入 3 个", body.get("imported") == 3, out[:350])

    code, out = run_cli(["run", "vm_source_list", "project_id=" + project_id,
                         "--compact"], env)
    body = last_json(out)
    if not check("列素材", body.get("count") == 3, out[:300]):
        return
    source_id = body["sources"][0]["id"]

    code, out = run_cli(["call", "vm_clip_add",
                         json.dumps({"project_id": project_id, "source_id": source_id,
                                     "category": "A", "start": 0, "end": 3.2,
                                     "note": "爆款开头1", "approved": True},
                                    ensure_ascii=False), "--compact"], env)
    body = last_json(out)
    if not check("打 A 片段", bool(body.get("clip")), out[:300]):
        return
    check("片段自动编号 A1", (body.get("clip") or {}).get("id") == "A1",
          str(body.get("clip"))[:200])

    code, out = run_cli(["call", "vm_clip_add",
                         json.dumps({"project_id": project_id, "source_id": source_id,
                                     "category": "B", "start": 3.2, "end": 6.4,
                                     "approved": True}), "--compact"], env)
    check("打 B 片段", last_json(out).get("ok") is True, out[:250])

    code, out = run_cli(["call", "vm_clip_update",
                         json.dumps({"project_id": project_id, "clip_id": "A1",
                                     "end": 3.5}), "--compact"], env)
    clip = ((last_json(out)).get("clip") or {})
    check("改片段出点", clip.get("end") == 3.5, out[:250])

    code, out = run_cli(["run", "vm_clip_list", "project_id=" + project_id,
                         "category=A", "--compact"], env)
    body = last_json(out)
    check("按类别过滤片段", body.get("count") == 1, out[:250])

    code, out = run_cli(["run", "vm_project_summary", "project_id=" + project_id,
                         "--compact"], env)
    summary = last_json(out)
    ok = summary.get("clips_total") == 2 and summary.get("clips_pending") == 0
    check("项目摘要统计正确", ok, json.dumps(summary, ensure_ascii=False)[:400])

    code, out = run_cli(["run", "vm_category_labels", "project_id=" + project_id,
                         "--compact"], env)
    check("读类别名", "爆款开头" in out, out[:250])

    code, out = run_cli(["call", "vm_category_labels",
                         json.dumps({"project_id": project_id,
                                     "labels": {"A": "钩子开头"}}, ensure_ascii=False),
                         "--compact"], env)
    check("改类别名", "钩子开头" in out, out[:250])

    code, out = run_cli(["call", "vm_formula_save",
                         json.dumps({"project_id": project_id, "name": "主公式",
                                     "formula": "ABCDEF"}, ensure_ascii=False),
                         "--compact"], env)
    check("保存公式", last_json(out).get("ok") is True, out[:250])

    code, out = run_cli(["call", "vm_formula_save",
                         json.dumps({"project_id": project_id, "name": "坏公式",
                                     "formula": "BCDEF"}), "--compact"], env)
    check("不以 A 开头的公式被本地拦截", code == 2 and "必须以 A 开头" in out, out[:250])

    code, out = run_cli(["call", "vm_plan_create",
                         json.dumps({"project_id": project_id, "formula": "ABCDEF",
                                     "count": 5, "product_id": "prod-1"}),
                         "--compact"], env)
    plan = last_json(out)
    if not check("生成组合清单", bool(plan.get("plan_id")), out[:400]):
        return
    plan_id = plan["plan_id"]

    code, out = run_cli(["call", "vm_test_plan_create",
                         json.dumps({"project_id": project_id, "kind": "hook",
                                     "formula": "ABCDEF", "product_id": "prod-1"}),
                         "--compact"], env)
    check("生成开头测试清单", last_json(out).get("ok") is True, out[:300])

    code, out = run_cli(["call", "vm_render",
                         json.dumps({"plan_id": plan_id, "output_dir": "D:/out",
                                     "width": 1080, "height": 1920}), "--compact"], env)
    render = (last_json(out)).get("render") or {}
    if not check("启动导出", bool(render.get("id")), out[:350]):
        return
    render_id = render["id"]

    code, out = run_cli(["run", "vm_render_status", "render_id=" + render_id,
                         "--compact"], env)
    check("查导出进度", "running" in out, out[:250])

    code, out = run_cli(["run", "vm_render_stop", "render_id=" + render_id,
                         "--compact"], env)
    check("停止单导出", last_json(out).get("ok") is True, out[:250])

    code, out = run_cli(["call", "vm_render_stop_all", "{}", "--compact"], env)
    check("停止全部导出", last_json(out).get("ok") is True, out[:250])

    code, out = run_cli(["call", "vm_simple_render",
                         json.dumps({"project_id": project_id, "plan_id": plan_id,
                                     "output_dir": "D:/out2"}), "--compact"], env)
    sr = (last_json(out)).get("simple_render") or {}
    check("简易渲染", bool(sr.get("id")), out[:300])
    if sr.get("id"):
        code, out = run_cli(["run", "vm_simple_render_status",
                             "render_id=" + sr["id"], "--compact"], env)
        check("查简易渲染状态", "queued" in out or "status" in out, out[:250])

    code, out = run_cli(["call", "vm_clip_delete",
                         json.dumps({"project_id": project_id, "clip_id": "A1"}),
                         "--compact"], env)
    check("删片段", last_json(out).get("ok") is True, out[:250])

    code, out = run_cli(["call", "vm_project_list", "{}", "--compact"], env)
    check("列项目", last_json(out).get("ok") is True, out[:250])


def test_legacy_flow(env):
    print("\n=== F. 传统混剪流 ===")
    code, out = run_cli(["call", "vm_legacy_preflight",
                         json.dumps({"hook_dir": "D:/Hook", "body_dirs": ["D:/Body"],
                                     "count": 20}), "--compact"], env)
    body = last_json(out)
    check("预检", body.get("capacity") == 12, out[:350])

    code, out = run_cli(["call", "vm_legacy_render",
                         json.dumps({"hook_dir": "D:/Hook", "body_dirs": ["D:/Body"],
                                     "count": 20}), "--compact"], env)
    body = last_json(out)
    if not check("启动传统任务", bool(body.get("task_id")), out[:350]):
        return
    task_id = body["task_id"]

    code, out = run_cli(["run", "vm_task_status", "task_id=" + task_id, "--compact"], env)
    check("查任务状态", "running" in out, out[:250])

    code, out = run_cli(["run", "vm_task_logs", "task_id=" + task_id, "--compact"], env)
    check("读任务日志", "logs" in out, out[:250])

    code, out = run_cli(["run", "vm_task_stop", "task_id=" + task_id, "--compact"], env)
    check("停任务", last_json(out).get("ok") is True, out[:250])

    code, out = run_cli(["run", "vm_task_resume", "task_id=" + task_id, "--compact"], env)
    check("续跑任务", last_json(out).get("ok") is True, out[:250])

    code, out = run_cli(["call", "vm_task_list", "{}", "--compact"], env)
    body = last_json(out)
    check("列任务", body.get("ok") is True and isinstance(body.get("tasks"), list), out[:250])


def test_error_paths(env):
    print("\n=== G. 错误路径 ===")
    code, out = run_cli(["discover", "--compact"],
                        {"VMAGENT_BASE": "http://127.0.0.1:59999"})
    check("后端离线时报错清晰",
          code != 0 and ("不可用" in out or "找不到" in out), out[:350])

    code, out = run_cli(["call", "vm_render",
                         json.dumps({"plan_id": "x"}), "--compact"], env)
    check("缺必填参数被拒", code == 2 and "缺少必填" in out, out[:250])

    code, out = run_cli(["call", "vm_project_get",
                         json.dumps({"project_id": "不存在的项目"},
                                    ensure_ascii=False), "--compact"], env)
    check("后端 404 转为可读错误",
          code != 0 and ("404" in out or "not found" in out), out[:350])

    code, out = run_cli(["call", "vm_plan_create",
                         json.dumps({"project_id": "proj-1", "formula": "ABCDEFG",
                                     "count": 1, "product_id": "p"}), "--compact"], env)
    check("公式含非法字母被本地拦截", code == 2 and "只能使用 A-F" in out, out[:250])

    code, out = run_cli(["call", "vm_render",
                         json.dumps({"plan_id": "", "output_dir": "D:/o"}),
                         "--compact"], env)
    check("空字符串视为缺失", code == 2 and "缺少必填" in out, out[:250])


def main():
    print("=" * 72)
    print("vmagent 一致性自测  ·  %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 72)

    mock_proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "mock_videomatrix.py"), str(PORT_MOCK)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port(PORT_MOCK):
            print("模拟后端启动失败,中止")
            return 2
        env = {"VMAGENT_BASE": "http://127.0.0.1:%d" % PORT_MOCK}

        test_registry()
        test_cli_layers(env)
        test_http_layer(env)
        test_mcp_layer(env)
        test_business_flow(env)
        test_legacy_flow(env)
        test_error_paths(env)
    finally:
        mock_proc.terminate()
        try:
            mock_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            mock_proc.kill()

    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [r for r in RESULTS if not r[1]]

    print("\n" + "=" * 72)
    print("结果: %d/%d 通过" % (passed, total))
    if failed:
        print("\n失败项:")
        for label, _, detail in failed:
            print("  - %s" % label)
            if detail:
                print("    " + detail.split("\n")[0][:300])
    print("=" * 72)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
