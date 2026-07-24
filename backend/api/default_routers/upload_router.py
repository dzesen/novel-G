from __future__ import annotations

import hashlib
import os

from fastapi import APIRouter, Depends, File, UploadFile, HTTPException
from backend.api.default_routers.auth_router import require_authenticated_request

router = APIRouter(
    prefix="/api/upload",
    tags=["upload"],
    dependencies=[Depends(require_authenticated_request)],
)

COVER_DIR = "static/covers"
MAX_COVER_BYTES = 2 * 1024 * 1024
os.makedirs(COVER_DIR, exist_ok=True)


def _detect_image_extension(content: bytes) -> str | None:
    """按文件签名识别允许作为封面的安全位图格式。"""
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    return None

@router.post("/cover")
async def upload_cover(file: UploadFile = File(...)):
    """上传小说封面图片，返回图片的URL地址。"""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File provided is not an image.")

    content = await file.read(MAX_COVER_BYTES + 1)
    if len(content) > MAX_COVER_BYTES:
        raise HTTPException(status_code=413, detail="Cover image must not exceed 2 MB.")
    if not content:
        raise HTTPException(status_code=400, detail="Cover image is empty.")

    ext = _detect_image_extension(content)
    if ext is None:
        raise HTTPException(
            status_code=400,
            detail="Unsupported image format. Use JPEG, PNG, GIF, or WebP.",
        )
    
    file_hash = hashlib.sha256(content).hexdigest()
    filename = f"{file_hash}{ext}"
    filepath = os.path.join(COVER_DIR, filename)
    
    if not os.path.exists(filepath):
        with open(filepath, "wb") as f:
            f.write(content)
            
    cover_url = f"/static/covers/{filename}"
    return {"url": cover_url, "message": "Image uploaded successfully"}
