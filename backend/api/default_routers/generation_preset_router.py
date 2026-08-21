"""Authenticated, write-free generation-preset preview endpoints."""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from backend.api.default_routers.auth_router import require_authenticated_request
from backend.services.interop.generation_preset_adapter import (
    MAX_GENERATION_PRESET_JSON_BYTES,
    GenerationPresetAdapter,
    GenerationPresetValidationError,
)


router = APIRouter(
    prefix="/api/generation-presets",
    tags=["generation-presets"],
    dependencies=[Depends(require_authenticated_request)],
)

_PAYLOAD_TOO_LARGE_CODES = frozenset({"file_too_large"})


def _validation_http_error(exc: GenerationPresetValidationError) -> HTTPException:
    detail: dict[str, object] = {
        "code": exc.code,
        "path": exc.path,
        "message": exc.message,
    }
    if exc.limit_name:
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


@router.post("/preview")
async def preview_generation_preset(
    file: UploadFile = File(...),
) -> dict[str, object]:
    """Inspect one preset without persistence, code execution or paid calls."""

    payload = await file.read(MAX_GENERATION_PRESET_JSON_BYTES + 1)
    try:
        parsed = GenerationPresetAdapter.parse_json(
            payload,
            declared_mime=file.content_type or "",
            filename=file.filename,
        )
        return parsed.public_view(
            source_name=file.filename or "preset.json",
            source_hash=hashlib.sha256(payload).hexdigest(),
        )
    except GenerationPresetValidationError as exc:
        raise _validation_http_error(exc) from exc
    finally:
        await file.close()
