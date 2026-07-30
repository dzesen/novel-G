"""Owner-scoped chapter illustration brief endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from backend.api.default_routers.auth_router import require_owned_path_resource
from backend.db.errors import InvalidIdError, NotFoundError
from backend.services.auth.identity_service import Actor
from backend.services.image.illustration_brief_service import (
    IllustrationBriefCreate,
    IllustrationBriefListProjection,
    IllustrationBriefPatch,
    IllustrationBriefProjection,
    IllustrationBriefRefresh,
    IllustrationBriefRevisionConflict,
    IllustrationBriefService,
    LegacyAssetAdoptionProjection,
    illustration_brief_service,
)


router = APIRouter(tags=["illustration-briefs"])


class LegacyAssetAdoptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_revision: int = Field(ge=1)
    asset_id: str = Field(min_length=1)


def get_illustration_brief_service() -> IllustrationBriefService:
    return illustration_brief_service


def _translate_error(error: Exception) -> HTTPException:
    if isinstance(error, IllustrationBriefRevisionConflict):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, NotFoundError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, (InvalidIdError, ValueError)):
        return HTTPException(status_code=400, detail=str(error))
    return HTTPException(status_code=500, detail="Illustration brief request failed")


@router.get(
    "/api/novels/{novel_id}/chapters/{chapter_id}/illustration-briefs",
    response_model=IllustrationBriefListProjection,
)
async def list_illustration_briefs(
    novel_id: str,
    chapter_id: str,
    include_archived: bool = Query(default=False),
    actor: Actor = Depends(require_owned_path_resource),
    service: IllustrationBriefService = Depends(
        get_illustration_brief_service
    ),
) -> IllustrationBriefListProjection:
    try:
        return await service.list_briefs(
            owner_id=actor.id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            include_archived=include_archived,
        )
    except Exception as error:
        raise _translate_error(error) from error


@router.post(
    "/api/novels/{novel_id}/chapters/{chapter_id}/illustration-briefs",
    response_model=IllustrationBriefProjection,
)
async def create_illustration_brief(
    novel_id: str,
    chapter_id: str,
    request: IllustrationBriefCreate,
    actor: Actor = Depends(require_owned_path_resource),
    service: IllustrationBriefService = Depends(
        get_illustration_brief_service
    ),
) -> IllustrationBriefProjection:
    try:
        return await service.create_brief(
            owner_id=actor.id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            request=request,
        )
    except Exception as error:
        raise _translate_error(error) from error


@router.patch(
    (
        "/api/novels/{novel_id}/chapters/{chapter_id}/"
        "illustration-briefs/{brief_id}"
    ),
    response_model=IllustrationBriefProjection,
)
async def patch_illustration_brief(
    novel_id: str,
    chapter_id: str,
    brief_id: str,
    request: IllustrationBriefPatch,
    actor: Actor = Depends(require_owned_path_resource),
    service: IllustrationBriefService = Depends(
        get_illustration_brief_service
    ),
) -> IllustrationBriefProjection:
    try:
        return await service.patch_brief(
            owner_id=actor.id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            brief_id=brief_id,
            request=request,
        )
    except Exception as error:
        raise _translate_error(error) from error


@router.post(
    (
        "/api/novels/{novel_id}/chapters/{chapter_id}/"
        "illustration-briefs/{brief_id}/refresh-from-outline"
    ),
    response_model=IllustrationBriefProjection,
)
async def refresh_illustration_brief_from_outline(
    novel_id: str,
    chapter_id: str,
    brief_id: str,
    request: IllustrationBriefRefresh,
    actor: Actor = Depends(require_owned_path_resource),
    service: IllustrationBriefService = Depends(
        get_illustration_brief_service
    ),
) -> IllustrationBriefProjection:
    try:
        return await service.update_brief_from_outline(
            owner_id=actor.id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            brief_id=brief_id,
            request=request,
        )
    except Exception as error:
        raise _translate_error(error) from error


@router.post(
    (
        "/api/novels/{novel_id}/chapters/{chapter_id}/"
        "illustration-briefs/{brief_id}/adopt-legacy-asset"
    ),
    response_model=LegacyAssetAdoptionProjection,
)
async def adopt_legacy_scene_illustration(
    novel_id: str,
    chapter_id: str,
    brief_id: str,
    request: LegacyAssetAdoptionRequest,
    actor: Actor = Depends(require_owned_path_resource),
    service: IllustrationBriefService = Depends(
        get_illustration_brief_service
    ),
) -> LegacyAssetAdoptionProjection:
    try:
        return await service.adopt_legacy_asset(
            owner_id=actor.id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            brief_id=brief_id,
            asset_id=request.asset_id,
            expected_revision=request.expected_revision,
        )
    except Exception as error:
        raise _translate_error(error) from error
