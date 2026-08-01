"""Owner-scoped staged illustration run and stage endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from backend.api.default_routers.auth_router import require_owned_path_resource
from backend.api.request_body import RequestBodyTooLarge, read_bounded_body
from backend.db.errors import InvalidIdError, NotFoundError
from backend.services.auth.identity_service import Actor
from backend.services.image.illustration_readiness_service import (
    IllustrationReadinessReport,
    IllustrationReadinessService,
    illustration_readiness_service,
)
from backend.services.image.illustration_run_service import (
    IllustrationRunActiveConflict,
    IllustrationRunCreate,
    IllustrationRunProjection,
    IllustrationRunRevisionConflict,
    IllustrationRunService,
    IllustrationRunStateError,
    illustration_run_service,
)
from backend.services.image.illustration_stage_service import (
    IllustrationCandidateDiscard,
    IllustrationCandidateListProjection,
    IllustrationCandidateMutationProjection,
    IllustrationCandidateProjection,
    IllustrationCandidateRestore,
    IllustrationCandidateSelect,
    IllustrationCandidateSelectionProjection,
    IllustrationExternalEditImport,
    IllustrationStageAdvance,
    IllustrationStageJobProjection,
    IllustrationStageService,
    IllustrationStageStart,
    illustration_stage_service,
)
from backend.services.image.managed_assets import InvalidImageAssetError


router = APIRouter(tags=["illustration-runs"])
MAX_EXTERNAL_EDIT_BYTES = 10 * 1024 * 1024


def get_illustration_readiness_service() -> IllustrationReadinessService:
    return illustration_readiness_service


def get_illustration_run_service() -> IllustrationRunService:
    return illustration_run_service


def get_illustration_stage_service() -> IllustrationStageService:
    return illustration_stage_service


def _translate_error(error: Exception) -> HTTPException:
    if isinstance(error, RequestBodyTooLarge):
        return HTTPException(status_code=413, detail=str(error))
    if isinstance(error, InvalidImageAssetError):
        return HTTPException(status_code=400, detail=str(error))
    if isinstance(
        error,
        (
            IllustrationRunActiveConflict,
            IllustrationRunRevisionConflict,
            IllustrationRunStateError,
        ),
    ):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, NotFoundError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, (InvalidIdError, ValueError)):
        return HTTPException(status_code=400, detail=str(error))
    return HTTPException(status_code=500, detail="Illustration run request failed")


@router.post(
    (
        "/api/novels/{novel_id}/chapters/{chapter_id}/"
        "illustration-briefs/{brief_id}/runs"
    ),
    response_model=IllustrationRunProjection,
)
async def create_illustration_run(
    novel_id: str,
    chapter_id: str,
    brief_id: str,
    request: IllustrationRunCreate,
    actor: Actor = Depends(require_owned_path_resource),
    service: IllustrationRunService = Depends(get_illustration_run_service),
) -> IllustrationRunProjection:
    try:
        return await service.create_run(
            owner_id=actor.id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            brief_id=brief_id,
            request=request,
        )
    except Exception as error:
        raise _translate_error(error) from error


@router.get(
    "/api/novels/{novel_id}/illustration-runs/{run_id}",
    response_model=IllustrationRunProjection,
)
async def get_illustration_run(
    novel_id: str,
    run_id: str,
    actor: Actor = Depends(require_owned_path_resource),
    service: IllustrationRunService = Depends(get_illustration_run_service),
) -> IllustrationRunProjection:
    try:
        return await service.get_run(
            owner_id=actor.id,
            novel_id=novel_id,
            run_id=run_id,
        )
    except Exception as error:
        raise _translate_error(error) from error


@router.post(
    (
        "/api/novels/{novel_id}/illustration-runs/{run_id}/"
        "stages/{stage}/start"
    ),
    response_model=IllustrationStageJobProjection,
)
async def start_illustration_stage(
    novel_id: str,
    run_id: str,
    stage: str,
    request: IllustrationStageStart,
    actor: Actor = Depends(require_owned_path_resource),
    service: IllustrationStageService = Depends(get_illustration_stage_service),
) -> IllustrationStageJobProjection:
    try:
        return await service.start_stage(
            owner_id=actor.id,
            novel_id=novel_id,
            run_id=run_id,
            stage=stage,
            request=request,
        )
    except Exception as error:
        raise _translate_error(error) from error


@router.get(
    (
        "/api/novels/{novel_id}/illustration-runs/{run_id}/"
        "jobs/{job_id}"
    ),
    response_model=IllustrationStageJobProjection,
)
async def poll_illustration_stage_job(
    novel_id: str,
    run_id: str,
    job_id: str,
    actor: Actor = Depends(require_owned_path_resource),
    service: IllustrationStageService = Depends(get_illustration_stage_service),
) -> IllustrationStageJobProjection:
    try:
        return await service.poll_job(
            owner_id=actor.id,
            novel_id=novel_id,
            run_id=run_id,
            job_id=job_id,
        )
    except Exception as error:
        raise _translate_error(error) from error


@router.post(
    (
        "/api/novels/{novel_id}/illustration-runs/{run_id}/"
        "jobs/{job_id}/cancel"
    ),
    response_model=IllustrationStageJobProjection,
)
async def cancel_illustration_stage_job(
    novel_id: str,
    run_id: str,
    job_id: str,
    actor: Actor = Depends(require_owned_path_resource),
    service: IllustrationStageService = Depends(get_illustration_stage_service),
) -> IllustrationStageJobProjection:
    try:
        return await service.cancel_job(
            owner_id=actor.id,
            novel_id=novel_id,
            run_id=run_id,
            job_id=job_id,
        )
    except Exception as error:
        raise _translate_error(error) from error


@router.get(
    "/api/novels/{novel_id}/illustration-runs/{run_id}/candidates",
    response_model=IllustrationCandidateListProjection,
)
async def list_illustration_candidates(
    novel_id: str,
    run_id: str,
    stage: Literal["compose", "identity_edit", "refine"] = Query(
        default="compose"
    ),
    actor: Actor = Depends(require_owned_path_resource),
    service: IllustrationStageService = Depends(get_illustration_stage_service),
) -> IllustrationCandidateListProjection:
    try:
        return await service.list_candidates(
            owner_id=actor.id,
            novel_id=novel_id,
            run_id=run_id,
            stage=stage,
        )
    except Exception as error:
        raise _translate_error(error) from error


@router.post(
    (
        "/api/novels/{novel_id}/illustration-runs/{run_id}/"
        "candidates/{asset_id}/select"
    ),
    response_model=IllustrationCandidateSelectionProjection,
)
async def select_illustration_candidate(
    novel_id: str,
    run_id: str,
    asset_id: str,
    request: IllustrationCandidateSelect,
    actor: Actor = Depends(require_owned_path_resource),
    service: IllustrationStageService = Depends(get_illustration_stage_service),
) -> IllustrationCandidateSelectionProjection:
    try:
        return await service.select_candidate(
            owner_id=actor.id,
            novel_id=novel_id,
            run_id=run_id,
            asset_id=asset_id,
            request=request,
        )
    except Exception as error:
        raise _translate_error(error) from error



@router.post(
    "/api/novels/{novel_id}/illustration-runs/{run_id}/external-edits",
    response_model=IllustrationCandidateProjection,
)
async def import_external_illustration_edit(
    novel_id: str,
    run_id: str,
    http_request: Request,
    target_stage: Literal["compose", "identity_edit", "refine"] = Query(),
    parent_asset_id: str = Query(min_length=1),
    expected_revision: int = Query(ge=1),
    actor: Actor = Depends(require_owned_path_resource),
    service: IllustrationStageService = Depends(get_illustration_stage_service),
) -> IllustrationCandidateProjection:
    try:
        content = await read_bounded_body(
            http_request,
            max_bytes=MAX_EXTERNAL_EDIT_BYTES,
        )
        return await service.import_external_edit(
            owner_id=actor.id,
            novel_id=novel_id,
            run_id=run_id,
            request=IllustrationExternalEditImport(
                expected_revision=expected_revision,
                target_stage=target_stage,
                parent_asset_id=parent_asset_id,
            ),
            content=content,
        )
    except Exception as error:
        raise _translate_error(error) from error


@router.post(
    "/api/novels/{novel_id}/illustration-candidates/{asset_id}/discard",
    response_model=IllustrationCandidateMutationProjection,
)
async def discard_illustration_candidate(
    novel_id: str,
    asset_id: str,
    request: IllustrationCandidateDiscard,
    actor: Actor = Depends(require_owned_path_resource),
    service: IllustrationStageService = Depends(get_illustration_stage_service),
) -> IllustrationCandidateMutationProjection:
    try:
        return await service.discard_candidate(
            owner_id=actor.id,
            novel_id=novel_id,
            asset_id=asset_id,
            request=request,
        )
    except Exception as error:
        raise _translate_error(error) from error


@router.post(
    "/api/novels/{novel_id}/illustration-candidates/{asset_id}/restore",
    response_model=IllustrationCandidateMutationProjection,
)
async def restore_illustration_candidate(
    novel_id: str,
    asset_id: str,
    request: IllustrationCandidateRestore,
    actor: Actor = Depends(require_owned_path_resource),
    service: IllustrationStageService = Depends(get_illustration_stage_service),
) -> IllustrationCandidateMutationProjection:
    try:
        return await service.restore_candidate(
            owner_id=actor.id,
            novel_id=novel_id,
            asset_id=asset_id,
            request=request,
        )
    except Exception as error:
        raise _translate_error(error) from error

@router.get(
    "/api/novels/{novel_id}/illustration-runs/{run_id}/readiness",
    response_model=IllustrationReadinessReport,
)
async def inspect_illustration_readiness(
    novel_id: str,
    run_id: str,
    actor: Actor = Depends(require_owned_path_resource),
    service: IllustrationReadinessService = Depends(
        get_illustration_readiness_service
    ),
) -> IllustrationReadinessReport:
    try:
        return await service.inspect(
            owner_id=actor.id,
            novel_id=novel_id,
            run_id=run_id,
        )
    except Exception as error:
        raise _translate_error(error) from error


@router.post(
    (
        "/api/novels/{novel_id}/illustration-runs/{run_id}/"
        "stages/{stage}/advance"
    ),
    response_model=IllustrationRunProjection,
)
async def advance_illustration_stage(
    novel_id: str,
    run_id: str,
    stage: str,
    request: IllustrationStageAdvance,
    actor: Actor = Depends(require_owned_path_resource),
    service: IllustrationStageService = Depends(
        get_illustration_stage_service
    ),
) -> IllustrationRunProjection:
    try:
        return await service.advance_stage(
            owner_id=actor.id,
            novel_id=novel_id,
            run_id=run_id,
            stage=stage,
            request=request,
        )
    except Exception as error:
        raise _translate_error(error) from error


__all__ = [
    "get_illustration_readiness_service",
    "get_illustration_run_service",
    "get_illustration_stage_service",
    "router",
]
