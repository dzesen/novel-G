"""Owner-scoped single-image jobs and managed image delivery."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from backend.api.default_routers.auth_router import (
    require_authenticated_request,
    require_owned_path_resource,
)
from backend.db.repositories.image_asset_repository import ImageAssetRepository
from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.utils import to_object_id
from backend.services.auth.identity_service import Actor
from backend.services.image.character_portrait_service import (
    CharacterPortraitService,
    CharacterPortraitStateProjection,
    PortraitAnchorResetRequired,
    PortraitConfigurationError,
    PortraitJobNotFoundError,
    PortraitJobProjection,
    character_portrait_service,
)
from backend.services.image.managed_assets import (
    ImageAssetIntegrityError,
    ImageAssetNotFoundError,
    ManagedImageAssetService,
)
from backend.services.image.novel_cover_service import (
    CoverAssetNotFoundError,
    CoverReferenceConflictError,
    NovelCoverService,
    NovelCoverStateProjection,
    novel_cover_service,
)
from backend.services.image.scene_illustration_service import (
    MAX_SCENE_CHARACTER_CARD_IDS,
    SceneIllustrationConfigurationError,
    SceneIllustrationService,
    SceneIllustrationStateProjection,
    scene_illustration_service,
)
from backend.services.image.single_image_job_service import ImageJobProjection
from backend.services.llm.agent_orchestrator import IllustrationPromptResult
from backend.services.novel.appearance_anchor import (
    AppearanceAnchorConflictError,
    AppearanceAnchorResetConfirmationRequired,
)


router = APIRouter(tags=["image-jobs"])


class CharacterPortraitJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: IllustrationPromptResult
    seed: int | None = None
    provider_alias: str | None = Field(default=None, max_length=200)
    confirm_anchor_reset: bool = False

    @field_validator("seed", mode="before")
    @classmethod
    def validate_seed(cls, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("seed must be an integer from 0 through 2^64-1")
        if isinstance(value, int):
            parsed = value
        elif (
            isinstance(value, str)
            and value
            and value.isascii()
            and value.isdecimal()
            and (value == "0" or not value.startswith("0"))
        ):
            parsed = int(value)
        else:
            raise ValueError("seed must be an integer from 0 through 2^64-1")
        if not 0 <= parsed <= (2**64 - 1):
            raise ValueError("seed must be an integer from 0 through 2^64-1")
        return parsed

    @field_validator("provider_alias", mode="before")
    @classmethod
    def normalize_provider_alias(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None


class NovelCoverJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: IllustrationPromptResult
    seed: int | None = None
    provider_alias: str | None = Field(default=None, max_length=200)
    width: int = Field(default=512, ge=64, le=4096)
    height: int = Field(default=768, ge=64, le=4096)

    @field_validator("seed", mode="before")
    @classmethod
    def validate_seed(cls, value: Any) -> int | None:
        return CharacterPortraitJobRequest.validate_seed(value)

    @field_validator("provider_alias", mode="before")
    @classmethod
    def normalize_provider_alias(cls, value: Any) -> str | None:
        return CharacterPortraitJobRequest.normalize_provider_alias(value)


class NovelCoverCurrentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str | None

    @field_validator("asset_id", mode="before")
    @classmethod
    def validate_asset_id(cls, value: Any) -> str | None:
        if value is None:
            return None
        try:
            return str(to_object_id(str(value).strip()))
        except InvalidIdError as error:
            raise ValueError(
                "asset_id must be a valid image asset id"
            ) from error


class SceneIllustrationJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: IllustrationPromptResult
    scene_character_card_ids: list[str] = Field(
        min_length=1,
        max_length=MAX_SCENE_CHARACTER_CARD_IDS,
    )
    reference_character_card_id: str
    seed: int | None = None
    provider_alias: str | None = Field(default=None, max_length=200)

    @field_validator("seed", mode="before")
    @classmethod
    def validate_seed(cls, value: Any) -> int | None:
        return CharacterPortraitJobRequest.validate_seed(value)

    @field_validator("provider_alias", mode="before")
    @classmethod
    def normalize_provider_alias(cls, value: Any) -> str | None:
        return CharacterPortraitJobRequest.normalize_provider_alias(value)

    @field_validator("scene_character_card_ids", mode="after")
    @classmethod
    def validate_scene_character_card_ids(
        cls,
        value: list[str],
    ) -> list[str]:
        canonical: list[str] = []
        for card_id in value:
            try:
                canonical.append(str(to_object_id(str(card_id).strip())))
            except InvalidIdError as error:
                raise ValueError(
                    "scene_character_card_ids must contain formal card ids"
                ) from error
        if len(set(canonical)) != len(canonical):
            raise ValueError(
                "scene_character_card_ids must not contain duplicates"
            )
        return canonical

    @field_validator("reference_character_card_id", mode="after")
    @classmethod
    def validate_reference_character_card_id(cls, value: str) -> str:
        try:
            return str(to_object_id(str(value).strip()))
        except InvalidIdError as error:
            raise ValueError(
                "reference_character_card_id must be a formal card id"
            ) from error

    @model_validator(mode="after")
    def reference_must_be_selected(self) -> "SceneIllustrationJobRequest":
        if (
            self.reference_character_card_id
            not in self.scene_character_card_ids
        ):
            raise ValueError(
                "reference_character_card_id must be selected for this scene"
            )
        return self


def get_character_portrait_service() -> CharacterPortraitService:
    return character_portrait_service


def get_novel_cover_service() -> NovelCoverService:
    return novel_cover_service


def get_scene_illustration_service() -> SceneIllustrationService:
    return scene_illustration_service


def _translate_portrait_error(error: Exception) -> HTTPException:
    if isinstance(error, PortraitJobNotFoundError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, NotFoundError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, InvalidIdError):
        return HTTPException(status_code=400, detail=str(error))
    if isinstance(
        error,
        (
            PortraitAnchorResetRequired,
            AppearanceAnchorResetConfirmationRequired,
            AppearanceAnchorConflictError,
        ),
    ):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, (PortraitConfigurationError, ValueError)):
        return HTTPException(status_code=400, detail=str(error))
    return HTTPException(status_code=500, detail="角色立绘任务处理失败")


def _translate_cover_error(error: Exception) -> HTTPException:
    if isinstance(error, (PortraitJobNotFoundError, CoverAssetNotFoundError)):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, NotFoundError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, CoverReferenceConflictError):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, (InvalidIdError, ValueError)):
        return HTTPException(status_code=400, detail=str(error))
    return HTTPException(status_code=500, detail="封面任务处理失败")


def _translate_scene_illustration_error(error: Exception) -> HTTPException:
    if isinstance(error, (PortraitJobNotFoundError, NotFoundError)):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(
        error,
        (
            InvalidIdError,
            PortraitConfigurationError,
            SceneIllustrationConfigurationError,
            ValueError,
        ),
    ):
        return HTTPException(status_code=400, detail=str(error))
    return HTTPException(status_code=500, detail="场景插图任务处理失败")


@router.get(
    "/api/reference-cards/novel/{novel_id}/character/{card_id}/portrait",
    response_model=CharacterPortraitStateProjection,
)
async def get_character_portrait_state(
    novel_id: str,
    card_id: str,
    actor: Actor = Depends(require_owned_path_resource),
    service: CharacterPortraitService = Depends(
        get_character_portrait_service
    ),
) -> CharacterPortraitStateProjection:
    try:
        return await service.get_state(
            owner_id=actor.id,
            novel_id=novel_id,
            card_id=card_id,
        )
    except Exception as error:
        raise _translate_portrait_error(error) from error


@router.post(
    "/api/reference-cards/novel/{novel_id}/character/{card_id}/portrait/jobs",
    response_model=PortraitJobProjection,
)
async def start_character_portrait_job(
    novel_id: str,
    card_id: str,
    request: CharacterPortraitJobRequest,
    actor: Actor = Depends(require_owned_path_resource),
    service: CharacterPortraitService = Depends(
        get_character_portrait_service
    ),
) -> PortraitJobProjection:
    try:
        return await service.start(
            owner_id=actor.id,
            novel_id=novel_id,
            card_id=card_id,
            prompt=request.prompt,
            seed=request.seed,
            provider_alias=request.provider_alias,
            confirm_anchor_reset=request.confirm_anchor_reset,
        )
    except Exception as error:
        raise _translate_portrait_error(error) from error


@router.get(
    "/api/reference-cards/novel/{novel_id}/character/{card_id}/portrait/jobs/{job_id}",
    response_model=PortraitJobProjection,
)
async def poll_character_portrait_job(
    novel_id: str,
    card_id: str,
    job_id: str,
    actor: Actor = Depends(require_owned_path_resource),
    service: CharacterPortraitService = Depends(
        get_character_portrait_service
    ),
) -> PortraitJobProjection:
    try:
        return await service.poll(
            owner_id=actor.id,
            novel_id=novel_id,
            card_id=card_id,
            job_id=job_id,
        )
    except Exception as error:
        raise _translate_portrait_error(error) from error


@router.post(
    "/api/reference-cards/novel/{novel_id}/character/{card_id}/portrait/jobs/{job_id}/cancel",
    response_model=PortraitJobProjection,
)
async def cancel_character_portrait_job(
    novel_id: str,
    card_id: str,
    job_id: str,
    actor: Actor = Depends(require_owned_path_resource),
    service: CharacterPortraitService = Depends(
        get_character_portrait_service
    ),
) -> PortraitJobProjection:
    try:
        return await service.cancel(
            owner_id=actor.id,
            novel_id=novel_id,
            card_id=card_id,
            job_id=job_id,
        )
    except Exception as error:
        raise _translate_portrait_error(error) from error


@router.get(
    "/api/novels/{novel_id}/cover",
    response_model=NovelCoverStateProjection,
)
async def get_novel_cover_state(
    novel_id: str,
    actor: Actor = Depends(require_owned_path_resource),
    service: NovelCoverService = Depends(get_novel_cover_service),
) -> NovelCoverStateProjection:
    try:
        return await service.get_state(
            owner_id=actor.id,
            novel_id=novel_id,
        )
    except Exception as error:
        raise _translate_cover_error(error) from error


@router.post(
    "/api/novels/{novel_id}/cover/jobs",
    response_model=ImageJobProjection,
)
async def start_novel_cover_job(
    novel_id: str,
    request: NovelCoverJobRequest,
    actor: Actor = Depends(require_owned_path_resource),
    service: NovelCoverService = Depends(get_novel_cover_service),
) -> ImageJobProjection:
    try:
        return await service.start(
            owner_id=actor.id,
            novel_id=novel_id,
            prompt=request.prompt,
            width=request.width,
            height=request.height,
            seed=request.seed,
            provider_alias=request.provider_alias,
        )
    except Exception as error:
        raise _translate_cover_error(error) from error


@router.get(
    "/api/novels/{novel_id}/cover/jobs/{job_id}",
    response_model=ImageJobProjection,
)
async def poll_novel_cover_job(
    novel_id: str,
    job_id: str,
    actor: Actor = Depends(require_owned_path_resource),
    service: NovelCoverService = Depends(get_novel_cover_service),
) -> ImageJobProjection:
    try:
        return await service.poll(
            owner_id=actor.id,
            novel_id=novel_id,
            job_id=job_id,
        )
    except Exception as error:
        raise _translate_cover_error(error) from error


@router.post(
    "/api/novels/{novel_id}/cover/jobs/{job_id}/cancel",
    response_model=ImageJobProjection,
)
async def cancel_novel_cover_job(
    novel_id: str,
    job_id: str,
    actor: Actor = Depends(require_owned_path_resource),
    service: NovelCoverService = Depends(get_novel_cover_service),
) -> ImageJobProjection:
    try:
        return await service.cancel(
            owner_id=actor.id,
            novel_id=novel_id,
            job_id=job_id,
        )
    except Exception as error:
        raise _translate_cover_error(error) from error


@router.put(
    "/api/novels/{novel_id}/cover/current",
    response_model=NovelCoverStateProjection,
)
async def select_novel_cover(
    novel_id: str,
    request: NovelCoverCurrentRequest,
    actor: Actor = Depends(require_owned_path_resource),
    service: NovelCoverService = Depends(get_novel_cover_service),
) -> NovelCoverStateProjection:
    try:
        return await service.select_current(
            owner_id=actor.id,
            novel_id=novel_id,
            asset_id=request.asset_id,
        )
    except Exception as error:
        raise _translate_cover_error(error) from error


@router.get(
    (
        "/api/novels/{novel_id}/chapters/{chapter_id}/"
        "scene-illustration"
    ),
    response_model=SceneIllustrationStateProjection,
)
async def get_scene_illustration_state(
    novel_id: str,
    chapter_id: str,
    actor: Actor = Depends(require_owned_path_resource),
    service: SceneIllustrationService = Depends(
        get_scene_illustration_service
    ),
) -> SceneIllustrationStateProjection:
    try:
        return await service.get_state(
            owner_id=actor.id,
            novel_id=novel_id,
            chapter_id=chapter_id,
        )
    except Exception as error:
        raise _translate_scene_illustration_error(error) from error


@router.post(
    (
        "/api/novels/{novel_id}/chapters/{chapter_id}/"
        "scene-illustration/jobs"
    ),
    response_model=ImageJobProjection,
)
async def start_scene_illustration_job(
    novel_id: str,
    chapter_id: str,
    request: SceneIllustrationJobRequest,
    actor: Actor = Depends(require_owned_path_resource),
    service: SceneIllustrationService = Depends(
        get_scene_illustration_service
    ),
) -> ImageJobProjection:
    try:
        return await service.start(
            owner_id=actor.id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            prompt=request.prompt,
            scene_character_card_ids=request.scene_character_card_ids,
            reference_character_card_id=(
                request.reference_character_card_id
            ),
            seed=request.seed,
            provider_alias=request.provider_alias,
        )
    except Exception as error:
        raise _translate_scene_illustration_error(error) from error


@router.get(
    (
        "/api/novels/{novel_id}/chapters/{chapter_id}/"
        "scene-illustration/jobs/{job_id}"
    ),
    response_model=ImageJobProjection,
)
async def poll_scene_illustration_job(
    novel_id: str,
    chapter_id: str,
    job_id: str,
    actor: Actor = Depends(require_owned_path_resource),
    service: SceneIllustrationService = Depends(
        get_scene_illustration_service
    ),
) -> ImageJobProjection:
    try:
        return await service.poll(
            owner_id=actor.id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            job_id=job_id,
        )
    except Exception as error:
        raise _translate_scene_illustration_error(error) from error


@router.post(
    (
        "/api/novels/{novel_id}/chapters/{chapter_id}/"
        "scene-illustration/jobs/{job_id}/cancel"
    ),
    response_model=ImageJobProjection,
)
async def cancel_scene_illustration_job(
    novel_id: str,
    chapter_id: str,
    job_id: str,
    actor: Actor = Depends(require_owned_path_resource),
    service: SceneIllustrationService = Depends(
        get_scene_illustration_service
    ),
) -> ImageJobProjection:
    try:
        return await service.cancel(
            owner_id=actor.id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            job_id=job_id,
        )
    except Exception as error:
        raise _translate_scene_illustration_error(error) from error


@router.get("/api/image-assets/{asset_id}/content")
async def get_managed_image_asset_content(
    asset_id: str,
    actor: Actor = Depends(require_authenticated_request),
) -> Response:
    repository = ImageAssetRepository()
    service = ManagedImageAssetService(repository=repository)
    try:
        document = await repository.get_owned_by_id(
            owner_id=to_object_id(actor.id),
            asset_id=to_object_id(asset_id),
        )
        if document is None:
            raise ImageAssetNotFoundError("Image asset not found")
        content = await service.read_owned_asset(
            owner_id=actor.id,
            asset_id=asset_id,
        )
    except (
        ImageAssetNotFoundError,
        ImageAssetIntegrityError,
        InvalidIdError,
        ValueError,
    ):
        raise HTTPException(status_code=404, detail="Image asset not found")
    return Response(
        content=content,
        media_type=str(document.get("mime") or "application/octet-stream"),
        headers={"Cache-Control": "private, no-store"},
    )
