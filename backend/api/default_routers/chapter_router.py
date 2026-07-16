"""章节 REST API。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.db.errors import DuplicateKeyError, InvalidIdError, NotFoundError
from backend.services.novel.chapter_service import ChapterService


router = APIRouter(prefix="/api/chapters", tags=["chapters"])


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


def _serialize(chapter: dict) -> dict:
    result = dict(chapter)
    for field in ("_id", "novel_id", "volume_id", "deleted_with_volume_id"):
        if result.get(field) is not None:
            result[field] = str(result[field])
    return result


def _handle_client_error(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, DuplicateKeyError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.post("/create")
async def create_chapter(req: CreateChapterRequest):
    try:
        chapter_id = await ChapterService.create_chapter(req.model_dump())
        return {"id": chapter_id, "message": "Chapter created"}
    except (NotFoundError, DuplicateKeyError, InvalidIdError, ValueError) as exc:
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
