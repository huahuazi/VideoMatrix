#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mock_videomatrix — 离线自测用的 VideoMatrix 后端模拟器。

只为在没有 Windows 真机时验证 vmagent 的协议层与工具逻辑。
实现 2.8.4.1 的 /api 与 /api/structured 两套命名空间的代表性子集。
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT_DEFAULT = 8799
VERSION = "2.8.4.1"
INSTANCE = "mock-instance-2841"

STATE = {"projects": {}, "renders": {}, "simple_renders": {}, "tasks": {}, "seq": {}}


def _next(key):
    STATE["seq"][key] = STATE["seq"].get(key, 0) + 1
    return STATE["seq"][key]


def new_project(name):
    pid = "proj-%d" % _next("proj")
    project = {
        "id": pid, "name": name, "products": [], "folders": [], "sources": [],
        "clips": [], "formulas": [], "plans": [], "renders": [],
        "category_labels": {"A": "爆款开头", "B": "购买理由", "C": "卖点",
                            "D": "促销", "E": "直播间引流", "F": "片段 F",
                            "G": "片段 G", "T": "历史待测片段"},
    }
    STATE["projects"][pid] = project
    return project


new_project("演示项目")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) or {}
        except Exception:
            return {}

    def do_GET(self):
        path = self.path.split("?")[0]
        parts = [p for p in path.strip("/").split("/") if p]

        if path == "/api/health":
            return self._json(200, {"status": "ok", "version": VERSION, "instance": INSTANCE})
        if path == "/api/openapi.json":
            return self._json(200, {"openapi": "3.0.0", "paths": {
                "/api/health": {"get": {}},
                "/api/structured/projects": {"get": {}, "post": {}},
                "/api/structured/renders": {"post": {}},
                "/api/tasks": {"get": {}, "post": {}}}})

        if len(parts) >= 3 and parts[1] == "structured":
            rest = parts[2:]
            if rest == ["projects"]:
                return self._json(200, {"projects": list(STATE["projects"].values())})
            if rest and rest[0] == "renders" and len(rest) == 2:
                found = STATE["renders"].get(rest[1])
                if not found:
                    return self._json(404, {"detail": "render not found"})
                return self._json(200, {"render": found})
            if rest and rest[0] == "simple-renders" and len(rest) == 2:
                found = STATE["simple_renders"].get(rest[1])
                if not found:
                    return self._json(404, {"detail": "simple render not found"})
                return self._json(200, {"simple_render": found})
            if rest and rest[0] == "projects" and len(rest) >= 2:
                project = STATE["projects"].get(rest[1])
                if not project:
                    return self._json(404, {"detail": "project not found"})
                tail = rest[2:]
                if not tail:
                    return self._json(200, {"project": project})
                if tail == ["category-labels"]:
                    return self._json(200, {"labels": project["category_labels"]})
                if tail == ["products"]:
                    return self._json(200, {"products": project["products"]})

        if parts[:2] == ["api", "tasks"]:
            if len(parts) == 2:
                return self._json(200, list(STATE["tasks"].values()))
            task = STATE["tasks"].get(parts[2])
            if not task:
                return self._json(404, {"detail": "task not found"})
            if len(parts) == 4 and parts[3] == "logs":
                return self._json(200, {"logs": task.get("logs", [])})
            return self._json(200, task)

        return self._json(404, {"detail": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        parts = [p for p in path.strip("/").split("/") if p]
        body = self._body()

        if len(parts) >= 3 and parts[1] == "structured":
            rest = parts[2:]

            if rest == ["projects"]:
                return self._json(200, {"project": new_project(body.get("name", "未命名"))})

            if rest == ["renders"]:
                plan_id = body.get("plan_id", "")
                out = body.get("output_dir", "")
                if not plan_id or not out:
                    missing = "plan_id" if not plan_id else "output_dir"
                    return self._json(422, {"detail": [
                        {"loc": ["body", missing], "msg": "required"}]})
                rid = "render-%d" % _next("render")
                render = {"id": rid, "plan_id": plan_id, "status": "running",
                          "output_dir": out, "done": 0, "total": 20,
                          "width": body.get("width"), "height": body.get("height"),
                          "fps": body.get("fps"), "failed": []}
                STATE["renders"][rid] = render
                return self._json(200, {"render": render})

            if rest == ["renders", "stop-all"]:
                count = 0
                for render in STATE["renders"].values():
                    if render["status"] == "running":
                        render["status"] = "stopped"
                        count += 1
                return self._json(200, {"ok": True, "stopped": count})

            if len(rest) == 3 and rest[0] == "renders" and rest[2] == "stop":
                render = STATE["renders"].get(rest[1])
                if not render:
                    return self._json(404, {"detail": "render not found"})
                render["status"] = "stopped"
                return self._json(200, {"ok": True, "stopped": rest[1]})

            if rest and rest[0] == "projects" and len(rest) >= 2:
                project = STATE["projects"].get(rest[1])
                if not project:
                    return self._json(404, {"detail": "project not found"})
                tail = rest[2:]

                if tail == ["products"]:
                    prod = {"id": "prod-%d" % _next("prod"), "name": body.get("name", "")}
                    project["products"].append(prod)
                    return self._json(200, {"product": prod})

                if tail == ["folders"]:
                    folder = {"id": "folder-%d" % _next("folder"),
                              "name": body.get("name", ""),
                              "product_id": body.get("product_id", "")}
                    project["folders"].append(folder)
                    return self._json(200, {"folder": folder})

                if tail == ["sources"]:
                    one = body.get("path", "")
                    if not one:
                        return self._json(422, {"detail": [
                            {"loc": ["body", "path"], "msg": "required"}]})
                    source = {"id": "src-%d" % _next("src"), "path": one,
                              "duration": 12.5,
                              "product_id": body.get("product_id", ""),
                              "folder_id": body.get("folder_id", "")}
                    project["sources"].append(source)
                    return self._json(200, {"source": source})

                if tail == ["sources", "import-directory"]:
                    directory = body.get("path", "")
                    if not directory:
                        return self._json(422, {"detail": [
                            {"loc": ["body", "path"], "msg": "required"}]})
                    made = []
                    for index in range(3):
                        source = {"id": "src-%d" % _next("src"),
                                  "path": "%s/clip%d.mp4" % (directory.rstrip("/"), index + 1),
                                  "duration": 10.0 + index,
                                  "product_id": body.get("product_id", ""),
                                  "folder_id": body.get("folder_id", "")}
                        project["sources"].append(source)
                        made.append(source)
                    return self._json(200, {"sources": made, "errors": []})

                if tail == ["sources", "assign"]:
                    return self._json(200, {"ok": True,
                                            "assigned": len(body.get("source_ids") or [])})

                if tail == ["clips"]:
                    category = str(body.get("category", "")).upper()
                    if not category:
                        return self._json(422, {"detail": [
                            {"loc": ["body", "category"], "msg": "required"}]})
                    num = 1
                    while True:
                        cid = "%s%d" % (category, num)
                        if not any(c.get("id") == cid for c in project["clips"]):
                            break
                        num += 1
                    clip = {"id": cid, "category": category,
                            "source_id": body.get("source_id", ""),
                            "start": body.get("start", 0), "end": body.get("end", 0),
                            "note": body.get("note", ""),
                            "cut_status": body.get("cut_status", "pending"),
                            "cut_reviewer": body.get("cut_reviewer", ""),
                            "cut_review_note": body.get("cut_review_note", "")}
                    project["clips"].append(clip)
                    return self._json(200, {"clip": clip})

                if tail == ["formulas"]:
                    formula = {"name": body.get("name", ""), "slots": body.get("slots", [])}
                    project["formulas"].append(formula)
                    return self._json(200, {"formula": formula})

                if tail in (["plans"], ["hook-tests"], ["shot-tests"]):
                    formula = body.get("formula", "A")
                    count = int(body.get("count") or body.get("per_hook_count") or 2)
                    combos = [{"hooks": ["A1"], "slots": ["B%d" % (i + 1), "C1"]}
                              for i in range(min(count, 5))]
                    plan = {"id": "plan-%d" % _next("plan"), "formula": formula,
                            "combinations": combos}
                    project["plans"].append(plan)
                    return self._json(200, {"message": "已生成 %d 条组合" % len(combos),
                                            "combinations": combos,
                                            "plan_id": plan["id"]})

                if tail == ["simple-renders"]:
                    rid = "sr-%d" % _next("sr")
                    record = {"id": rid, "plan_id": body.get("plan_id", ""),
                              "status": "queued"}
                    STATE["simple_renders"][rid] = record
                    return self._json(200, {"simple_render": record})

            return self._json(404, {"detail": "not found"})

        if path == "/api/preflight":
            if not (body.get("hook_dir") or ""):
                return self._json(422, {"detail": [
                    {"loc": ["body", "hook_dir"], "msg": "required"}]})
            return self._json(200, {"ok": True, "capacity": 12,
                                    "report": [{"name": "hook", "ok": True},
                                               {"name": "body", "ok": True}]})

        if path == "/api/tasks":
            tid = "task-%d" % _next("task")
            config = body.get("config", {})
            task = {"task_id": tid, "task_name": config.get("task_name", ""),
                    "status": "running", "success_count": 0, "failed_count": 0,
                    "target_count": config.get("target_count", 10),
                    "logs": [">>> 任务已启动"]}
            STATE["tasks"][tid] = task
            return self._json(200, {"task_id": tid})

        if len(parts) == 4 and parts[1] == "tasks" and parts[3] in ("stop", "resume"):
            task = STATE["tasks"].get(parts[2])
            if not task:
                return self._json(404, {"detail": "task not found"})
            task["status"] = "stopped" if parts[3] == "stop" else "running"
            return self._json(200, {"ok": True})

        if path == "/api/scan":
            return self._json(200, {"files": ["a.mp4", "b.mp4"], "count": 2})

        return self._json(404, {"detail": "not found"})

    def do_PATCH(self):
        path = self.path.split("?")[0]
        parts = [p for p in path.strip("/").split("/") if p]
        body = self._body()

        if len(parts) >= 3 and parts[1] == "structured" and parts[2] == "projects":
            project = STATE["projects"].get(parts[3]) if len(parts) >= 4 else None
            if not project:
                return self._json(404, {"detail": "project not found"})
            tail = parts[4:]

            if tail == ["category-labels"]:
                for key, value in body.items():
                    if key in project["category_labels"]:
                        project["category_labels"][key] = value
                return self._json(200, {"labels": project["category_labels"]})

            if len(tail) == 2 and tail[0] == "clips":
                for clip in project["clips"]:
                    if clip["id"] == tail[1]:
                        clip.update(body)
                        return self._json(200, {"clip": clip})
                return self._json(404, {"detail": "clip not found"})

            if len(tail) == 2 and tail[0] == "products":
                for prod in project["products"]:
                    if prod["id"] == tail[1]:
                        prod.update(body)
                        return self._json(200, {"product": prod})
                return self._json(404, {"detail": "product not found"})

        return self._json(404, {"detail": "not found"})

    def do_DELETE(self):
        path = self.path.split("?")[0]
        parts = [p for p in path.strip("/").split("/") if p]
        if len(parts) >= 4 and parts[1] == "structured" and parts[2] == "projects":
            project = STATE["projects"].get(parts[3])
            if not project:
                return self._json(404, {"detail": "project not found"})
            tail = parts[4:]
            if len(tail) == 2 and tail[0] == "clips":
                before = len(project["clips"])
                project["clips"] = [c for c in project["clips"] if c["id"] != tail[1]]
                if len(project["clips"]) == before:
                    return self._json(404, {"detail": "clip not found"})
                return self._json(200, {"deleted": tail[1]})
        return self._json(404, {"detail": "not found"})


def serve(port=PORT_DEFAULT):
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT_DEFAULT
    srv = serve(port)
    sys.stderr.write("mock VideoMatrix on http://127.0.0.1:%d\n" % port)
    sys.stderr.flush()
    srv.serve_forever()
