"""状态时间线诊断与零模型调用的确定性重建端点。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.repositories.novel_repository import novel_repo
from backend.services.novel.narrative_timeline import narrative_timeline
from backend.api.default_routers.auth_router import require_owned_path_resource
from backend.services.auth.identity_service import Actor
from backend.services.novel.state_completeness_audit import (
    state_completeness_audit,
)


router = APIRouter(
    prefix="/api/state-timeline",
    tags=["state-timeline"],
    dependencies=[Depends(require_owned_path_resource)],
)


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


@router.get("/novel/{novel_id}/completeness-audit")
async def completeness_audit(
    novel_id: str,
    scope: str = "book",
    volume_id: str | None = None,
    actor: Actor = Depends(require_owned_path_resource),
):
    """Read-only coverage report; it never generates or accepts state."""
    try:
        return await state_completeness_audit.audit(
            actor_id=actor.id,
            novel_id=novel_id,
            scope=scope,
            volume_id=volume_id,
        )
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
