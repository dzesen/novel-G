"""伏笔线索 API。

AI 的唯一写入口仍是 accept 章细纲（2a 设计 §3.2 —— 细纲提议 new_threads、
accept 时创建并回填 id）。本路由额外提供**人类管理界面**的显式写入面
（新建 / 编辑 / 软删），二者互不冲突：前者是自动化装配的一部分，后者是人对
记忆层的直接纠正与补充。手动新建的伏笔标 source="manual"，供孤儿审计区分。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.plot_thread_repository import plot_thread_repo
from backend.db.utils import to_object_id
from backend.services.novel.plot_thread_service import audit_thread_references


router = APIRouter(prefix="/api/plot-threads", tags=["plot-threads"])


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
    notes: str = ""


class PlotThreadUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    importance: Optional[str] = None
    planted_chapter_order: Optional[int] = None
    due_chapter_order: Optional[int] = None
    resolved_chapter_order: Optional[int] = None
    notes: Optional[str] = None


def _serialize_thread(thread: dict) -> dict:
    """ObjectId→str；补 source 缺省（本字段出现前的历史伏笔一律读作 outline）。"""
    result = dict(thread)
    for key in ("_id", "novel_id"):
        if key in result:
            result[key] = str(result[key])
    result.setdefault("source", "outline")
    return result


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (InvalidIdError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


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
            for item in serialized:
                item["referenced_by_chapter_orders"] = audit.get(item["_id"], [])
        return {"data": serialized}
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/novel/{novel_id}")
async def create_thread(novel_id: str, req: PlotThreadCreateRequest):
    try:
        data = req.model_dump(exclude_none=True)
        data["source"] = "manual"
        thread_id = await plot_thread_repo.create_thread(novel_id, data)
        thread = await plot_thread_repo.find_one(
            {"_id": to_object_id(thread_id), "novel_id": to_object_id(novel_id)}
        )
        return _serialize_thread(thread)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.put("/novel/{novel_id}/{thread_id}")
async def update_thread(novel_id: str, thread_id: str, req: PlotThreadUpdateRequest):
    try:
        await plot_thread_repo.update_thread(
            novel_id, thread_id, req.model_dump(exclude_unset=True)
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
        return {"success": await plot_thread_repo.soft_delete_thread(novel_id, thread_id)}
    except Exception as exc:
        raise _translate_error(exc) from exc
