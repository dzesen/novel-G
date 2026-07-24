"""状态时间线诊断与零模型调用的确定性重建端点。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.novel_repository import novel_repo
from backend.services.novel.narrative_timeline import narrative_timeline


router = APIRouter(prefix="/api/state-timeline", tags=["state-timeline"])


def _client_error(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (InvalidIdError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@router.get("/novel/{novel_id}/replay-plan")
async def replay_plan(novel_id: str):
    try:
        await novel_repo.get_novel_by_id(novel_id)
        return await narrative_timeline.audit(novel_id)
    except Exception as exc:
        raise _client_error(exc) from exc


@router.post("/novel/{novel_id}/rebuild")
async def rebuild(novel_id: str):
    """仅重放已接受数据和人工订正；此端点永不调用 LLM。"""
    try:
        await novel_repo.get_novel_by_id(novel_id)
        return await narrative_timeline.refresh(novel_id)
    except Exception as exc:
        raise _client_error(exc) from exc
