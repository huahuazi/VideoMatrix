"""结构混剪路由。

24 条路由,路径与方法逐条还原自 2.8.4.1 的 app/api/structured_routes.py 字节码。
挂载方式: app.include_router(router, prefix="/api"),模块内 prefix="/structured"。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

from ..services import structured_service as svc
from ..models.structured import (
    ClipCreate,
    ClipUpdate,
    FolderCreate,
    FormulaCreate,
    HookTestPlanCreate,
    PlanCreate,
    ProductCreate,
    ProductUpdate,
    ProjectCreate,
    RenderCreate,
    ShotTestPlanCreate,
    SourceAssign,
    SourceCreate,
    SourceDirectoryImport,
)

router = APIRouter(prefix="/structured", tags=["structured"])


def _call(fn, *args, **kwargs):
    """把服务层异常映射成 HTTP 状态码。"""
    try:
        return fn(*args, **kwargs)
    except svc.StructuredNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except svc.StructuredBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/projects")
def list_projects() -> Dict[str, Any]:
    return {"projects": _call(svc.get_service().list_projects)}


@router.post("/projects")
def create_project(payload: ProjectCreate) -> Dict[str, Any]:
    return {"project": _call(svc.get_service().create_project, payload.name)}


@router.get("/projects/{pid}")
def get_project(pid: str) -> Dict[str, Any]:
    return {"project": _call(svc.get_service().get_project, pid)}


@router.get("/projects/{pid}/category-labels")
def get_category_labels(pid: str) -> Dict[str, Any]:
    project = _call(svc.get_service().get_project, pid)
    return {"labels": project.get("category_labels") or svc.CATEGORY_LABELS}


@router.patch("/projects/{pid}/category-labels")
def update_category_labels(pid: str, payload: Dict[str, str]) -> Dict[str, Any]:
    service = svc.get_service()
    project = _call(service.get_project, pid)
    labels = dict(project.get("category_labels") or svc.CATEGORY_LABELS)
    for key, value in payload.items():
        category = str(key).strip().upper()
        if category not in svc.CATEGORY_LABELS:
            raise HTTPException(status_code=422, detail="仅可修改 A–G 与 T 的类别名称")
        try:
            labels[category] = svc.label_name(value, "类别")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    project["category_labels"] = labels
    _call(service._save, project)
    return {"labels": labels}


@router.post("/projects/{pid}/products")
def create_product(pid: str, payload: ProductCreate) -> Dict[str, Any]:
    return {"product": _call(svc.get_service().create_product, pid, payload.name)}


@router.patch("/projects/{pid}/products/{product_id}")
def update_product(pid: str, product_id: str,
                   payload: ProductUpdate) -> Dict[str, Any]:
    changes = payload.model_dump(exclude_unset=True)
    return {"product": _call(svc.get_service().update_product, pid, product_id, changes)}


@router.post("/projects/{pid}/folders")
def create_folder(pid: str, payload: FolderCreate) -> Dict[str, Any]:
    return {"folder": _call(svc.get_service().create_folder,
                            pid, payload.name, payload.product_id)}


@router.post("/projects/{pid}/sources")
def add_source(pid: str, payload: SourceCreate) -> Dict[str, Any]:
    return {"source": _call(svc.get_service().add_source,
                            pid, payload.path, payload.product_id, payload.folder_id)}


@router.post("/projects/{pid}/sources/assign")
def assign_sources(pid: str, payload: SourceAssign) -> Dict[str, Any]:
    count = _call(svc.get_service().assign_sources,
                  pid, payload.source_ids, payload.product_id, payload.folder_id)
    return {"ok": True, "assigned": count}


@router.post("/projects/{pid}/sources/import-directory")
def import_source_directory(pid: str,
                            payload: SourceDirectoryImport) -> Dict[str, Any]:
    return _call(svc.get_service().import_source_directory,
                 pid, payload.path, payload.product_id, payload.folder_id, payload.recursive)


@router.post("/projects/{pid}/clips")
def add_clip(pid: str, payload: ClipCreate) -> Dict[str, Any]:
    data = payload.model_dump()
    return {"clip": _call(svc.get_service().add_clip, pid, data)}


@router.patch("/projects/{pid}/clips/{cid}")
def update_clip(pid: str, cid: str, payload: ClipUpdate) -> Dict[str, Any]:
    changes = payload.model_dump(exclude_unset=True)
    return {"clip": _call(svc.get_service().update_clip, pid, cid, changes)}


@router.delete("/projects/{pid}/clips/{cid}")
def delete_clip(pid: str, cid: str) -> Dict[str, Any]:
    return _call(svc.get_service().delete_clip, pid, cid)


@router.post("/projects/{pid}/formulas")
def add_formula(pid: str, payload: FormulaCreate) -> Dict[str, Any]:
    return {"formula": _call(svc.get_service().add_formula,
                             pid, payload.name, payload.slots)}


@router.post("/projects/{pid}/plans")
def create_plan(pid: str, payload: PlanCreate) -> Dict[str, Any]:
    return _call(svc.get_service().create_plan, pid, payload.model_dump())


@router.post("/projects/{pid}/hook-tests")
def create_hook_test_plan(pid: str,
                          payload: HookTestPlanCreate) -> Dict[str, Any]:
    data = payload.model_dump()
    data["formula"] = "ABCDEF"
    data["count"] = len(payload.hook_ids) * 2
    return _call(svc.get_service().create_plan, pid, data)


@router.post("/projects/{pid}/shot-tests")
def create_shot_test_plan(pid: str,
                          payload: ShotTestPlanCreate) -> Dict[str, Any]:
    data = payload.model_dump()
    data["count"] = len(payload.hook_ids) * max(1, payload.per_hook_count)
    return _call(svc.get_service().create_plan, pid, data)


@router.post("/projects/{pid}/renders")
def start_project_render(pid: str, payload: RenderCreate) -> Dict[str, Any]:
    data = payload.model_dump()
    return _call(svc.get_service().start_render, pid, data)


@router.get("/renders/{rid}")
def get_render(rid: str) -> Dict[str, Any]:
    return {"render": _call(svc.get_service().get_render, rid)}


@router.post("/renders/{rid}/stop")
def stop_render(rid: str) -> Dict[str, Any]:
    return _call(svc.get_service().stop_render, rid)


@router.post("/renders/stop-all")
def stop_all_renders() -> Dict[str, Any]:
    return _call(svc.get_service().stop_all)

