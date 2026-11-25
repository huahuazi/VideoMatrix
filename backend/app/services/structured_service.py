"""结构混剪服务层。

还原自 VideoMatrix Structured 2.8.4.1 的 app/services/structured_service.py。
原始模块 120KB 字节码、63 个方法;本实现覆盖数据模型、校验规则、组合生成与导出调度。

关键规则全部来自原始字节码常量:
  * 片段 ID  ^[A-GT][1-9][0-9]*$
  * 项目/任务 ID ^[a-f0-9]{32}$(uuid4().hex)
  * 片段最短 0.04 秒,终点必须晚于起点
  * 切点状态 pending / approved / blocked;只有 approved 可参与组合
  * 公式 1-16 个 A-F 槽位,必须以 A 开头
  * 不同产品/文件夹的片段不能混合组合
  * 单计划最多 500 条
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import structured_ffmpeg as sff
from pathlib import Path

_ID = re.compile(r"^[a-f0-9]{32}$")
_CLIP_ID = re.compile(r"^[A-GT][1-9][0-9]*$")

CATEGORY_LABELS = {
    "A": "爆款开头",
    "B": "购买理由",
    "C": "卖点",
    "D": "促销",
    "E": "直播引流",
    "F": "卖点1",
    "G": "卖点2",
    "T": "待测开头",
}
CATEGORY_KEYS = tuple(CATEGORY_LABELS)

MIN_CLIP_SECONDS = 0.04
MAX_FORMULA_SLOTS = 16
MAX_PLAN_ITEMS = 500
MAX_REVIEW_NOTE = 4000
MAX_REVIEWER = 120


class StructuredNotFound(Exception):
    """资源不存在或编号非法。"""


class StructuredBusy(Exception):
    """有导出任务正在运行。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


def _valid_id(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise StructuredNotFound("无效的项目或任务编号")
    return value


def _is_absolute_path(value: str) -> bool:
    """判定是否为绝对路径。

    原版为 Windows 程序,接受盘符路径(C:/dir)与 UNC 共享(\\server\\share)。
    额外接受 POSIX 路径以便在 macOS / Linux 运行。
    不能用 ntpath.isabs 单独判断 —— 它把 "/relative/path" 也算绝对。
    """
    if not isinstance(value, str) or not value:
        return False
    import ntpath
    drive, _ = ntpath.splitdrive(value)
    if len(drive) == 2 and drive[1] == chr(58):
        return True
    if value.startswith(chr(92) * 2):
        return True
    if value.startswith(chr(47) * 2) and len(value) > 2 and value[2] not in (chr(47) + chr(92)):
        return True
    if value.startswith(chr(47)) and os.name != chr(110) + chr(116):
        return True
    return False


def _atomic_json(target: Path, value: Any) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + "." + _new_id() + ".tmp")
    handle = None
    try:
        handle = temporary.open("x", encoding="utf-8", newline="\n")
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.replace(temporary, target)
    finally:
        if handle is not None:
            handle.close()
        temporary.unlink(missing_ok=True)


def parse_formula(slots: Sequence[str]) -> List[str]:
    """校验并规范化公式槽位。"""
    if not slots or len(slots) > MAX_FORMULA_SLOTS:
        raise ValueError("公式必须由 1 至 16 个 A–F 槽位组成")
    out = [str(s).strip().upper() for s in slots]
    if any(len(s) != 1 or s not in "ABCDEF" for s in out):
        raise ValueError("公式必须由 1 至 16 个 A–F 槽位组成")
    return out


def formula_from_text(text: str) -> List[str]:
    """把 "ABCDEF" / "ACDBEF" 这类字符串拆成槽位。"""
    cleaned = re.sub(r"[\s,，\[\]\-→]", "", str(text or "")).upper()
    if not cleaned:
        raise ValueError("公式必须由 1 至 16 个 A–F 槽位组成")
    return parse_formula(list(cleaned))


def label_name(name: Any, label: str = "名称") -> str:
    if not isinstance(name, str):
        raise ValueError("请填写 1–120 字的" + label)
    text = name.strip()
    if not 1 <= len(text) <= 120:
        raise ValueError("请填写 1–120 字的" + label)
    return text


def validate_cut_review(status: str, note: str, reviewer: str) -> None:
    if status not in ("pending", "approved", "blocked"):
        raise ValueError("切点状态必须为 pending、approved 或 blocked")
    if note and len(note) > MAX_REVIEW_NOTE:
        raise ValueError("切点复核备注必须为不超过 4000 字的文字")
    if reviewer and len(reviewer) > MAX_REVIEWER:
        raise ValueError("切点复核来源必须为不超过 120 字的文字")


def validate_range(start: float, end: float, duration: Optional[float] = None) -> None:
    if not (start >= 0 and end > start):
        raise ValueError("终点必须晚于起点，且时间码必须为非负有限数")
    if end - start < MIN_CLIP_SECONDS:
        raise ValueError("片段至少需要 0.04 秒")
    if duration is not None and end > duration + 1e-6:
        raise ValueError("片段终点超出素材时长")


def require_cut_approved(clip: Dict[str, Any]) -> None:
    """只有 approved 的片段能进入组合 / 导出。"""
    status = clip.get("cut_status") or "pending"
    if status != "approved":
        label = "已阻止" if status == "blocked" else "待复核"
        raise ValueError(
            "片段 " + str(clip.get("id", "?")) + " 的切点" + label +
            "，不能组合或导出；请先复核并明确批准"
        )


def sample_combinations(
    formula: Sequence[str],
    pool: Dict[str, List[Dict[str, Any]]],
    count: int,
    rng: random.Random,
    distinct_sources: bool = False,
    manual: Optional[Sequence[Sequence[str]]] = None,
    limit: int = MAX_PLAN_ITEMS,
) -> List[List[str]]:
    """按公式从各类别池中抽组合。

    formula 里每个槽位代表一条视频中该位置的类别;A 是开头类,必须存在。
    返回每个组合的片段 ID 列表(顺序与 formula 一致)。
    """
    if not formula:
        raise ValueError("公式必须由 1 至 16 个 A–F 槽位组成")

    slots = list(formula)
    if slots[0] != "A":
        raise ValueError("普通结构组合的开头须来自 A 类")

    missing = [c for c in dict.fromkeys(slots) if not pool.get(c)]
    if missing:
        raise ValueError(
            "以下类别尚无已批准切点的片段：" +
            "、".join(missing) + "；请先复核并明确批准"
        )

    if manual:
        return _validate_manual(slots, pool, manual)

    target = max(0, min(int(count), limit))
    if target == 0:
        return []

    results: List[List[str]] = []
    seen = set()
    attempts = 0
    max_attempts = max(200, target * 40)

    while len(results) < target and attempts < max_attempts:
        attempts += 1
        picked: List[str] = []
        used_sources = set()
        ok = True

        for category in slots:
            candidates = pool[category]
            if distinct_sources:
                candidates = [c for c in candidates
                              if c.get("source_id") not in used_sources]
            if not candidates:
                ok = False
                break
            chosen = candidates[rng.randrange(len(candidates))]
            picked.append(chosen["id"])
            used_sources.add(chosen.get("source_id"))

        if not ok:
            continue
        key = tuple(picked)
        if key in seen:
            continue
        seen.add(key)
        results.append(picked)

    if not results:
        raise ValueError("未找到满足约束的组合；可增加源视频或关闭不同源约束")
    return results


def _validate_manual(slots: Sequence[str], pool: Dict[str, List[Dict[str, Any]]],
                     manual: Sequence[Sequence[str]]) -> List[List[str]]:
    if not 1 <= len(manual) <= MAX_PLAN_ITEMS:
        raise ValueError("手动数组需要 1 至 500 条组合")

    by_id: Dict[str, Dict[str, Any]] = {}
    for items in pool.values():
        for item in items:
            by_id[item["id"]] = item

    out: List[List[str]] = []
    seen = set()
    for row in manual:
        row = [str(x).strip().upper() for x in row]
        if len(row) != len(slots):
            raise ValueError("手动数组长度与公式槽位不一致")
        combined = "".join(row)
        if combined in seen:
            raise ValueError("手动数组含重复组合或不满足不同源视频约束")
        seen.add(combined)
        for position, clip_id in enumerate(row):
            clip = by_id.get(clip_id)
            if clip is None:
                raise ValueError("数组含不存在或类别与槽位不匹配的片段")
            if clip.get("category") != slots[position]:
                raise ValueError("数组含不存在或类别与槽位不匹配的片段")
        out.append(row)
    return out


class StructuredService:
    """结构化项目的持久化与业务操作。

    存储布局(全部 JSON,无数据库):
        <home>/projects/<project_id>.json
        <home>/projects/<project_id>/renders/<render_id>.json
    """

    def __init__(self, home: Optional[Path] = None) -> None:
        home = home or Path(os.environ.get(
            "VIDEOMATRIX_STRUCTURED_HOME",
            str(Path(os.environ.get("APPDATA", Path.home())) / "VideoMatrix" / "library"),
        ))
        self.home = Path(home)
        self.projects_dir = self.home / "projects"
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._processes: Dict[str, subprocess.Popen] = {}

    # ---------------- 持久化 ----------------

    def _project_path(self, pid: str) -> Path:
        return self.projects_dir / (_valid_id(pid) + ".json")

    def _load(self, pid: str) -> Dict[str, Any]:
        path = self._project_path(pid)
        if not path.is_file():
            raise StructuredNotFound("项目不存在")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StructuredNotFound("项目不存在") from exc

    def _save(self, project: Dict[str, Any]) -> None:
        project["updated_at"] = _now()
        _atomic_json(self._project_path(project["id"]), project)

    # ---------------- 项目 ----------------

    def list_projects(self) -> List[Dict[str, Any]]:
        rows = []
        for path in sorted(self.projects_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            rows.append({
                "id": data.get("id"),
                "name": data.get("name"),
                "products": len(data.get("products") or []),
                "sources": len(data.get("sources") or []),
                "clips": len(data.get("clips") or []),
                "updated_at": data.get("updated_at"),
            })
        return rows

    def create_project(self, name: str) -> Dict[str, Any]:
        project = {
            "schema_version": 1,
            "id": _new_id(),
            "name": label_name(name, "项目名称"),
            "created_at": _now(),
            "updated_at": _now(),
            "category_labels": dict(CATEGORY_LABELS),
            "products": [],
            "folders": [],
            "sources": [],
            "clips": [],
            "formulas": [],
            "plans": [],
            "renders": [],
        }
        self._save(project)
        return project

    def get_project(self, pid: str) -> Dict[str, Any]:
        return self._load(pid)

    # ---------------- 产品 / 文件夹 ----------------

    def create_product(self, pid: str, name: str) -> Dict[str, Any]:
        with self._lock:
            project = self._load(pid)
            clean = label_name(name, "产品")
            if any(p.get("name") == clean for p in project["products"]):
                raise ValueError("产品名称已存在，请选择现有产品或使用不同名称")
            product = {"id": _new_id(), "name": clean, "created_at": _now(),
                       "input_dir": "", "output_dir": ""}
            project["products"].append(product)
            self._save(project)
            return product

    def update_product(self, pid: str, product_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {"name", "input_dir", "output_dir"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError("仅允许修改产品名称、素材目录和输出目录")
        with self._lock:
            project = self._load(pid)
            product = self._product(project, product_id)
            if "name" in changes and changes["name"] is not None:
                clean = label_name(changes["name"], "产品")
                if any(p.get("name") == clean and p["id"] != product_id
                       for p in project["products"]):
                    raise ValueError("产品名称已存在")
                product["name"] = clean
            for key in ("input_dir", "output_dir"):
                if changes.get(key) is not None:
                    value = str(changes[key]).strip()
                    if value and not os.path.isabs(value):
                        raise ValueError("产品目录必须为绝对路径")
                    product[key] = value
            self._save(project)
            return product

    def create_folder(self, pid: str, name: str, product_id: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            project = self._load(pid)
            clean = label_name(name, "文件夹")
            if product_id:
                self._product(project, product_id)
                if any(f.get("name") == clean and f.get("product_id") == product_id
                       for f in project["folders"]):
                    raise ValueError("当前产品下已有同名素材文件夹")
            folder = {"id": _new_id(), "name": clean,
                      "product_id": product_id or "", "created_at": _now()}
            project["folders"].append(folder)
            self._save(project)
            return folder

    def _product(self, project: Dict[str, Any], product_id: str) -> Dict[str, Any]:
        for product in project["products"]:
            if product["id"] == product_id:
                return product
        raise StructuredNotFound("产品不存在")

    def _folder(self, project: Dict[str, Any], folder_id: str) -> Dict[str, Any]:
        for folder in project["folders"]:
            if folder["id"] == folder_id:
                return folder
        raise StructuredNotFound("文件夹不存在")

    def _folder_scope(self, project: Dict[str, Any], product_id: str,
                      folder_ids: Optional[Iterable[str]]) -> List[str]:
        """校验文件夹属于该产品,返回有效 folder id 列表。"""
        ids = [str(f) for f in (folder_ids or [])]
        if not ids:
            return []
        if len(set(ids)) != len(ids):
            raise ValueError("请选择有效且不重复的素材文件夹")
        for fid in ids:
            folder = self._folder(project, fid)
            if product_id and folder.get("product_id") != product_id:
                raise ValueError("素材文件夹不属于当前产品")
        return ids

    # ---------------- 素材 ----------------

    def add_source(self, pid: str, path: str, product_id: Optional[str] = None,
                   folder_id: Optional[str] = None) -> Dict[str, Any]:
        candidate = Path(str(path)).expanduser()
        if not candidate.is_file():
            raise ValueError("请选择本地视频文件")
        with self._lock:
            project = self._load(pid)
            resolved = str(candidate.resolve())
            for source in project["sources"]:
                if source.get("path") == resolved:
                    if (product_id and source.get("product_id") != product_id) or \
                       (folder_id and source.get("folder_id") != folder_id):
                        raise ValueError(
                            "原片已存在于其他产品或文件夹，请通过明确归属操作调整，"
                            "导入不会自动移动旧素材"
                        )
                    return source
            if product_id:
                self._product(project, product_id)
            if folder_id:
                self._folder(project, folder_id)
            source = {
                "id": _new_id(), "path": resolved, "name": candidate.name,
                "product_id": product_id or "",
                "folder_id": folder_id or "",
                "size": candidate.stat().st_size,
                "duration": None, "width": None, "height": None,
                "created_at": _now(),
            }
            project["sources"].append(source)
            self._save(project)
            return source

    def assign_sources(self, pid: str, source_ids: Sequence[str],
                       product_id: Optional[str], folder_id: Optional[str] = None) -> int:
        ids = [str(s) for s in source_ids]
        if not 1 <= len(ids) <= 5000:
            raise ValueError("请选择 1–5000 条不同原片进行归属")
        if len(set(ids)) != len(ids):
            raise ValueError("请选择 1–5000 条不同原片进行归属")
        with self._lock:
            project = self._load(pid)
            if product_id:
                self._product(project, product_id)
            if folder_id:
                self._folder(project, folder_id)
            index = {s["id"]: s for s in project["sources"]}
            missing = [i for i in ids if i not in index]
            if missing:
                raise StructuredNotFound("素材不存在")
            for sid in ids:
                index[sid]["product_id"] = product_id or ""
                index[sid]["folder_id"] = folder_id or ""
            self._save(project)
            return len(ids)

    def import_source_directory(self, pid: str, path: str,
                                product_id: Optional[str] = None,
                                folder_id: Optional[str] = None,
                                recursive: bool = False) -> Dict[str, Any]:
        directory = Path(str(path)).expanduser()
        if not directory.is_dir():
            raise ValueError("素材目录不存在或不是文件夹")
        pattern = "**/*" if recursive else "*"
        extensions = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
        imported: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []
        for item in sorted(directory.glob(pattern)):
            if not item.is_file() or item.suffix.lower() not in extensions:
                continue
            try:
                imported.append(self.add_source(pid, str(item), product_id, folder_id))
            except (ValueError, StructuredNotFound, OSError) as exc:
                errors.append({"path": str(item), "error": str(exc)})
        return {"sources": imported, "errors": errors}

    def _source(self, project: Dict[str, Any], source_id: str) -> Dict[str, Any]:
        for source in project["sources"]:
            if source["id"] == source_id:
                return source
        raise StructuredNotFound("素材不存在")

    def _media_info(self, source: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """读素材的真实时长/音轨/时间基,结果缓存在项目 JSON 里。

        首次访问会跑 ffprobe 并回写项目,后续直接命中缓存。
        """
        if not source:
            return {}
        path = Path(str(source.get("path") or ""))
        if not path.is_file():
            return {}
        cached = source.get("media_info") or {}
        if cached.get("duration") and cached.get("path_mtime") == path.stat().st_mtime:
            return cached

        info = sff.probe_media(path)
        if not info.get("duration"):
            return {}
        info["path_mtime"] = path.stat().st_mtime
        source["media_info"] = info
        # 顺手补回 duration 字段,前端列表能直接显示
        if not source.get("duration"):
            source["duration"] = info["duration"]
        return info



    # ---------------- 片段 ----------------

    def add_clip(self, pid: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        category = str(payload.get("category", "")).strip().upper()
        if category not in CATEGORY_LABELS:
            raise ValueError("片段类别必须为 A、B、C、D、E、F、G 或 T")
        source_id = str(payload.get("source_id") or "")
        start = float(payload.get("start", 0))
        end = float(payload.get("end", 0))
        validate_range(start, end)

        with self._lock:
            project = self._load(pid)
            source = self._source(project, source_id)
            media = self._media_info(source)
            if media.get("duration"):
                validate_range(start, end, float(media["duration"]))
            if not source.get("product_id"):
                raise ValueError(
                    "素材 " + source.get("name", "") + " 尚未归属产品，请先明确原片归属"
                )
            validate_cut_review(
                str(payload.get("cut_status") or "pending"),
                str(payload.get("cut_review_note") or ""),
                str(payload.get("cut_reviewer") or ""),
            )
            clip = {
                "id": self._next_clip_id(project, category),
                "category": category,
                "source_id": source_id,
                "product_id": source.get("product_id", ""),
                "folder_id": source.get("folder_id", ""),
                "start": round(start, 6),
                "end": round(end, 6),
                "duration": round(end - start, 6),
                "note": str(payload.get("note") or "")[:MAX_REVIEW_NOTE],
                "cut_status": str(payload.get("cut_status") or "pending"),
                "cut_review_note": str(payload.get("cut_review_note") or ""),
                "cut_reviewer": str(payload.get("cut_reviewer") or ""),
                "created_at": _now(),
                "updated_at": _now(),
            }
            project["clips"].append(clip)
            self._save(project)
            return clip

    def _next_clip_id(self, project: Dict[str, Any], category: str) -> str:
        used = {c["id"] for c in project["clips"] if c.get("category") == category}
        number = 1
        while (category + str(number)) in used:
            number += 1
        return category + str(number)

    def update_clip(self, pid: str, clip_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {"start", "end", "note", "cut_status", "cut_review_note", "cut_reviewer"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError("仅允许修改起止点、备注及切点复核信息，且值不能为空")
        if not _CLIP_ID.fullmatch(str(clip_id)):
            raise StructuredNotFound("片段不存在")

        with self._lock:
            project = self._load(pid)
            clip = next((c for c in project["clips"] if c["id"] == clip_id), None)
            if clip is None:
                raise StructuredNotFound("片段不存在")
            start = float(changes["start"]) if changes.get("start") is not None else clip["start"]
            end = float(changes["end"]) if changes.get("end") is not None else clip["end"]
            source = next((s for s in project["sources"] if s["id"] == clip.get("source_id")), None)
            media = self._media_info(source) if source else {}
            validate_range(start, end, float(media["duration"]) if media.get("duration") else None)
            clip["start"] = round(start, 6)
            clip["end"] = round(end, 6)
            clip["duration"] = round(end - start, 6)
            if changes.get("note") is not None:
                clip["note"] = str(changes["note"])[:MAX_REVIEW_NOTE]
            for key in ("cut_status", "cut_review_note", "cut_reviewer"):
                if changes.get(key) is not None:
                    clip[key] = str(changes[key])
            validate_cut_review(clip.get("cut_status") or "pending",
                                clip.get("cut_review_note") or "",
                                clip.get("cut_reviewer") or "")
            clip["updated_at"] = _now()
            self._save(project)
            return clip

    def delete_clip(self, pid: str, clip_id: str) -> Dict[str, Any]:
        with self._lock:
            project = self._load(pid)
            before = len(project["clips"])
            project["clips"] = [c for c in project["clips"] if c["id"] != clip_id]
            if len(project["clips"]) == before:
                raise StructuredNotFound("片段不存在")
            self._save(project)
            return {"deleted": clip_id}

    # ---------------- 公式 / 组合清单 ----------------

    def add_formula(self, pid: str, name: str, slots: Sequence[str]) -> Dict[str, Any]:
        with self._lock:
            project = self._load(pid)
            formula = {"id": _new_id(), "name": label_name(name, "公式名称"),
                       "slots": parse_formula(slots), "created_at": _now()}
            project["formulas"].append(formula)
            self._save(project)
            return formula

    def _approved_pool(self, project: Dict[str, Any], product_id: str,
                       folder_ids: Optional[Sequence[str]] = None) -> Dict[str, List[Dict[str, Any]]]:
        approved = [c for c in project["clips"]
                    if (c.get("cut_status") or "pending") == "approved"]
        scoped = [c for c in approved
                  if not product_id or c.get("product_id") == product_id]
        if folder_ids:
            allowed = set(folder_ids)
            scoped = [c for c in scoped if c.get("folder_id") in allowed]
        pool: Dict[str, List[Dict[str, Any]]] = {k: [] for k in CATEGORY_KEYS}
        for clip in scoped:
            pool.setdefault(clip["category"], []).append(clip)
        return pool

    def create_plan(self, pid: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        slots = formula_from_text(payload.get("formula", ""))
        count = int(payload.get("count") or 10)
        if not 1 <= count <= MAX_PLAN_ITEMS:
            raise ValueError("每个计划最多生成 500 条")
        seed = payload.get("seed")
        rng = random.Random(seed) if seed is not None else random.Random()

        with self._lock:
            project = self._load(pid)
            product_id = str(payload.get("product_id") or "")
            if product_id:
                self._product(project, product_id)
            folder_ids = self._folder_scope(project, product_id, payload.get("folder_ids"))
            pool = self._approved_pool(project, product_id, folder_ids)

            manual_rows = payload.get("manual")
            manual = None
            if manual_rows:
                manual = [formula_from_text(row) for row in manual_rows]

            combos = sample_combinations(
                slots, pool, count, rng,
                distinct_sources=bool(payload.get("distinct_sources")),
                manual=manual,
            )
            plan = {
                "id": _new_id(),
                "formula": "".join(slots),
                "combinations": combos,
                "product_id": product_id,
                "folder_ids": list(folder_ids),
                "seed": seed,
                "distinct_sources": bool(payload.get("distinct_sources")),
                "created_at": _now(),
            }
            project["plans"].append(plan)
            self._save(project)

            message = "已生成 " + str(len(combos)) + " 条组合"
            if len(combos) < count:
                message = (
                    "已生成 " + str(len(combos)) + " 条；请求 " + str(count) +
                    " 条，组合不足或已达到有界搜索上限"
                )
            return {
                "plan_id": plan["id"],
                "message": message,
                "combinations": [{"clip_ids": c} for c in combos],
                "count": len(combos),
                "requested": count,
            }

    # ---------------- 导出 ----------------

    def start_render(self, pid: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        plan_id = str(payload.get("plan_id") or "")
        output_dir = str(payload.get("output_dir") or "").strip()
        if not output_dir:
            raise ValueError("请选择导出目录")
        if not _is_absolute_path(output_dir):
            raise ValueError("输出目录必须为绝对路径")
        width = int(payload.get("width") or 1080)
        height = int(payload.get("height") or 1920)
        fps = int(payload.get("fps") or 30)
        if width % 2 or height % 2 or not (64 <= width <= 4096) or not (64 <= height <= 4096):
            raise ValueError("输出宽高需为 64–4096 的偶数，帧率需为 1–120")
        if not 1 <= fps <= 120:
            raise ValueError("输出宽高需为 64–4096 的偶数，帧率需为 1–120")

        with self._lock:
            if any(j.get("status") in ("queued", "running") for j in self._jobs.values()):
                raise StructuredBusy("已有结构混剪任务正在导出，请等待或停止")

            project = self._load(pid)
            plan = next((p for p in project["plans"] if p["id"] == plan_id), None)
            if plan is None:
                raise StructuredNotFound("计划不存在")
            index = {c["id"]: c for c in project["clips"]}
            for combo in plan["combinations"]:
                for clip_id in combo:
                    clip = index.get(clip_id)
                    if clip is None:
                        raise ValueError("计划引用的片段已删除，请重新生成组合")
                    require_cut_approved(clip)

            render_id = _new_id()
            job = {
                "id": render_id,
                "project_id": pid,
                "plan_id": plan_id,
                "status": "queued",
                "output_dir": output_dir,
                "width": width,
                "height": height,
                "fps": fps,
                "keep_audio": bool(payload.get("keep_audio", True)),
                "postprocess": bool(payload.get("postprocess")),
                "variant_config": payload.get("variant_config"),
                "total": len(plan["combinations"]),
                "done": 0,
                "succeeded": 0,
                "failed": 0,
                "messages": [],
                "created_at": _now(),
            }
            self._jobs[render_id] = job
            project["renders"].append({k: job[k] for k in
                                       ("id", "plan_id", "status", "created_at")})
            self._save(project)

        thread = threading.Thread(target=self._render_worker,
                                  args=(render_id, copy.deepcopy(job)),
                                  daemon=True)
        thread.start()
        return {"render": self.get_render(render_id)}

    def get_render(self, render_id: str) -> Dict[str, Any]:
        _valid_id(render_id)
        job = self._jobs.get(render_id)
        if job is None:
            raise StructuredNotFound("导出任务不存在")
        return {k: v for k, v in job.items() if k != "variant_config"}

    def stop_render(self, render_id: str) -> Dict[str, Any]:
        _valid_id(render_id)
        job = self._jobs.get(render_id)
        if job is None:
            raise StructuredNotFound("导出任务不存在")
        if job.get("status") in ("completed", "failed", "stopped"):
            return {"stopped": render_id, "already_finished": True}
        job["status"] = "stopping"
        process = self._processes.get(render_id)
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        return {"stopped": render_id}

    def stop_all(self) -> Dict[str, Any]:
        count = 0
        for render_id, job in list(self._jobs.items()):
            if job.get("status") in ("queued", "running"):
                self.stop_render(render_id)
                count += 1
        return {"stopped": count}

    def _render_worker(self, render_id: str, job: Dict[str, Any]) -> None:
        """导出调度:逐条拼接 → 校验 → 汇总。

        每条成片一次 FFmpeg 调用,单条失败不中断整批。
        取消通过 is_cancelled 回调传给 run_process,会 terminate/kill 子进程。
        """
        live = self._jobs.get(render_id)
        if live is None:
            return
        live["status"] = "running"

        def cancelled() -> bool:
            return live.get("status") in ("stopping", "stopped")

        def bind(process):
            if process is None:
                self._processes.pop(render_id, None)
            else:
                self._processes[render_id] = process

        try:
            project = self._load(job["project_id"])
            plan = next((p for p in project["plans"] if p["id"] == job["plan_id"]), None)
            if plan is None:
                live["status"] = "failed"
                live["messages"].append("计划不存在")
                return

            index = {c["id"]: c for c in project["clips"]}
            sources = {s["id"]: s for s in project["sources"]}
            width, height, fps = job["width"], job["height"], job["fps"]
            keep_audio = bool(job.get("keep_audio", True))

            output_dir = Path(job["output_dir"])
            output_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%m%d")

            for position, combo in enumerate(plan["combinations"]):
                if cancelled():
                    break

                segments: List[Dict[str, Any]] = []
                expected = 0.0
                missing = []
                for clip_id in combo:
                    clip = index.get(clip_id)
                    if clip is None:
                        missing.append(clip_id)
                        continue
                    source = sources.get(clip.get("source_id"))
                    if source is None:
                        missing.append(clip_id)
                        continue
                    info = self._media_info(source)
                    segments.append({
                        "path": source["path"],
                        "start": float(clip["start"]),
                        "end": float(clip["end"]),
                        "has_audio": bool(info.get("has_audio", False)),
                        "time_base": info.get("time_base"),
                    })
                    expected += max(0.0, float(clip["end"]) - float(clip["start"]))

                live["messages"].append(
                    "正在导出 %d/%d" % (position + 1, live["total"]))

                if missing:
                    live["failed"] += 1
                    live["messages"].append(
                        "第 %d 条：片段已失效 %s" % (position + 1, ", ".join(missing)))
                    live["done"] = position + 1
                    continue

                target = output_dir / ("混剪-%s-%d.mp4" % (stamp, position + 1))
                if target.exists():
                    live["failed"] += 1
                    live["messages"].append("第 %d 条：目标文件已存在，拒绝覆盖" % (position + 1))
                    live["done"] = position + 1
                    continue

                result = sff.render_concat(
                    segments, target, width, height, fps,
                    keep_audio=keep_audio,
                    is_cancelled=cancelled,
                    on_process=bind,
                    timeout=7200,
                )

                if cancelled():
                    # FFmpeg 被 terminate 后会留下半截文件,不能算成片
                    target.unlink(missing_ok=True)
                    live["messages"].append("第 %d 条已取消，未完成文件已删除" % (position + 1))
                    break

                if not result["ok"]:
                    live["failed"] += 1
                    live["messages"].append(
                        "第 %d 条：%s" % (position + 1, result.get("error") or "FFmpeg 执行失败"))
                    live["done"] = position + 1
                    continue

                check = sff.verify_output(target, expected, width, height, fps)
                if not check["ok"]:
                    live["failed"] += 1
                    target.unlink(missing_ok=True)
                    live["messages"].append(
                        "第 %d 条：导出校验失败 —— %s" % (position + 1, check["reason"]))
                    live["done"] = position + 1
                    continue

                live["succeeded"] += 1
                live["done"] = position + 1
                live["messages"].append(
                    "第 %d 条完成：%s（%.2f 秒）" % (position + 1, target.name, check["duration"]))

            if cancelled():
                live["status"] = "stopped"
                live["messages"].append(
                    "已停止，保留 %d 条成片" % live["succeeded"])
            else:
                live["status"] = "completed"
                live["messages"].append(
                    "导出结束：成功 %d 条，失败 %d 条" % (live["succeeded"], live["failed"]))
        except Exception as exc:
            live["status"] = "failed"
            live["messages"].append("导出中断，请查看错误: %s" % exc)
        finally:
            self._processes.pop(render_id, None)



_SERVICE: Optional[StructuredService] = None


def get_service() -> StructuredService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = StructuredService()
    return _SERVICE

