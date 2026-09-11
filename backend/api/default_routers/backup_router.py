"""本地备份、恢复与整书导出 API。"""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response

from backend.db.collections import NOVELS
from backend.db.errors import InvalidIdError, NotFoundError
from backend.services.backup.backup_service import (
    MAX_BACKUP_BYTES,
    BackupRestoreBusyError,
    build_novel_backup,
    build_novel_text,
    create_backup_snapshot,
    get_backup_status,
    parse_backup,
    restore_backup,
    serialize_backup,
)
from backend.api.default_routers.auth_router import (
    require_admin_request,
    require_owned_path_resource,
)
from backend.services.auth.identity_service import Actor
from backend.services.image.asset_reconciliation import (
    reconcile_managed_image_assets,
)


router = APIRouter(prefix="/api/backup", tags=["backup"])
logger = logging.getLogger(__name__)


def _attachment_headers(filename: str) -> dict[str, str]:
    safe_ascii = "backup" + (".txt" if filename.lower().endswith(".txt") else ".json")
    return {
        "Content-Disposition": (
            f"attachment; filename=\"{safe_ascii}\"; filename*=UTF-8''{quote(filename)}"
        ),
        "Cache-Control": "no-store",
    }


@router.get("/status", dependencies=[Depends(require_admin_request)])
async def backup_status():
    return await get_backup_status()


@router.get("/export", dependencies=[Depends(require_admin_request)])
async def export_backup():
    snapshot = await create_backup_snapshot()
    created = snapshot["created_at"].strftime("%Y%m%d-%H%M%S")
    filename = f"novel-generator-backup-{created}.json"
    return Response(
        content=serialize_backup(snapshot),
        media_type="application/json; charset=utf-8",
        headers=_attachment_headers(filename),
    )


@router.post("/restore")
async def import_backup(
    file: UploadFile = File(...),
    actor: Actor = Depends(require_admin_request),
):
    content = await file.read(MAX_BACKUP_BYTES + 1)
    try:
        payload = parse_backup(content)
        stats = await restore_backup(payload)
    except BackupRestoreBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Backup restore failed: {exc}") from exc
    try:
        report = await reconcile_managed_image_assets(owner_id=actor.id)
    except Exception:
        logger.exception(
            "Managed image asset reconciliation failed after a successful restore."
        )
        return {
            "message": "Backup restored",
            "stats": stats,
            "asset_reconciliation_status": "unavailable",
            "asset_reconciliation": None,
        }
    return {
        "message": "Backup restored",
        "stats": stats,
        "asset_reconciliation_status": "completed",
        "asset_reconciliation": report,
    }


@router.post("/image-assets/reconcile")
async def reconcile_image_assets(
    actor: Actor = Depends(require_admin_request),
):
    try:
        return await reconcile_managed_image_assets(owner_id=actor.id)
    except Exception as exc:
        logger.exception("Managed image asset reconciliation failed.")
        raise HTTPException(
            status_code=500,
            detail="Managed image asset reconciliation is currently unavailable",
        ) from exc


@router.get(
    "/novel/{novel_id}/text",
    dependencies=[Depends(require_owned_path_resource)],
)
async def export_novel_text(novel_id: str):
    try:
        filename, content = await build_novel_text(novel_id)
        return Response(
            content=content,
            media_type="text/plain; charset=utf-8",
            headers=_attachment_headers(filename),
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/novel/{novel_id}/json",
    dependencies=[Depends(require_owned_path_resource)],
)
async def export_novel_json(novel_id: str):
    try:
        snapshot = await build_novel_backup(novel_id)
        title = snapshot["collections"][NOVELS][0].get("title", "novel")
        return Response(
            content=serialize_backup(snapshot),
            media_type="application/json; charset=utf-8",
            headers=_attachment_headers(f"{title}-backup.json"),
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
