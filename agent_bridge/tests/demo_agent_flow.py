#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""demo_agent_flow — 以「Agent 视角」跑一遍完整流程。

模拟一个 LLM Agent 拿到 MCP 工具列表后,依次调用工具完成任务。
这不是测试,是演示:打印每一步 Agent 会看到什么。

用法:
  python3 tests/demo_agent_flow.py            # 自动起模拟后端
  python3 tests/demo_agent_flow.py --real     # 连真实后端(需先打开 VideoMatrix)
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

PORT = 8823


def wait_port(port, timeout=12.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def banner(text):
    print("\n" + "-" * 68)
    print(text)
    print("-" * 68)


def show(label, value, limit=380):
    text = json.dumps(value, ensure_ascii=False)
    if len(text) > limit:
        text = text[:limit] + " …(截断)"
    print("  %-22s %s" % (label, text))


def main():
    real = "--real" in sys.argv
    mock = None
    if not real:
        mock = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "mock_videomatrix.py"), str(PORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_port(PORT):
            print("模拟后端启动失败")
            return 2
        os.environ["VMAGENT_BASE"] = "http://127.0.0.1:%d" % PORT

    try:
        from vmagent.core import VMError
        from vmagent.tools import REGISTRY, call_tool, list_tools

        banner("① Agent 连接:它先看到有哪些工具")
        print("  工具总数: %d" % len(list_tools()))
        for group in ("connection", "structured-read", "structured-write",
                      "render", "legacy"):
            names = [t["name"] for t in list_tools(group)]
            print(" %-18s %d 个: %s" % (group, len(names), ", ".join(names)))

        banner("② 探测后端")
        info = call_tool("vm_discover", {})
        show("flavor", info.get("flavor"))
        show("version", info.get("version"))
        show("base", info.get("base"))
        if not info.get("ok"):
            print("\n  后端不可用: %s" % info.get("error"))
            print("  请先打开 VideoMatrix 桌面程序,或去掉 --real 用模拟后端演示。")
            return 0
        if info.get("flavor") != "structured":
            print("\n  当前是开源版后端(无结构混剪)。下面的 structured 步骤会被拒绝。")
            return 0

        banner("③ 建项目 + 产品")
        project = call_tool("vm_project_create", {"name": "Agent 演示项目"})
        project_id = project["project"]["id"]
        show("project_id", project_id)

        product = call_tool("vm_product_create",
                            {"project_id": project_id, "name": "秋季新品"})
        product_id = product["product"]["id"]
        show("product_id", product_id)

        banner("④ 导入素材目录")
        imported = call_tool("vm_import_sources",
                             {"project_id": project_id,
                              "directory": "D:/素材/Hook",
                              "product_id": product_id,
                              "recursive": True})
        show("imported", imported.get("imported"))
        sources = imported.get("sources") or []
        for one in sources[:3]:
            print("    source id=%-8s duration=%ss path=%s"
                  % (one.get("id"), one.get("duration"), one.get("path")))
        if not sources:
            print("  没导入到素材,后续步骤跳过")
            return 0

        banner("⑤ 打片段(关键:approved 才能进组合)")
        clip_result = call_tool("vm_clip_add",
                                {"project_id": project_id,
                                 "source_id": sources[0]["id"],
                                 "category": "A",
                                 "start": 0, "end": 3.2,
                                 "note": "爆款开头1",
                                 "approved": True})
        show("clip", clip_result.get("clip"))

        call_tool("vm_clip_add",
                  {"project_id": project_id,
                   "source_id": sources[0]["id"],
                   "category": "B",
                   "start": 3.2, "end": 6.4,
                   "approved": True})

        pending = call_tool("vm_clip_add",
                            {"project_id": project_id,
                             "source_id": sources[0]["id"],
                             "category": "C",
                             "start": 6.4, "end": 9.0})
        show("未核查片段状态", (pending.get("clip") or {}).get("cut_status"))

        banner("⑥ 查看项目摘要(Agent 决策依据)")
        summary = call_tool("vm_project_summary", {"project_id": project_id})
        for key in ("sources", "clips_total", "clips_by_category",
                    "clips_approved_by_category", "clips_pending", "plans"):
            print("  %-28s %s" % (key, json.dumps(summary.get(key), ensure_ascii=False)))
        print("\n  → Agent 由此判断:有 2 个已核查片段,可以生成 ABCDEF 形式的组合" )

        banner("⑦ 本地校验:非法公式不必发到后端")
        try:
            call_tool("vm_plan_create",
                      {"project_id": project_id, "formula": "BCDEF",
                       "count": 5, "product_id": product_id})
        except VMError as exc:
            print("  被本地拦截: %s" % exc)

        banner("⑧ 生成组合清单")
        plan = call_tool("vm_plan_create",
                         {"project_id": project_id,
                          "formula": "ABCDEF",
                          "count": 5,
                          "product_id": product_id})
        show("plan_id", plan.get("plan_id"))
        show("message", plan.get("message"))
        combos = plan.get("combinations") or []
        print("  前 2 条组合:")
        for combo in combos[:2]:
            print("    %s" % json.dumps(combo, ensure_ascii=False))

        banner("⑨ 导出成片")
        render = call_tool("vm_render",
                           {"plan_id": plan["plan_id"],
                            "output_dir": "D:/out",
                            "width": 1080, "height": 1920,
                            "fps": "30"})
        show("render", render.get("render"))

        banner("⑩ 轮询进度")
        render_id = (render.get("render") or {}).get("id")
        for attempt in range(3):
            status = call_tool("vm_render_status", {"render_id": render_id})
            body = status.get("render") or {}
            print("  第 %d 次: status=%-10s done=%s/%s"
                  % (attempt + 1, body.get("status"),
                     body.get("done"), body.get("total")))
            time.sleep(0.4)

        banner("⑪ 收尾:停止导出")
        show("stop", call_tool("vm_render_stop", {"render_id": render_id}))

        banner("完成")
        print("  Agent 用到的工具: vm_discover, vm_project_create, vm_product_create,")
        print("    vm_import_sources, vm_clip_add, vm_project_summary, vm_plan_create,")
        print("    vm_render, vm_render_status, vm_render_stop")
        print("\n  全部经 HTTP 走后端,未修改 VideoMatrix 任何文件。")
        return 0

    finally:
        if mock is not None:
            mock.terminate()
            try:
                mock.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mock.kill()


if __name__ == "__main__":
    sys.exit(main())
