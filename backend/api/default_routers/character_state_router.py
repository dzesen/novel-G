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


router = APIRouter(prefix="/api/character-states", tags=["character-states"])


class CurrentStateUpdateRequest(BaseModel):
    current_state: str
    as_of_chapter_order: int


class PermanentFactUpdateRequest(BaseModel):
    # kind 用 Optional[str] 而非 Literal：非法值落到仓储 ValueError→400，非 422。
    fact: Optional[str] = None
    kind: Optional[str] = None
    chapter_order: Optional[int] = None


def _serialize_state(state: dict) -> dict:
    """顶层 + 每条 fact 的 ObjectId 转字符串。datetime 由 FastAPI 自行编码。"""
    result = dict(state)
    for key in ("_id", "novel_id", "card_id"):
        if key in result:
            result[key] = str(result[key])
    facts = []
    for fact in result.get("permanent_facts") or []:
        item = dict(fact)
        if "id" in item:
            item["id"] = str(item["id"])
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
        await character_state_repo.set_current_state(
            novel_id, card_id, req.current_state, req.as_of_chapter_order
        )
        state = await character_state_repo.get_state(novel_id, card_id)
        return _serialize_state(state)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.put("/novel/{novel_id}/card/{card_id}/facts/{fact_id}")
async def edit_fact(novel_id: str, card_id: str, fact_id: str, req: PermanentFactUpdateRequest):
    try:
        await character_state_repo.update_permanent_fact(
            novel_id, card_id, fact_id, req.model_dump(exclude_unset=True)
        )
        state = await character_state_repo.get_state(novel_id, card_id)
        return _serialize_state(state)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.delete("/novel/{novel_id}/card/{card_id}/facts/{fact_id}")
async def delete_fact(novel_id: str, card_id: str, fact_id: str):
    try:
        await character_state_repo.delete_permanent_fact(novel_id, card_id, fact_id)
        state = await character_state_repo.get_state(novel_id, card_id)
        return _serialize_state(state)
    except Exception as exc:
        raise _translate_error(exc) from exc
