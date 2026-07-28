"""Authenticated Character Card and world-book import endpoints."""

from __future__ import annotations

import hashlib
from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from backend.api.default_routers.auth_router import (
    require_authenticated_request,
    require_owned_path_resource,
)
from backend.api.default_routers.reference_card_router import (
    ReferenceCardCurationDecision,
)
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


def _validation_http_error(
    exc: CharacterCardValidationError | WorldBookValidationError,
) -> HTTPException:
    detail = {
        "code": exc.code,
        "path": exc.path,
        "message": exc.message,
    }
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
