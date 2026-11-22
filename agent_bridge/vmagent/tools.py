"""vmagent.tools — 工具注册表(单一事实源)。

每个工具声明一次:name / description / JSON Schema / handler。
MCP、HTTP、CLI 三种协议层都从这里自动生成,不存在重复定义。
"""
from __future__ import annotations

import urllib.parse

from typing import Any, Dict, List, Optional

from .core import (DEFAULT_TIMEOUT, VMError, _b, _proj, _require_structured,
                   discover, passthrough)

REGISTRY: Dict[str, Dict[str, Any]] = {}


def tool(name, description, schema, group="general", destructive=False):
    """注册一个工具。schema 为 JSON Schema 的 properties 部分。"""
    required = sorted(k for k, v in schema.items() if v.get("x-required"))
    clean = {}
    for key, value in schema.items():
        item = dict(value)
        item.pop("x-required", None)
        clean[key] = item

    def wrap(fn):
        REGISTRY[name] = {
            "name": name,
            "description": description,
            "schema": {
                "type": "object",
                "properties": clean,
                "required": required,
                "additionalProperties": False,
            },
            "group": group,
            "destructive": destructive,
            "handler": fn,
        }
        return fn
    return wrap


def _S(typ, desc, required=False, **extra):
    out = {"type": typ, "description": desc}
    if required:
        out["x-required"] = True
    out.update(extra)
    return out


# ===========================================================================
# 连接与自描述
# ===========================================================================

@tool("vm_discover",
      "连接 VideoMatrix 并返回后端地址、版本、能力面(是否支持结构混剪)。任何操作前先调用它确认软件已启动。",
      {}, group="connection")
def vm_discover():
    backend = discover(force=True)
    info = backend.describe()
    info["tools"] = sorted(REGISTRY)
    info["hint"] = "用 vm_project_list 看现有项目;用 vm_openapi 看工具集之外的原始端点。"
    return info


@tool("vm_health", "轻量健康检查,只确认后端存活与版本,不扫描项目。",
      {}, group="connection")
def vm_health():
    backend = _b()
    return {"ok": True, "version": backend.health.get("version"),
            "instance": backend.health.get("instance"), "base": backend.base}


@tool("vm_openapi",
      "导出后端完整 OpenAPI 文档(端点 + 请求模型)。用于发现工具集之外的新能力,或核对参数契约。",
      {"path_only": _S("boolean", "只返回方法+路径清单,不返回完整 schema")},
      group="connection")
def vm_openapi(path_only=False):
    doc = _b().request("GET", "openapi.json")
    if not isinstance(doc, dict):
        return {"ok": True, "raw": doc}
    paths = doc.get("paths") or {}
    if path_only:
        summary = []
        for path, ops in sorted(paths.items()):
            for method in ops:
                if method.lower() in ("get", "post", "put", "patch", "delete"):
                    summary.append(method.upper() + " " + path)
        return {"ok": True, "count": len(summary), "endpoints": summary}
    return {"ok": True, "endpoints": len(paths), "openapi": doc}


@tool("vm_api_call",
      "通用逃生舱:直接调用任意后端端点。新版本新增的 API 不必等工具更新即可使用。",
      {"method": _S("string", "HTTP 方法: GET / POST / PATCH / DELETE", True),
       "path": _S("string", "相对路径,不带 /api 前缀。如 tasks 或 structured/projects", True),
       "body": _S("object", "JSON 请求体(POST / PATCH 用)"),
       "timeout": _S("number", "超时秒数,默认 60;长任务可调到 1800")},
      group="connection")
def vm_api_call(method, path, body=None, timeout=None):
    result = _b().request(method, path, body, timeout or DEFAULT_TIMEOUT)
    return {"ok": True, "result": result}


# ===========================================================================
# 结构混剪:项目读取
# ===========================================================================

@tool("vm_project_list", "列出全部结构混剪项目。", {}, group="structured-read")
def vm_project_list():
    backend = _b()
    _require_structured(backend)
    return passthrough(backend.request("GET", "structured/projects"))


@tool("vm_project_get", "读取单个项目完整状态:产品、文件夹、素材、片段、公式、历史清单。",
      {"project_id": _S("string", "项目 ID", True)}, group="structured-read")
def vm_project_get(project_id):
    backend = _b()
    _require_structured(backend)
    return passthrough(backend.request("GET", _proj(project_id)))


@tool("vm_project_summary",
      "把项目压成 Agent 友好摘要:各类别(A-G/T)片段数、已核查数、待核查数、素材数、公式与清单数。"
      "规划下一步前先调用它,避免拉全量 JSON 占用上下文。",
      {"project_id": _S("string", "项目 ID", True)}, group="structured-read")
def vm_project_summary(project_id):
    backend = _b()
    _require_structured(backend)
    raw = backend.request("GET", _proj(project_id))
    project = raw.get("project", raw) if isinstance(raw, dict) else {}

    clips = project.get("clips") or []
    by_category = {}
    approved = {}
    pending = 0
    blocked = 0
    for clip in clips:
        cat = str(clip.get("category", "?"))
        by_category[cat] = by_category.get(cat, 0) + 1
        status = clip.get("cut_status") or "pending"
        if status == "approved":
            approved[cat] = approved.get(cat, 0) + 1
        elif status == "blocked":
            blocked += 1
        else:
            pending += 1

    return {
        "ok": True,
        "project_id": project.get("id", project_id),
        "name": project.get("name"),
        "products": len(project.get("products") or []),
        "folders": len(project.get("folders") or []),
        "sources": len(project.get("sources") or []),
        "clips_total": len(clips),
        "clips_by_category": by_category,
        "clips_approved_by_category": approved,
        "clips_pending": pending,
        "clips_blocked": blocked,
        "formulas": [f.get("name") for f in (project.get("formulas") or [])],
        "plans": len(project.get("plans") or []),
        "category_labels": project.get("category_labels") or {},
    }


@tool("vm_source_list", "列出项目内全部素材(id / 路径 / 时长 / 归属),供打片段时选取。",
      {"project_id": _S("string", "项目 ID", True)}, group="structured-read")
def vm_source_list(project_id):
    backend = _b()
    _require_structured(backend)
    raw = backend.request("GET", _proj(project_id))
    project = raw.get("project", raw) if isinstance(raw, dict) else {}
    sources = project.get("sources") or []
    return {"ok": True, "count": len(sources),
            "sources": [{"id": s.get("id"), "path": s.get("path"),
                         "duration": s.get("duration"),
                         "product_id": s.get("product_id"),
                         "folder_id": s.get("folder_id")} for s in sources]}


@tool("vm_clip_list", "列出项目内片段,可按类别或核查状态过滤。",
      {"project_id": _S("string", "项目 ID", True),
       "category": _S("string", "只看某一类别,如 A / B / T"),
       "cut_status": _S("string", "只看某状态: pending / approved / blocked")},
      group="structured-read")
def vm_clip_list(project_id, category="", cut_status=""):
    backend = _b()
    _require_structured(backend)
    raw = backend.request("GET", _proj(project_id))
    project = raw.get("project", raw) if isinstance(raw, dict) else {}
    clips = project.get("clips") or []
    if category:
        want = category.upper()
        clips = [c for c in clips if str(c.get("category", "")).upper() == want]
    if cut_status:
        clips = [c for c in clips if (c.get("cut_status") or "pending") == cut_status]
    return {"ok": True, "count": len(clips),
            "clips": [{"id": c.get("id"), "category": c.get("category"),
                       "start": c.get("start"), "end": c.get("end"),
                       "note": c.get("note"),
                       "cut_status": c.get("cut_status") or "pending",
                       "source_id": c.get("source_id")} for c in clips]}


# ===========================================================================
# 结构混剪:项目写入
# ===========================================================================

@tool("vm_project_create", "新建结构混剪项目。",
      {"name": _S("string", "项目名称,如 八月爆款", True)}, group="structured-write")
def vm_project_create(name):
    backend = _b()
    _require_structured(backend)
    return passthrough(backend.request("POST", "structured/projects", {"name": name}))


@tool("vm_product_create", "在项目内新建产品(产品用于隔离素材,禁止跨产品引用片段)。",
      {"project_id": _S("string", "项目 ID", True),
       "name": _S("string", "产品名称", True)}, group="structured-write")
def vm_product_create(project_id, name):
    backend = _b()
    _require_structured(backend)
    return passthrough(backend.request("POST", _proj(project_id) + "/products", {"name": name}))


@tool("vm_folder_create", "在项目内新建素材文件夹,可挂到某产品下。",
      {"project_id": _S("string", "项目 ID", True),
       "name": _S("string", "文件夹名称", True),
       "product_id": _S("string", "可选,归属产品 ID")}, group="structured-write")
def vm_folder_create(project_id, name, product_id=""):
    backend = _b()
    _require_structured(backend)
    payload = {"name": name}
    if product_id:
        payload["product_id"] = product_id
    return passthrough(backend.request("POST", _proj(project_id) + "/folders", payload))


@tool("vm_category_labels",
      "读取或修改项目的片段类别名称(A-G 与 T)。省略 labels 为读取;给出 labels 为修改(仅可改 A-G 和 T,1-40 字)。",
      {"project_id": _S("string", "项目 ID", True),
       "labels": _S("object", "要改的类别名,如 {\"A\":\"钩子开头\"};省略则只读取")},
      group="structured-write")
def vm_category_labels(project_id, labels=None):
    backend = _b()
    _require_structured(backend)
    path = _proj(project_id) + "/category-labels"
    if labels:
        return passthrough(backend.request("PATCH", path, labels))
    return passthrough(backend.request("GET", path))


# ===========================================================================
# 素材导入
# ===========================================================================

@tool("vm_import_sources",
      "把本机视频导入项目。给 directory 则整目录导入(可递归),给 paths 则逐个导入。"
      "返回新建 source 列表(含 id 与时长),后续 vm_clip_add 要用这些 id。",
      {"project_id": _S("string", "项目 ID", True),
       "directory": _S("string", "要导入的目录绝对路径"),
       "paths": _S("array", "要导入的文件绝对路径列表", items={"type": "string"}),
       "product_id": _S("string", "归入的产品 ID"),
       "folder_id": _S("string", "归入的文件夹 ID"),
       "recursive": _S("boolean", "目录导入是否递归子目录,默认 false")},
      group="structured-write")
def vm_import_sources(project_id, directory="", paths=None, product_id="",
                      folder_id="", recursive=False):
    backend = _b()
    _require_structured(backend)
    base = _proj(project_id)
    imported = []
    errors = []

    if directory:
        payload = {"path": directory, "recursive": bool(recursive)}
        if product_id:
            payload["product_id"] = product_id
        if folder_id:
            payload["folder_id"] = folder_id
        data = backend.request("POST", base + "/sources/import-directory",
                               payload, timeout=1800)
        if isinstance(data, dict):
            imported.extend(data.get("sources") or [])
            errors.extend(data.get("errors") or [])

    for one in (paths or []):
        payload = {"path": one}
        if product_id:
            payload["product_id"] = product_id
        if folder_id:
            payload["folder_id"] = folder_id
        try:
            imported.append(backend.request("POST", base + "/sources", payload))
        except VMError as exc:
            errors.append({"path": one, "error": str(exc)})

    return {"ok": True, "imported": len(imported),
            "sources": imported, "errors": errors}


# ===========================================================================
# 片段标注
# ===========================================================================

@tool("vm_clip_add",
      "给素材打片段:指定类别(A-G 或 T)、入点、出点(秒)。"
      "默认 cut_status=pending(需人工核查后才可参与组合);approved=true 表示已完成切点核查。"
      "A 类=开头类,T 类=待测开头,两者是组合公式的起点。",
      {"project_id": _S("string", "项目 ID", True),
       "source_id": _S("string", "素材 ID(来自 vm_source_list)", True),
       "category": _S("string", "类别:A/B/C/D/E/F/G/T", True),
       "start": _S("number", "入点秒数", True),
       "end": _S("number", "出点秒数", True),
       "note": _S("string", "片段备注,如 低价促销开场"),
       "approved": _S("boolean", "是否标记为人工已核查(可参与组合)"),
       "cut_review_note": _S("string", "复核说明,如 首尾无异镜、口播完整")},
      group="structured-write")
def vm_clip_add(project_id, source_id, category, start, end,
                note="", approved=False, cut_review_note=""):
    backend = _b()
    _require_structured(backend)
    payload = {
        "source_id": source_id,
        "category": str(category).upper(),
        "start": float(start),
        "end": float(end),
        "note": note or "",
        "cut_status": "approved" if approved else "pending",
        "cut_reviewer": "user" if approved else "",
        "cut_review_note": cut_review_note or ("命令行标记" if approved else ""),
    }
    return passthrough(backend.request("POST", _proj(project_id) + "/clips", payload))


@tool("vm_clip_update",
      "修改已有片段:时间码、备注、核查状态。只传要改的字段。"
      "approved=true 会把片段置为已核查(可用于组合)。",
      {"project_id": _S("string", "项目 ID", True),
       "clip_id": _S("string", "片段 ID,如 A1", True),
       "start": _S("number", "新入点秒数"),
       "end": _S("number", "新出点秒数"),
       "note": _S("string", "新备注"),
       "cut_status": _S("string", "直接指定状态: pending / approved / blocked"),
       "approved": _S("boolean", "置为已核查"),
       "cut_review_note": _S("string", "复核说明")},
      group="structured-write")
def vm_clip_update(project_id, clip_id, start=None, end=None, note=None,
                   cut_status="", approved=False, cut_review_note=""):
    backend = _b()
    _require_structured(backend)
    payload = {}
    if start is not None:
        payload["start"] = float(start)
    if end is not None:
        payload["end"] = float(end)
    if note is not None:
        payload["note"] = note
    if cut_status:
        payload["cut_status"] = cut_status
    if approved:
        payload["cut_status"] = "approved"
        payload["cut_reviewer"] = "user"
    if cut_review_note:
        payload["cut_review_note"] = cut_review_note
    if not payload:
        raise VMError("vm_clip_update 需要至少一个要修改的字段")
    return passthrough(backend.request("PATCH",
                                    _proj(project_id) + "/clips/" +
                                    urllib.parse.quote(clip_id, safe=""), payload))


@tool("vm_clip_delete", "删除片段。会破坏引用它的组合清单,需重新生成。",
      {"project_id": _S("string", "项目 ID", True),
       "clip_id": _S("string", "片段 ID", True)},
      group="structured-write", destructive=True)
def vm_clip_delete(project_id, clip_id):
    backend = _b()
    _require_structured(backend)
    return passthrough(backend.request("DELETE",
                                       _proj(project_id) + "/clips/" +
                                       urllib.parse.quote(clip_id, safe="")))


# ===========================================================================
# 公式与组合清单
# ===========================================================================

@tool("vm_formula_save", "把组合公式保存到项目,便于复用。formula 是字母序列,如 ABCDEF。",
      {"project_id": _S("string", "项目 ID", True),
       "name": _S("string", "公式名称", True),
       "formula": _S("string", "字母序列,仅 A-F,每字符一个片段位,最长 16", True)},
      group="structured-write")
def vm_formula_save(project_id, name, formula):
    backend = _b()
    _require_structured(backend)
    slots = list(_normalize_formula(formula))
    return passthrough(backend.request("POST", _proj(project_id) + "/formulas",
                                       {"name": name, "slots": slots}))


def _normalize_formula(formula):
    text = "".join(ch for ch in str(formula or "").upper().strip()
                   if ch.isalpha())
    if not text:
        raise VMError("公式不能为空")
    if any(ch not in "ABCDEF" for ch in text):
        raise VMError("公式只能使用 A-F,例如 ABCDEF 或 ACDBEF")
    if not text.startswith("A"):
        raise VMError("公式必须以 A 开头(每条视频第一个片段是开头类)")
    if len(text) > 16:
        raise VMError("每条公式最多 16 个片段")
    return text


@tool("vm_plan_create",
      "生成组合清单(Plan):按公式从已核查片段里抽组合。返回 plan_id,导出成片要用它。"
      "count 为目标条数(1-500);distinct_sources=true 时同一条视频内不重复用同一素材。",
      {"project_id": _S("string", "项目 ID", True),
       "formula": _S("string", "组合公式,如 ABCDEF(必须以 A 开头)", True),
       "count": _S("integer", "生成条数,1-500", True),
       "product_id": _S("string", "产品 ID", True),
       "folder_ids": _S("array", "限定只从这些文件夹抽取", items={"type": "string"}),
       "hook_ids": _S("array", "限定只用这些开头片段(A/T 类)", items={"type": "string"}),
       "per_hook_count": _S("integer", "每个开头生成几条正文组合"),
       "distinct_sources": _S("boolean", "同一条视频内不重复使用同一素材"),
       "manual": _S("array", "手工指定组合,每项形如 A1B5C7D9", items={"type": "string"})},
      group="structured-write")
def vm_plan_create(project_id, formula, count, product_id,
                   folder_ids=None, hook_ids=None, per_hook_count=None,
                   distinct_sources=False, manual=None):
    backend = _b()
    _require_structured(backend)
    payload = {
        "formula": _normalize_formula(formula),
        "count": int(count),
        "product_id": product_id,
        "distinct_sources": bool(distinct_sources),
    }
    if folder_ids:
        payload["folder_ids"] = folder_ids
    if hook_ids:
        payload["hook_ids"] = hook_ids
    if per_hook_count:
        payload["per_hook_count"] = int(per_hook_count)
    if manual:
        payload["manual"] = manual
    return passthrough(backend.request("POST", _proj(project_id) + "/plans",
                                    payload, timeout=600))


@tool("vm_test_plan_create",
      "生成测试清单:kind=hook 为开头测试(每个开头搭配随机正文),"
      "kind=shot 为镜头测试。用于验证哪种开头/镜头组合效果好。",
      {"project_id": _S("string", "项目 ID", True),
       "kind": _S("string", "hook 或 shot", True),
       "formula": _S("string", "组合公式,必须以 A 开头", True),
       "product_id": _S("string", "产品 ID", True),
       "hook_ids": _S("array", "参与测试的开头片段 ID", items={"type": "string"}),
       "per_hook_count": _S("integer", "每个开头搭配几组正文,默认 2"),
       "folder_ids": _S("array", "限定正文文件夹", items={"type": "string"}),
       "distinct_sources": _S("boolean", "同一视频内不重复素材")},
      group="structured-write")
def vm_test_plan_create(project_id, kind, formula, product_id, hook_ids=None,
                        per_hook_count=2, folder_ids=None, distinct_sources=False):
    backend = _b()
    _require_structured(backend)
    which = str(kind).lower()
    if which not in ("hook", "shot"):
        raise VMError("kind 只支持 hook 或 shot")
    payload = {
        "product_id": product_id,
        "formula": _normalize_formula(formula),
        "hook_ids": hook_ids or [],
        "per_hook_count": int(per_hook_count or 2),
        "distinct_sources": bool(distinct_sources),
    }
    if folder_ids:
        payload["folder_ids"] = folder_ids
    suffix = "hook-tests" if which == "hook" else "shot-tests"
    return passthrough(backend.request("POST", _proj(project_id) + "/" + suffix,
                                    payload, timeout=600))


# ===========================================================================
# 导出
# ===========================================================================

@tool("vm_render",
      "按组合清单导出成片。plan_id 来自 vm_plan_create。"
      "输出目录用 Windows 绝对路径。返回 render_id,用 vm_render_status 轮询进度。",
      {"plan_id": _S("string", "组合清单 ID", True),
       "output_dir": _S("string", "输出目录绝对路径", True),
       "width": _S("integer", "输出宽,默认 1080"),
       "height": _S("integer", "输出高,默认 1920"),
       "fps": _S("string", "帧率,默认 30"),
       "keep_audio": _S("boolean", "保留原声,默认 true"),
       "postprocess": _S("boolean", "启用成品去重变换(微裁切/色彩/镜像/混合)"),
       "variant_config": _S("object", "去重变换细项:strength / crop / color / mirror / blend 等")},
      group="render")
def vm_render(plan_id, output_dir, width=1080, height=1920,
              fps="30", keep_audio=True, postprocess=False, variant_config=None):
    backend = _b()
    _require_structured(backend)
    payload = {
        "plan_id": plan_id,
        "output_dir": output_dir,
        "width": int(width),
        "height": int(height),
        "fps": str(fps),
        "keep_audio": bool(keep_audio),
    }
    if postprocess:
        payload["postprocess"] = True
        if variant_config:
            payload["variant_config"] = variant_config
    return passthrough(backend.request("POST", "structured/renders", payload))


@tool("vm_render_status", "查询一次导出任务进度:状态、已完成条数、失败项。",
      {"render_id": _S("string", "导出任务 ID", True)}, group="render")
def vm_render_status(render_id):
    backend = _b()
    _require_structured(backend)
    return passthrough(backend.request("GET", "structured/renders/" +
                                      urllib.parse.quote(render_id, safe="")))


@tool("vm_render_stop", "停止单个导出任务(已产出的成片保留)。",
      {"render_id": _S("string", "导出任务 ID", True)},
      group="render", destructive=True)
def vm_render_stop(render_id):
    backend = _b()
    _require_structured(backend)
    return passthrough(backend.request("POST", "structured/renders/" +
                                       urllib.parse.quote(render_id, safe="") + "/stop"))


@tool("vm_render_stop_all", "停止全部正在进行的导出任务(已产出的成片保留)。",
      {}, group="render", destructive=True)
def vm_render_stop_all():
    backend = _b()
    _require_structured(backend)
    return passthrough(backend.request("POST", "structured/renders/stop-all"))


@tool("vm_simple_render", "简易渲染:直接按清单导出,不走去重后处理。",
      {"project_id": _S("string", "项目 ID", True),
       "plan_id": _S("string", "组合清单 ID", True),
       "output_dir": _S("string", "输出目录绝对路径", True),
       "base_plan_id": _S("string", "可选基础清单 ID"),
       "width": _S("integer", "输出宽,默认 1080"),
       "height": _S("integer", "输出高,默认 1920"),
       "fps": _S("string", "帧率,默认 30"),
       "keep_audio": _S("boolean", "保留原声,默认 true")},
      group="render")
def vm_simple_render(project_id, plan_id, output_dir, base_plan_id="",
                     width=1080, height=1920, fps="30", keep_audio=True):
    backend = _b()
    _require_structured(backend)
    payload = {
        "plan_id": plan_id,
        "output_dir": output_dir,
        "width": int(width),
        "height": int(height),
        "fps": str(fps),
        "keep_audio": bool(keep_audio),
    }
    if base_plan_id:
        payload["base_plan_id"] = base_plan_id
    return passthrough(backend.request(
                "POST", _proj(project_id) + "/simple-renders", payload))


@tool("vm_simple_render_status", "查询简易渲染任务状态。",
      {"render_id": _S("string", "简易渲染任务 ID", True)}, group="render")
def vm_simple_render_status(render_id):
    backend = _b()
    _require_structured(backend)
    return passthrough(backend.request(
                "GET", "structured/simple-renders/" +
                urllib.parse.quote(render_id, safe="")))


# ===========================================================================
# 传统混剪(开源 /api 命名空间)
# ===========================================================================

@tool("vm_legacy_preflight",
      "预检传统混剪产能(不渲染):扫素材、算能产多少条、报缺什么。",
      {"hook_dir": _S("string", "Hook 素材目录", True),
       "body_dirs": _S("array", "Body 素材目录列表", items={"type": "string"}),
       "bgm_dir": _S("string", "BGM 目录或带音轨的视频"),
       "count": _S("integer", "目标条数,默认 10"),
       "clips": _S("integer", "每条视频片段数,默认 5"),
       "t_hook": _S("number", "首段时长秒,默认 3"),
       "t_body": _S("number", "后段时长秒,默认 3"),
       "resolution": _S("string", "分辨率,如 1080*1920"),
       "fps": _S("string", "帧率"),
       "bitrate": _S("string", "码率,如 8000k"),
       "concurrent": _S("integer", "并发数 1-16,默认 3")},
      group="legacy")
def vm_legacy_preflight(hook_dir, body_dirs=None, bgm_dir="", count=10,
                        clips=5, t_hook=3.0, t_body=3.0,
                        resolution="", fps="", bitrate="", concurrent=3):
    config = _legacy_config(hook_dir, body_dirs, bgm_dir, count, clips,
                            t_hook, t_body, resolution, fps, bitrate, concurrent)
    return passthrough(_b().request("POST", "preflight", config, timeout=900))


@tool("vm_legacy_render",
      "启动传统混剪任务(先自动预检,不合格不启动)。返回 task_id。",
      {"hook_dir": _S("string", "Hook 素材目录", True),
       "body_dirs": _S("array", "Body 素材目录列表", items={"type": "string"}),
       "bgm_dir": _S("string", "BGM 目录或带音轨的视频"),
       "count": _S("integer", "目标条数,默认 10"),
       "clips": _S("integer", "每条视频片段数,默认 5"),
       "t_hook": _S("number", "首段时长秒"),
       "t_body": _S("number", "后段时长秒"),
       "resolution": _S("string", "分辨率"),
       "fps": _S("string", "帧率"),
       "bitrate": _S("string", "码率"),
       "concurrent": _S("integer", "并发数 1-16"),
       "output_dir": _S("string", "输出父目录")},
      group="legacy")
def vm_legacy_render(hook_dir, body_dirs=None, bgm_dir="", count=10, clips=5,
                     t_hook=3.0, t_body=3.0, resolution="", fps="", bitrate="",
                     concurrent=3, output_dir=""):
    config = _legacy_config(hook_dir, body_dirs, bgm_dir, count, clips,
                            t_hook, t_body, resolution, fps, bitrate, concurrent)
    if output_dir:
        config["base_out_dir"] = output_dir
    backend = _b()
    pre = backend.request("POST", "preflight", config, timeout=900)
    if not pre.get("ok"):
        return {"ok": False, "stage": "preflight",
                "message": "预检未通过,未启动渲染", "preflight": pre}
    created = backend.request("POST", "tasks", {"config": config})
    return {"ok": True, "task_id": (created or {}).get("task_id"),
            "capacity": pre.get("capacity"), "created": created}


def _legacy_config(hook_dir, body_dirs, bgm_dir, count, clips,
                   t_hook, t_body, resolution, fps, bitrate, concurrent):
    config = {
        "task_name": "AgentTask",
        "hook_dir": hook_dir,
        "body_dirs": body_dirs or [],
        "t_hook": float(t_hook),
        "t_body": float(t_body),
        "total_clips": int(clips),
        "target_count": int(count),
        "concurrent_tasks": int(concurrent),
        "enable_gpu": True,
    }
    if bgm_dir:
        config["bgm_dir"] = bgm_dir
        config["bgm_tracks"] = {
            "full": {"enabled": True, "path": bgm_dir, "volume": 30,
                     "fade_in": 0, "fade_out": 0, "short_behavior": "loop",
                     "source_mode": "random", "overlap": 0.3},
            "hook": {"enabled": False},
            "body": {"enabled": False},
        }
    if resolution:
        config["resolution"] = resolution
    if fps:
        config["fps"] = str(fps)
    if bitrate:
        config["bitrate"] = bitrate
    return config


@tool("vm_task_list", "列出传统混剪任务及其状态。", {}, group="legacy")
def vm_task_list():
    data = _b().request("GET", "tasks")
    tasks = data if isinstance(data, list) else (data or {}).get("tasks", [])
    return {"ok": True, "count": len(tasks), "tasks": tasks}


@tool("vm_task_status", "查询单个传统混剪任务状态与产出统计。",
      {"task_id": _S("string", "任务 ID", True)}, group="legacy")
def vm_task_status(task_id):
    return passthrough(_b().request("GET", "tasks/" + urllib.parse.quote(task_id, safe="")))


@tool("vm_task_logs", "读取传统混剪任务日志(最近若干行)。",
      {"task_id": _S("string", "任务 ID", True)}, group="legacy")
def vm_task_logs(task_id):
    return passthrough(_b().request("GET", "tasks/" + urllib.parse.quote(task_id, safe="") + "/logs"))


@tool("vm_task_stop", "停止传统混剪任务(已产出的成片保留)。",
      {"task_id": _S("string", "任务 ID", True)}, group="legacy", destructive=True)
def vm_task_stop(task_id):
    return passthrough(_b().request("POST",
                                    "tasks/" + urllib.parse.quote(task_id, safe="") + "/stop"))


@tool("vm_task_resume", "继续未完成的传统混剪任务(只补剩余数量)。",
      {"task_id": _S("string", "任务 ID", True)}, group="legacy")
def vm_task_resume(task_id):
    return passthrough(_b().request("POST",
                                    "tasks/" + urllib.parse.quote(task_id, safe="") + "/resume"))


# ===========================================================================
# 派发入口(MCP / HTTP / CLI 共用)
# ===========================================================================

def call_tool(name, arguments):
    """按名字调用工具。arguments 为 dict。返回 dict。"""
    entry = REGISTRY.get(name)
    if entry is None:
        raise VMError("未知工具: " + str(name) + "。可用: " + ", ".join(sorted(REGISTRY)))
    args = dict(arguments or {})
    allowed = entry["schema"]["properties"]
    unknown = [k for k in args if k not in allowed]
    if unknown:
        raise VMError("工具 " + name + " 不接受参数: " + ", ".join(unknown))
    missing = [k for k in entry["schema"]["required"] if args.get(k) in (None, "")]
    if missing:
        raise VMError("工具 " + name + " 缺少必填参数: " + ", ".join(missing))
    return entry["handler"](**args)


def list_tools(group=""):
    """返回工具定义清单(MCP tools/list 与 HTTP /tools 共用)。"""
    items = []
    for name in sorted(REGISTRY):
        entry = REGISTRY[name]
        if group and entry["group"] != group:
            continue
        items.append({
            "name": entry["name"],
            "description": entry["description"],
            "inputSchema": entry["schema"],
            "group": entry["group"],
            "destructive": entry["destructive"],
        })
    return items

