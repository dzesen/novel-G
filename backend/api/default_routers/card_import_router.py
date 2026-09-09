"""Authenticated Character Card and world-book import endpoints."""

from __future__ import annotations

import hashlib
from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from backend.api.default_routers.auth_router import (
    require_authenticated_request,
    require_owned_path_resource,
)
from backend.api.default_routers.reference_card_router import (
    ReferenceCardCurationDecision,
)
from backend.api.request_body import RequestBodyTooLarge, read_bounded_body
from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.mutation import MutationConflictError
from backend.services.auth.identity_service import Actor
from backend.services.interop.card_import_proposal_service import (
    MAX_CARD_IMPORT_CANDIDATES,
    CardImportProposalError,
    RawPayloadTooLargeError,
    StaleCardImportProposal,
    card_import_proposal_service,
)
from backend.services.interop.character_card_adapter import (
    MAX_JSON_BYTES,
    MAX_PNG_BYTES,
    CharacterCardAdapter,
    CharacterCardValidationError,
)
from backend.services.interop.character_card_avatar_import import (
    CharacterCardAvatarImportService,
    CharacterCardAvatarNotFound,
    InvalidCharacterCardAvatar,
    character_card_avatar_import_service,
)
from backend.services.interop.generation_preset_adapter import (
    GenerationPresetAdapter,
    GenerationPresetValidationError,
)
from backend.services.interop.world_book_adapter import (
    MAX_WORLD_BOOK_JSON_BYTES,
    WorldBookAdapter,
    WorldBookValidationError,
)


router = APIRouter(prefix="/api/card-imports", tags=["card-imports"])
_PAYLOAD_TOO_LARGE_CODES = frozenset(
    {
        "file_too_large",
        "decoded_metadata_too_large",
    }
)


class CardImportApplyRequest(BaseModel):
    digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    decisions: List[ReferenceCardCurationDecision] = Field(
        min_length=1,
        max_length=MAX_CARD_IMPORT_CANDIDATES,
    )


def get_character_card_avatar_import_service() -> CharacterCardAvatarImportService:
    return character_card_avatar_import_service


def _validation_http_error(
    exc: CharacterCardValidationError | WorldBookValidationError,
) -> HTTPException:
    detail = {
        "code": exc.code,
        "path": exc.path,
        "message": exc.message,
    }
    if (
        isinstance(exc, CharacterCardValidationError)
        and exc.missing_metadata_kind is not None
    ):
        detail.update(
            {
                "missing_metadata_kind": exc.missing_metadata_kind,
                "text_keywords": list(exc.text_keywords),
            }
        )
    if isinstance(exc, WorldBookValidationError) and exc.limit_name:
        detail.update(
            {
                "limit_name": exc.limit_name,
                "current_value": exc.current_value,
                "max_value": exc.max_value,
            }
        )
    return HTTPException(
        status_code=413 if exc.code in _PAYLOAD_TOO_LARGE_CODES else 400,
        detail=detail,
    )


async def _stage_upload(
    file: UploadFile,
    *,
    actor: Actor,
    novel_id: str | None,
) -> dict:
    declared_mime = (file.content_type or "").partition(";")[0].strip().lower()
    try:
        if declared_mime == "application/json":
            payload = await file.read(MAX_WORLD_BOOK_JSON_BYTES + 1)
            try:
                GenerationPresetAdapter.parse_json(
                    payload,
                    declared_mime=file.content_type or "",
                    filename=file.filename,
                )
            except GenerationPresetValidationError:
                pass
            else:
                raise HTTPException(
                    status_code=400,
                    detail={
                        # Stable compatibility code: older clients still key on
                        # the retired Studio name even though the destination is
                        # now global generation-role settings.
                        "code": "generation_preset_requires_agent_studio",
                        "path": "$",
                        "message": (
                            "这是生成预设，不是世界书或角色卡；"
                            "请到全局设置的生成角色页导入。"
                        ),
                    },
                )
            try:
                parsed = WorldBookAdapter.parse_json(
                    payload,
                    declared_mime=file.content_type or "",
                    filename=file.filename,
                )
            except WorldBookValidationError as worldbook_error:
                fallback_to_character = (
                    worldbook_error.code == "not_standalone_worldbook"
                    or (
                        len(payload) > MAX_JSON_BYTES
                        and worldbook_error.code
                        in {
                            "malformed_json",
                            "duplicate_key",
                            "invalid_encoding",
                        }
                    )
                )
                if not fallback_to_character:
                    raise
                parsed = CharacterCardAdapter.parse_json(
                    payload,
                    declared_mime=file.content_type or "",
                    filename=file.filename,
                )
        elif declared_mime == "image/png":
            payload = await file.read(MAX_PNG_BYTES + 1)
            parsed = CharacterCardAdapter.parse_png(
                payload,
                declared_mime=file.content_type or "",
                filename=file.filename,
            )
        else:
            raise HTTPException(
                status_code=415,
                detail={
                    "code": "unsupported_media_type",
                    "path": "$",
                    "message": "仅接受 application/json 或 image/png",
                },
            )

        return await card_import_proposal_service.stage(
            parsed,
            owner_id=actor.id,
            novel_id=novel_id,
            source_hash=hashlib.sha256(payload).hexdigest(),
            source_name=file.filename,
        )
    except (CharacterCardValidationError, WorldBookValidationError) as exc:
        raise _validation_http_error(exc) from exc
    except RawPayloadTooLargeError as exc:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "raw_payload_too_large",
                "current_bytes": exc.current_bytes,
                "max_bytes": exc.max_bytes,
                "message": str(exc),
            },
        ) from exc
    except HTTPException:
        raise
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await file.close()


@router.post("/novel/{novel_id}/preview")
async def preview_card_import_for_novel(
    novel_id: str,
    file: UploadFile = File(...),
    actor: Actor = Depends(require_owned_path_resource),
):
    """Stage a preview for an owned novel without writing formal cards."""
    return await _stage_upload(file, actor=actor, novel_id=novel_id)


@router.post("/preview")
async def preview_card_import_before_novel(
    file: UploadFile = File(...),
    actor: Actor = Depends(require_authenticated_request),
):
    """Stage an owner-only preview before a novel exists."""
    return await _stage_upload(file, actor=actor, novel_id=None)


@router.get("/proposals/{import_proposal_id}")
async def inspect_card_import_proposal(
    import_proposal_id: str,
    actor: Actor = Depends(require_authenticated_request),
):
    try:
        return await card_import_proposal_service.inspect(
            import_proposal_id,
            owner_id=actor.id,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/proposals/{import_proposal_id}/apply")
async def apply_card_import_proposal(
    import_proposal_id: str,
    req: CardImportApplyRequest,
    actor: Actor = Depends(require_authenticated_request),
):
    try:
        return await card_import_proposal_service.apply(
            import_proposal_id,
            owner_id=actor.id,
            digest=req.digest,
            decisions=[
                item.model_dump(exclude_none=True) for item in req.decisions
            ],
        )
    except (StaleCardImportProposal, MutationConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (CardImportProposalError, InvalidIdError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/proposals/{import_proposal_id}/review-direction")
async def review_card_import_for_direction(
    import_proposal_id: str,
    req: CardImportApplyRequest,
    actor: Actor = Depends(require_authenticated_request),
):
    """Save reviewed candidate text and selections without formal writes or LLM calls."""
    try:
        return await card_import_proposal_service.review_for_direction(
            import_proposal_id, owner_id=actor.id, digest=req.digest,
            decisions=[item.model_dump(exclude_none=True) for item in req.decisions],
        )
    except StaleCardImportProposal as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (CardImportProposalError, InvalidIdError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/proposals/{import_proposal_id}/avatar")
async def import_applied_character_card_avatar(
    import_proposal_id: str,
    request: Request,
    actor: Actor = Depends(require_authenticated_request),
    service: CharacterCardAvatarImportService = Depends(
        get_character_card_avatar_import_service
    ),
):
    """Transfer reviewed card-owned image bytes into managed asset storage."""

    declared_mime = (
        request.headers.get("content-type", "")
        .partition(";")[0]
        .strip()
        .lower()
    )
    if declared_mime == "image/png":
        max_bytes = MAX_PNG_BYTES
    elif declared_mime == "application/json":
        max_bytes = MAX_JSON_BYTES
    else:
        raise HTTPException(
            status_code=415,
            detail={
                "code": "unsupported_avatar_source_type",
                "message": "头像来源必须是角色卡 PNG 或 JSON 文件",
            },
        )
    try:
        source_payload = await read_bounded_body(
            request,
            max_bytes=max_bytes,
        )
    except RequestBodyTooLarge as exc:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "avatar_source_too_large",
                "current_bytes": exc.current_bytes,
                "max_bytes": exc.max_bytes,
                "message": "角色卡头像来源文件超过允许的字节上限",
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_avatar_source_length",
                "message": str(exc),
            },
        ) from exc

    try:
        result = await service.import_applied_source(
            proposal_id=import_proposal_id,
            owner_id=actor.id,
            declared_mime=declared_mime,
            source_payload=source_payload,
        )
        return result.as_dict()
    except CharacterCardAvatarNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CharacterCardValidationError as exc:
        raise _validation_http_error(exc) from exc
    except InvalidCharacterCardAvatar as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_card_avatar",
                "message": str(exc),
            },
        ) from exc
