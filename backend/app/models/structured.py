"""结构混剪数据模型。

还原自 VideoMatrix Structured 2.8.4.1 的 app/models/structured.py(PyInstaller 打包,
Python 3.14 字节码反汇编还原)。字段名、类型、约束、默认值均来自原始字节码常量。
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class StrictRequest(BaseModel):
    """所有结构化请求的基类:禁止未知字段。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ProjectCreate(StrictRequest):
    name: str = Field(min_length=1, max_length=120)


class ProductCreate(StrictRequest):
    name: str = Field(min_length=1, max_length=120)


class ProductUpdate(StrictRequest):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    input_dir: Optional[str] = Field(default=None, max_length=32768)
    output_dir: Optional[str] = Field(default=None, max_length=32768)


class FolderCreate(StrictRequest):
    product_id: Optional[str] = None
    name: str = Field(min_length=1, max_length=120)


class SourceCreate(StrictRequest):
    path: str = Field(min_length=1, max_length=32768)
    product_id: Optional[str] = None
    folder_id: Optional[str] = None


class SourceAssign(StrictRequest):
    source_ids: List[str] = Field(min_length=1, max_length=5000)
    product_id: Optional[str] = None
    folder_id: Optional[str] = None


class SourceDirectoryImport(StrictRequest):
    path: str = Field(min_length=1, max_length=32768)
    product_id: Optional[str] = None
    folder_id: Optional[str] = None
    recursive: bool = False


class ClipCreate(StrictRequest):
    source_id: str
    category: str = Field(pattern=r"^[A-GT]$")
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    note: str = Field(default="", max_length=4000)
    cut_status: Literal["pending", "approved", "blocked"] = "pending"
    cut_review_note: str = ""
    cut_reviewer: str = ""


class ClipUpdate(StrictRequest):
    start: Optional[float] = Field(default=None, ge=0)
    end: Optional[float] = Field(default=None, gt=0)
    note: Optional[str] = Field(default=None, max_length=4000)
    cut_status: Optional[Literal["pending", "approved", "blocked"]] = None
    cut_review_note: Optional[str] = None
    cut_reviewer: Optional[str] = None


class FormulaCreate(StrictRequest):
    name: str = Field(min_length=1, max_length=120)
    slots: List[str] = Field(min_length=1, max_length=16)


class PlanCreate(StrictRequest):
    formula: str = Field(min_length=1, max_length=16)
    count: int = Field(default=10, ge=1, le=500)
    seed: Optional[int] = None
    distinct_sources: bool = False
    manual: Optional[List[str]] = Field(default=None, min_length=1, max_length=500)
    product_id: Optional[str] = None
    folder_ids: Optional[List[str]] = Field(default=None, max_length=5000)
    per_hook_count: Optional[int] = None
    hook_ids: Optional[List[str]] = Field(default=None, max_length=5000)


class HookTestPlanCreate(StrictRequest):
    base_plan_id: str
    hook_ids: List[str] = Field(min_length=1, max_length=500)
    product_id: Optional[str] = None


class ShotTestPlanCreate(StrictRequest):
    product_id: str
    formula: str = Field(min_length=1, max_length=16)
    hook_ids: List[str] = Field(min_length=1, max_length=500)
    per_hook_count: int = 2
    folder_ids: Optional[List[str]] = Field(default=None, max_length=5000)
    seed: Optional[int] = None
    distinct_sources: bool = False


class StructuredVariantConfig(StrictRequest):
    variant_strength: Literal["mild", "balanced", "strong"] = "balanced"
    variant_hook: bool = True
    variant_body: bool = True
    variant_mirror: bool = False
    variant_crop: bool = True
    variant_color: bool = False
    variant_blend_enabled: bool = False
    variant_blend_path: str = ""
    variant_blend_opacity: float = Field(default=0.03, ge=0.01, le=0.15)
    variant_blend_eof: Literal["loop", "freeze", "stop"] = "loop"
    variant_protected_regions: List[Dict[str, Any]] = Field(default_factory=list)
    variant_seed: Optional[int] = None


class RenderCreate(StrictRequest):
    plan_id: str
    output_dir: str = Field(min_length=1, max_length=32768)
    width: int = Field(default=1080, ge=64, le=4096)
    height: int = Field(default=1920, ge=64, le=4096)
    fps: int = Field(default=30, ge=1, le=120)
    keep_audio: bool = True
    postprocess: bool = False
    variant_config: Optional[StructuredVariantConfig] = None


class PreviewRange(StrictRequest):
    source_id: str
    start: float = Field(ge=0)
    end: float = Field(gt=0)


class PreviewCreate(StrictRequest):
    clip_ids: Optional[List[str]] = Field(default=None, min_length=1, max_length=16)
    draft: Optional[Dict[str, Any]] = None
    width: int = Field(default=360, ge=64, le=1280)
    height: int = Field(default=640, ge=64, le=1280)
    fps: int = Field(default=30, ge=1, le=120)
    keep_audio: bool = True

