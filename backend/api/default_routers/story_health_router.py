"""Read-only deterministic story health API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from backend.api.default_routers.auth_router import require_owned_path_resource
from backend.db.errors import InvalidIdError, NotFoundError
from backend.services.novel.story_health import StoryHealthReport, story_health


router = APIRouter(
    prefix="/api/story-health",
    tags=["story-health"],
    dependencies=[Depends(require_owned_path_resource)],
)


@router.get("/novel/{novel_id}", response_model=StoryHealthReport)
async def inspect_story_health(
    novel_id: str,
    volume_id: str | None = None,
) -> StoryHealthReport:
    """Return deterministic signals without invoking a model or writing data."""
    try:
        return await story_health.inspect(novel_id, volume_id=volume_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
