"""伏笔线索 API。

AI 的唯一写入口仍是 accept 章细纲（2a 设计 §3.2 —— 细纲提议 new_threads、
accept 时创建并回填 id）。本路由额外提供**人类管理界面**的显式写入面
（新建 / 编辑 / 软删），二者互不冲突：前者是自动化装配的一部分，后者是人对
记忆层的直接纠正与补充。手动新建的伏笔标 source="manual"，供孤儿审计区分。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.plot_thread_repository import plot_thread_repo
from backend.db.utils import to_object_id
from backend.api.default_routers.auth_router import require_owned_path_resource
from backend.services.novel.plot_thread_service import (
    PlotThreadService,
    audit_thread_reference_chapters,
    audit_thread_references,
)
from backend.services.novel.chapter_timeline import validate_chapter_reference
from backend.services.novel.chapter_timeline import ChapterTimeline
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.volume_repository import volume_repo


router = APIRouter(
    prefix="/api/plot-threads",
    tags=["plot-threads"],
    dependencies=[Depends(require_owned_path_resource)],
)


class PlotThreadCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""
    # status/importance 用 Optional[str] 而非 Literal：非法值要落到仓储 ValueError→400，
    # 而不是 FastAPI 的 422（与 reference_card_router 同一手法）。
    status: Optional[str] = None
    importance: Optional[str] = None
    planted_chapter_order: Optional[int] = None
    due_chapter_order: Optional[int] = None
    resolved_chapter_order: Optional[int] = None
    planted_chapter_id: Optional[str] = None
    due_target: Optional[dict] = None
    resolved_chapter_id: Optional[str] = None
    notes: str = ""


class PlotThreadUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    importance: Optional[str] = None
    planted_chapter_order: Optional[int] = None
    due_chapter_order: Optional[int] = None
    resolved_chapter_order: Optional[int] = None
    planted_chapter_id: Optional[str] = None
    due_target: Optional[dict] = None
    resolved_chapter_id: Optional[str] = None
    notes: Optional[str] = None


def _serialize_thread(thread: dict) -> dict:
    """ObjectId→str；补 source 缺省（本字段出现前的历史伏笔一律读作 outline）。"""
    result = dict(thread)
    for key in (
        "_id",
        "novel_id",
        "planted_chapter_id",
        "resolved_chapter_id",
    ):
        if key in result:
            result[key] = str(result[key]) if result[key] is not None else None
    due_target = result.get("due_target")
    if isinstance(due_target, dict) and due_target.get("chapter_id") is not None:
        result["due_target"] = {
            **due_target,
            "chapter_id": str(due_target["chapter_id"]),
        }
    result.setdefault("source", "outline")
    return result


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (InvalidIdError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


async def _normalize_chapter_references(novel_id: str, data: dict) -> dict:
    prepared = dict(data)
    for id_field, snapshot_field in (
        ("planted_chapter_id", "planted_chapter_order"),
        ("resolved_chapter_id", "resolved_chapter_order"),
    ):
        chapter_id = prepared.get(id_field)
        if chapter_id:
            chapter = await validate_chapter_reference(novel_id, chapter_id)
            prepared[snapshot_field] = int(chapter.get("order_index") or 0)
        elif prepared.get(snapshot_field) is not None:
            raise ValueError(
                f"{id_field} is required when {snapshot_field} is supplied; "
                "numeric chapter order is read-only compatibility data"
            )
    due_target = prepared.get("due_target")
    if isinstance(due_target, dict) and due_target.get("kind") == "chapter":
        await validate_chapter_reference(novel_id, str(due_target.get("chapter_id") or ""))
    if "due_target" not in prepared and prepared.get("due_chapter_order") is not None:
        prepared["due_target"] = {
            "kind": "planned_ordinal",
            "ordinal": prepared["due_chapter_order"],
        }
    return prepared


async def _effective_chapter_id(novel_id: str, data: dict) -> str | None:
    for field in ("resolved_chapter_id", "planted_chapter_id"):
        if data.get(field):
            return str(data[field])
    due = data.get("due_target")
    if isinstance(due, dict) and due.get("kind") == "chapter" and due.get("chapter_id"):
        return str(due["chapter_id"])
    chapters = await chapter_repo.get_chapters_by_novel(novel_id)
    volumes = await volume_repo.get_volumes_by_novel(novel_id)
    timeline = ChapterTimeline(volumes, chapters)
    return timeline.positions[-1].chapter_id if timeline.positions else None


@router.get("/novel/{novel_id}")
async def list_threads(novel_id: str, with_reference_audit: bool = False):
    """列出小说下全部未删除伏笔。with_reference_audit=true 时每条附
    referenced_by_chapter_orders（空 = 无任何章节细纲引用它）。默认关闭，
    现有 roster 消费方零影响。"""
    try:
        threads = await plot_thread_repo.list_threads(novel_id)
        serialized = [_serialize_thread(t) for t in threads]
        if with_reference_audit:
            audit = await audit_thread_references(novel_id)
            chapter_audit = await audit_thread_reference_chapters(novel_id)
            for item in serialized:
                item["referenced_by_chapter_orders"] = audit.get(item["_id"], [])
                item["referenced_by_chapters"] = chapter_audit.get(item["_id"], [])
        return {"data": serialized}
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/novel/{novel_id}")
async def create_thread(novel_id: str, req: PlotThreadCreateRequest):
    try:
        data = await _normalize_chapter_references(
            novel_id, req.model_dump(exclude_none=True)
        )
        data["source"] = "manual"
        effective = await _effective_chapter_id(novel_id, data)
        if effective is None:
            raise ValueError("A plot thread requires an active planted chapter")
        if not data.get("planted_chapter_id"):
            chapter = await validate_chapter_reference(novel_id, effective)
            data["planted_chapter_id"] = effective
            data["planted_chapter_order"] = int(chapter.get("order_index") or 0)
        if data.get("status") == "resolved" and not data.get("resolved_chapter_id"):
            raise ValueError("resolved_chapter_id is required when status is resolved")
        thread_id = await PlotThreadService.create_thread(
            novel_id, data, effective_chapter_id=effective
        )
        thread = await plot_thread_repo.find_one(
            {"_id": to_object_id(thread_id), "novel_id": to_object_id(novel_id)}
        )
        return _serialize_thread(thread)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.put("/novel/{novel_id}/{thread_id}")
async def update_thread(novel_id: str, thread_id: str, req: PlotThreadUpdateRequest):
    try:
        data = await _normalize_chapter_references(
            novel_id, req.model_dump(exclude_unset=True)
        )
        if data.get("status") == "resolved" and not data.get("resolved_chapter_id"):
            raise ValueError("resolved_chapter_id is required when status is resolved")
        effective = await _effective_chapter_id(novel_id, data)
        await PlotThreadService.update_thread(
            novel_id, thread_id, data, effective_chapter_id=effective
        )
        thread = await plot_thread_repo.find_one(
            {"_id": to_object_id(thread_id), "novel_id": to_object_id(novel_id)}
        )
        return _serialize_thread(thread)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.delete("/novel/{novel_id}/{thread_id}")
async def delete_thread(novel_id: str, thread_id: str):
    try:
        current = await plot_thread_repo.get_thread(novel_id, thread_id)
        effective = await _effective_chapter_id(novel_id, current)
        success = await PlotThreadService.soft_delete_thread(
            novel_id, thread_id, effective_chapter_id=effective
        )
        return {"success": success}
    except Exception as exc:
        raise _translate_error(exc) from exc
