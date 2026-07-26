"""章节 REST API。"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.db.errors import DuplicateKeyError, InvalidIdError, NotFoundError
from backend.services.novel.chapter_service import ChapterService
from backend.api.default_routers.auth_router import require_owned_path_resource
from backend.services.auth.identity_service import Actor
from backend.services.auth.novel_access_service import (
    NovelAccessService,
    get_novel_access_service,
)


router = APIRouter(
    prefix="/api/chapters",
    tags=["chapters"],
    dependencies=[Depends(require_owned_path_resource)],
)


class CreateChapterRequest(BaseModel):
    novel_id: str
    volume_id: str
    title: str = Field(min_length=1, max_length=200)
    summary: str = ""
    content: str = ""
    status: str = "draft"
    order_index: Optional[int] = Field(default=None, ge=1)


class UpdateChapterRequest(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    summary: Optional[str] = None
    content: Optional[str] = None
    status: Optional[str] = None
    order_index: Optional[int] = Field(default=None, ge=1)


class AcceptChapterOutlineRequest(BaseModel):
    outline: dict
    edited_by_human: bool = False


class UpdateChapterOutlineRequest(BaseModel):
    outline: dict


class BulkDeleteChaptersRequest(BaseModel):
    novel_id: str
    chapter_ids: List[str] = Field(min_length=1, max_length=100)


_OUTLINE_ID_FIELDS = (
    "pov_character_card_id",
    "present_character_card_ids",
    "mentioned_character_card_ids",
    "referenced_worldbook_card_ids",
    "threads_planted",
    "threads_resolved",
)


def _serialize_outline(outline: dict) -> dict:
    """将 outline 子文档里的 ObjectId 字段（单值或列表）转为字符串。"""
    result = dict(outline)
    for field in _OUTLINE_ID_FIELDS:
        value = result.get(field)
        if value is None:
            continue
        if isinstance(value, list):
            result[field] = [str(item) for item in value]
        else:
            result[field] = str(value)
    return result


def _serialize(chapter: dict) -> dict:
    result = dict(chapter)
    for field in ("_id", "novel_id", "volume_id", "deleted_with_volume_id"):
        if result.get(field) is not None:
            result[field] = str(result[field])
    if result.get("outline") is not None:
        result["outline"] = _serialize_outline(result["outline"])
    return result


def _handle_client_error(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, DuplicateKeyError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.post("/create")
async def create_chapter(
    req: CreateChapterRequest,
    actor: Actor = Depends(require_owned_path_resource),
    access: NovelAccessService = Depends(get_novel_access_service),
):
    try:
        await access.require_owned_novel(actor, req.novel_id)
        chapter_id = await ChapterService.create_chapter(req.model_dump())
        return {"id": chapter_id, "message": "Chapter created"}
    except (NotFoundError, DuplicateKeyError, InvalidIdError, ValueError) as exc:
        raise _handle_client_error(exc) from exc


@router.post("/{chapter_id}/accept-outline")
async def accept_chapter_outline(chapter_id: str, req: AcceptChapterOutlineRequest):
    """接受章节细纲预览：创建 new_threads 并写入 chapter.outline（设计 §5.2）。

    允许重复接受、直接覆盖：chapter.outline 是单文档字段，没有卷链 §7.4 那种
    "拼接两套方案"的风险。上次接受创建的伏笔不会被自动清理，以
    previous_thread_ids 原样返回。
    """
    try:
        return await ChapterService.accept_chapter_outline(
            chapter_id,
            req.outline,
            edited_by_human=req.edited_by_human,
        )
    except (NotFoundError, DuplicateKeyError, InvalidIdError, ValueError) as exc:
        raise _handle_client_error(exc) from exc


@router.put("/{chapter_id}/outline")
async def update_chapter_outline(chapter_id: str, req: UpdateChapterOutlineRequest):
    """编辑已存章节细纲的作者字段（设计 §5）。

    threads_planted 与 generated_at 由服务端从现有 outline 保留，edited_by_human
    强制 true。不创建/删除任何伏笔。请求体不含 edited_by_human（服务端固定 true）。
    """
    try:
        chapter = await ChapterService.update_chapter_outline(chapter_id, req.outline)
        return _serialize(chapter)
    except (NotFoundError, InvalidIdError, ValueError) as exc:
        raise _handle_client_error(exc) from exc


@router.get("/novel/{novel_id}")
async def list_chapters_by_novel(novel_id: str):
    try:
        chapters = await ChapterService.get_chapters_by_novel(novel_id)
        return {"data": [_serialize(chapter) for chapter in chapters]}
    except (NotFoundError, InvalidIdError) as exc:
        raise _handle_client_error(exc) from exc


@router.get("/novel/{novel_id}/trash")
async def list_deleted_chapters(novel_id: str):
    try:
        chapters = await ChapterService.get_deleted_chapters(novel_id)
        return {"data": [_serialize(chapter) for chapter in chapters]}
    except (NotFoundError, InvalidIdError) as exc:
        raise _handle_client_error(exc) from exc


@router.get("/volume/{volume_id}")
async def list_chapters_by_volume(volume_id: str):
    try:
        chapters = await ChapterService.get_chapters_by_volume(volume_id)
        return {"data": [_serialize(chapter) for chapter in chapters]}
    except (NotFoundError, InvalidIdError) as exc:
        raise _handle_client_error(exc) from exc


@router.get("/{chapter_id}")
async def get_chapter(chapter_id: str):
    try:
        return _serialize(await ChapterService.get_chapter(chapter_id))
    except (NotFoundError, InvalidIdError) as exc:
        raise _handle_client_error(exc) from exc


@router.put("/{chapter_id}")
async def update_chapter(chapter_id: str, req: UpdateChapterRequest):
    try:
        success = await ChapterService.update_chapter(
            chapter_id,
            req.model_dump(exclude_unset=True),
        )
        return {"success": success}
    except (NotFoundError, DuplicateKeyError, InvalidIdError, ValueError) as exc:
        raise _handle_client_error(exc) from exc


@router.post("/bulk-delete")
async def bulk_soft_delete_chapters(
    req: BulkDeleteChaptersRequest,
    actor: Actor = Depends(require_owned_path_resource),
    access: NovelAccessService = Depends(get_novel_access_service),
):
    """批量把至多 100 个章节移入回收站，返回逐项结果。"""
    try:
        await access.require_owned_novel(actor, req.novel_id)
        return await ChapterService.soft_delete_chapters(
            req.novel_id,
            req.chapter_ids,
        )
    except (NotFoundError, InvalidIdError, ValueError) as exc:
        raise _handle_client_error(exc) from exc


@router.delete("/{chapter_id}")
async def soft_delete_chapter(chapter_id: str):
    try:
        return {"success": await ChapterService.soft_delete_chapter(chapter_id)}
    except (NotFoundError, InvalidIdError, ValueError) as exc:
        raise _handle_client_error(exc) from exc


@router.post("/{chapter_id}/restore")
async def restore_chapter(chapter_id: str):
    try:
        return {"success": await ChapterService.restore_chapter(chapter_id)}
    except (NotFoundError, DuplicateKeyError, InvalidIdError, ValueError) as exc:
        raise _handle_client_error(exc) from exc


@router.delete("/{chapter_id}/hard")
async def hard_delete_chapter(chapter_id: str):
    try:
        return {"success": await ChapterService.hard_delete_chapter(chapter_id)}
    except (NotFoundError, InvalidIdError, ValueError) as exc:
        raise _handle_client_error(exc) from exc
