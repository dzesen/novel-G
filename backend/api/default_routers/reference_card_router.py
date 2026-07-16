"""CRUD API for character, location, item and world-rule cards."""

from __future__ import annotations

from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.db.errors import InvalidIdError, NotFoundError
from backend.services.novel.reference_card_service import ReferenceCardService


router = APIRouter(prefix="/api/reference-cards", tags=["reference-cards"])


class ReferenceCardCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    subtitle: str = Field(default="", max_length=200)
    description: str = ""
    details: Dict[str, str] = Field(default_factory=dict)
    tags: List[str] = Field(default_factory=list)
    sort_order: Optional[int] = None


class ReferenceCardUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    subtitle: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = None
    details: Optional[Dict[str, str]] = None
    tags: Optional[List[str]] = None
    sort_order: Optional[int] = None


def _serialize_card(card: dict) -> dict:
    result = dict(card)
    for key in ("_id", "novel_id"):
        if key in result:
            result[key] = str(result[key])
    return result


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (InvalidIdError, ValueError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@router.post("/novel/{novel_id}/{card_type}")
async def create_card(novel_id: str, card_type: str, req: ReferenceCardCreateRequest):
    try:
        card_id = await ReferenceCardService.create(
            novel_id,
            card_type,
            req.model_dump(exclude_none=True),
        )
        card = await ReferenceCardService.get(novel_id, card_type, card_id)
        return _serialize_card(card)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/novel/{novel_id}/{card_type}")
async def list_cards(novel_id: str, card_type: str):
    try:
        cards = await ReferenceCardService.list(novel_id, card_type)
        return {"data": [_serialize_card(card) for card in cards]}
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/novel/{novel_id}/{card_type}/trash")
async def list_deleted_cards(novel_id: str, card_type: str):
    try:
        cards = await ReferenceCardService.list(novel_id, card_type, deleted_only=True)
        return {"data": [_serialize_card(card) for card in cards]}
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/novel/{novel_id}/{card_type}/{card_id}")
async def get_card(novel_id: str, card_type: str, card_id: str):
    try:
        return _serialize_card(await ReferenceCardService.get(novel_id, card_type, card_id))
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.put("/novel/{novel_id}/{card_type}/{card_id}")
async def update_card(novel_id: str, card_type: str, card_id: str, req: ReferenceCardUpdateRequest):
    try:
        await ReferenceCardService.update(
            novel_id,
            card_type,
            card_id,
            req.model_dump(exclude_unset=True),
        )
        card = await ReferenceCardService.get(novel_id, card_type, card_id)
        return _serialize_card(card)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.delete("/novel/{novel_id}/{card_type}/{card_id}")
async def soft_delete_card(novel_id: str, card_type: str, card_id: str):
    try:
        return {"success": await ReferenceCardService.soft_delete(novel_id, card_type, card_id)}
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/novel/{novel_id}/{card_type}/{card_id}/restore")
async def restore_card(novel_id: str, card_type: str, card_id: str):
    try:
        await ReferenceCardService.restore(novel_id, card_type, card_id)
        card = await ReferenceCardService.get(novel_id, card_type, card_id)
        return _serialize_card(card)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.delete("/novel/{novel_id}/{card_type}/{card_id}/hard")
async def hard_delete_card(novel_id: str, card_type: str, card_id: str):
    try:
        return {"success": await ReferenceCardService.hard_delete(novel_id, card_type, card_id)}
    except Exception as exc:
        raise _translate_error(exc) from exc
