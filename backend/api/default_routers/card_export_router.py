"""Authenticated Character Card V2 JSON export endpoints."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from backend.api.default_routers.auth_router import require_owned_path_resource
from backend.db.errors import InvalidIdError, NotFoundError
from backend.services.auth.identity_service import Actor
from backend.services.interop.character_card_exporter import (
    character_card_export_service,
)


router = APIRouter(prefix="/api/card-exports", tags=["card-exports"])


@router.get("/novel/{novel_id}/character/{character_card_id}/v2")
async def export_character_card_v2(
    novel_id: str,
    character_card_id: str,
    worldbook_card_ids: list[str] = Query(default=[]),
    actor: Actor = Depends(require_owned_path_resource),
) -> Response:
    """Download one reviewed character and explicitly selected lore cards."""

    del actor
    try:
        document = await character_card_export_service.export_v2(
            novel_id,
            character_card_id,
            worldbook_card_ids,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InvalidIdError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    payload = (
        json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
        + b"\n"
    )
    return Response(
        content=payload,
        media_type="application/json",
        headers={
            "Content-Disposition": (
                'attachment; filename="novel-g-character-card-v2.json"'
            ),
        },
    )
