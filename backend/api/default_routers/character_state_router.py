"""人物记忆（character_states）管理 API。

该集合此前只被 accept_chapter_state 服务写过、无任何读端点。本路由提供人工
管理面：编辑 current_state、删/改单条 permanent_facts。人只能纠正（删/改），
不能凭空新增 fact；current_state 编辑要求文档已存在（不凭空创建）。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.character_state_repository import character_state_repo
from backend.services.novel.chapter_timeline import validate_chapter_reference
from backend.services.novel.character_state_service import CharacterStateService
from backend.services.novel.state_timeline import latest_chapter_id


router = APIRouter(prefix="/api/character-states", tags=["character-states"])


class CurrentStateUpdateRequest(BaseModel):
    current_state: str
    as_of_chapter_order: int
    as_of_chapter_id: Optional[str] = None


class PermanentFactUpdateRequest(BaseModel):
    # kind 用 Optional[str] 而非 Literal：非法值落到仓储 ValueError→400，非 422。
    fact: Optional[str] = None
    kind: Optional[str] = None
    chapter_order: Optional[int] = None
    source_chapter_id: Optional[str] = None


def _serialize_state(state: dict) -> dict:
    """顶层 + 每条 fact 的 ObjectId 转字符串。datetime 由 FastAPI 自行编码。"""
    result = dict(state)
    for key in ("_id", "novel_id", "card_id"):
        if key in result:
            result[key] = str(result[key])
    facts = []
    if result.get("as_of_chapter_id") is not None:
        result["as_of_chapter_id"] = str(result["as_of_chapter_id"])
    for fact in result.get("permanent_facts") or []:
        item = dict(fact)
        if "id" in item:
            item["id"] = str(item["id"])
        if item.get("source_chapter_id") is not None:
            item["source_chapter_id"] = str(item["source_chapter_id"])
        facts.append(item)
    result["permanent_facts"] = facts
    return result


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (InvalidIdError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@router.get("/novel/{novel_id}")
async def list_states(novel_id: str):
    """列出小说下全部人物状态（返回前幂等回填历史缺失的 fact id）。"""
    try:
        await character_state_repo.ensure_fact_ids(novel_id)
        states = await character_state_repo.list_states(novel_id)
        return {"data": [_serialize_state(state) for state in states]}
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.put("/novel/{novel_id}/card/{card_id}/current-state")
async def edit_current_state(novel_id: str, card_id: str, req: CurrentStateUpdateRequest):
    try:
        chapter_order = req.as_of_chapter_order
        if req.as_of_chapter_id:
            chapter = await validate_chapter_reference(novel_id, req.as_of_chapter_id)
            chapter_order = int(chapter.get("order_index") or 0)
        if req.as_of_chapter_id:
            state = await CharacterStateService.update_current_state(
                novel_id, card_id, req.current_state, req.as_of_chapter_id, chapter_order
            )
        else:
            # 仅保留给旧客户端的裸章号兼容入口；当前 UI 总是提交稳定章节 ID。
            state = await CharacterStateService.update_current_state_legacy(
                novel_id, card_id, req.current_state, chapter_order
            )
        if state is None:
            raise NotFoundError(f"Character state for card '{card_id}' was not found")
        return _serialize_state(state)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.put("/novel/{novel_id}/card/{card_id}/facts/{fact_id}")
async def edit_fact(novel_id: str, card_id: str, fact_id: str, req: PermanentFactUpdateRequest):
    try:
        fields = req.model_dump(exclude_unset=True)
        if req.source_chapter_id:
            chapter = await validate_chapter_reference(novel_id, req.source_chapter_id)
            fields["chapter_order"] = int(chapter.get("order_index") or 0)
        if req.source_chapter_id:
            state = await CharacterStateService.update_fact(
                novel_id, card_id, fact_id, fields, req.source_chapter_id
            )
        else:
            state = await CharacterStateService.update_fact_legacy(
                novel_id, card_id, fact_id, fields
            )
        if state is None:
            raise NotFoundError(f"Character state for card '{card_id}' was not found")
        return _serialize_state(state)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.delete("/novel/{novel_id}/card/{card_id}/facts/{fact_id}")
async def delete_fact(
    novel_id: str,
    card_id: str,
    fact_id: str,
    effective_chapter_id: Optional[str] = None,
):
    try:
        before = await character_state_repo.get_state(novel_id, card_id)
        if not any(
            str(fact.get("id")) == fact_id
            for fact in (before or {}).get("permanent_facts") or []
        ):
            raise NotFoundError(f"Permanent fact '{fact_id}' was not found")
        effective_id = effective_chapter_id or await latest_chapter_id(novel_id)
        await validate_chapter_reference(novel_id, effective_id)
        state = await CharacterStateService.delete_fact(
            novel_id, card_id, fact_id, effective_id
        )
        if state is None:
            raise NotFoundError(f"Character state for card '{card_id}' was not found")
        return _serialize_state(state)
    except Exception as exc:
        raise _translate_error(exc) from exc
