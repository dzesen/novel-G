"""MongoDB 本地快照、恢复与整书导出。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from bson import json_util

from backend.config import get_config_value
from backend.db.collections import (
    AGENT_DEFINITIONS,
    AGENT_REVISION_PROPOSALS,
    AGENT_RUNS,
    CHAPTERS,
    CHAPTER_STATE_DELTAS,
    CHARACTER_STATE_SNAPSHOTS,
    CHARACTER_STATES,
    CHARACTERS,
    FACTION_RELATIONS,
    FACTIONS,
    GENERATION_JOBS,
    GENERATION_TASKS,
    MANUAL_CORRECTIONS,
    MEMORY_FRAGMENTS,
    NOVELS,
    OUTLINES,
    PLOT_THREADS,
    PLOT_THREAD_EVENTS,
    PROSE_RUNS,
    STATE_PREVIEWS,
    REFERENCE_CARD_PROPOSALS,
    MUTATION_JOURNALS,
    USERS,
    AUTH_LOGIN_ATTEMPTS,
    AUTH_SESSIONS,
    VOLUMES,
    WORLDBOOK,
)
from backend.db.mongo import get_database
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db.repositories.volume_repository import volume_repo
from backend.runtime_reports import write_runtime_report


logger = logging.getLogger(__name__)
BACKUP_FORMAT = "novel-generator-backup"
BACKUP_VERSION = 1
MAX_BACKUP_BYTES = 100 * 1024 * 1024
# 恢复按本元组顺序 delete_many + insert_many，故保持有序且不重复。
# 新增集合务必同步登记，test_backup_covers_every_registered_collection 会守着这条。
BACKUP_COLLECTIONS = (
    USERS,
    AGENT_DEFINITIONS,
    AGENT_RUNS,
    AGENT_REVISION_PROPOSALS,
    NOVELS,
    VOLUMES,
    CHAPTERS,
    OUTLINES,
    GENERATION_TASKS,
    MEMORY_FRAGMENTS,
    CHARACTERS,
    WORLDBOOK,
    FACTIONS,
    FACTION_RELATIONS,
    PLOT_THREADS,
    CHARACTER_STATES,
    GENERATION_JOBS,
    PROSE_RUNS,
    CHAPTER_STATE_DELTAS,
    CHARACTER_STATE_SNAPSHOTS,
    PLOT_THREAD_EVENTS,
    MANUAL_CORRECTIONS,
    STATE_PREVIEWS,
    REFERENCE_CARD_PROPOSALS,
    MUTATION_JOURNALS,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _backup_settings() -> Dict[str, Any]:
    raw = get_config_value("backup", {})
    return raw if isinstance(raw, dict) else {}


def get_backup_directory(*, create: bool = True) -> Path:
    settings = _backup_settings()
    raw_directory = str(settings.get("directory", "backups")).strip() or "backups"
    path = Path(raw_directory)
    if not path.is_absolute():
        path = Path.cwd() / path
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _record_restore_report(payload: Dict[str, Any]) -> None:
    """Diagnostics are best-effort and must never change restore semantics."""
    try:
        write_runtime_report("restore", payload)
    except Exception:
        logger.warning("Unable to write the local restore report.", exc_info=True)


async def create_backup_snapshot() -> Dict[str, Any]:
    db = get_database()
    collections: Dict[str, list[dict]] = {}
    for collection_name in BACKUP_COLLECTIONS:
        collections[collection_name] = await db[collection_name].find({}).to_list(length=None)
    return {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "created_at": _utc_now(),
        "collections": collections,
    }


def serialize_backup(snapshot: Dict[str, Any], *, indent: int | None = 2) -> str:
    return json_util.dumps(snapshot, ensure_ascii=False, indent=indent)


def parse_backup(content: bytes) -> Dict[str, Any]:
    if len(content) > MAX_BACKUP_BYTES:
        raise ValueError("Backup file must not exceed 100 MB")
    try:
        payload = json_util.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Backup file is not valid UTF-8 JSON") from exc
    return validate_backup_payload(payload)


def validate_backup_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Backup root must be an object")
    if payload.get("format") != BACKUP_FORMAT:
        raise ValueError("Unsupported backup format")
    if payload.get("version") != BACKUP_VERSION:
        raise ValueError(f"Unsupported backup version: {payload.get('version')}")
    collections = payload.get("collections")
    if not isinstance(collections, dict):
        raise ValueError("Backup collections are missing")
    unknown = set(collections) - set(BACKUP_COLLECTIONS)
    if unknown:
        raise ValueError(f"Backup contains unsupported collections: {', '.join(sorted(unknown))}")
    for name, documents in collections.items():
        if not isinstance(documents, list) or any(not isinstance(item, dict) for item in documents):
            raise ValueError(f"Backup collection '{name}' must contain a document list")
    return payload


async def _replace_database_collections(collections: Dict[str, list[dict]]) -> None:
    db = get_database()
    for collection_name in BACKUP_COLLECTIONS:
        await db[collection_name].delete_many({})
        documents = collections.get(collection_name, [])
        if documents:
            await db[collection_name].insert_many(documents)


async def restore_backup(payload: Dict[str, Any]) -> Dict[str, Any]:
    validated = validate_backup_payload(payload)
    if validated.get("scope") == "novel":
        raise ValueError("A single-novel export cannot replace the full database")
    safety_snapshot = await create_backup_snapshot()
    safety_path = await save_snapshot_file(safety_snapshot, prefix="pre-restore")
    try:
        await _replace_database_collections(validated["collections"])
    except Exception as exc:
        logger.exception("Backup restore failed; restoring the pre-restore safety snapshot.")
        await _replace_database_collections(safety_snapshot["collections"])
        _record_restore_report(
            {
                "status": "failed_rolled_back",
                "error_type": type(exc).__name__,
                "safety_snapshot": str(safety_path),
            },
        )
        raise
    # 会话与登录失败计数属于运行时认证状态，不进入快照。恢复用户与所有权后
    # 统一清空，防止旧会话命中恢复后的 user_id/session_version，也避免恢复后
    # 无法解释的历史限流继续生效。
    await get_database()[AUTH_SESSIONS].delete_many({})
    await get_database()[AUTH_LOGIN_ATTEMPTS].delete_many({})
    result = {
        name: len(validated["collections"].get(name, []))
        for name in BACKUP_COLLECTIONS
    } | {"safety_snapshot": str(safety_path)}
    _record_restore_report(
        {
            "status": "passed",
            "safety_snapshot": str(safety_path),
            "collection_counts": {
                name: result[name] for name in BACKUP_COLLECTIONS
            },
        },
    )
    return result


async def save_snapshot_file(snapshot: Dict[str, Any], *, prefix: str) -> Path:
    timestamp = _utc_now().strftime("%Y%m%d-%H%M%S-%f")
    path = get_backup_directory() / f"{prefix}-{timestamp}.json"
    serialized = serialize_backup(snapshot)
    await asyncio.to_thread(path.write_text, serialized, encoding="utf-8")
    return path


async def create_automatic_backup_if_due() -> Path | None:
    settings = _backup_settings()
    if settings.get("enabled", True) is False:
        return None
    try:
        directory = get_backup_directory()
        interval_hours = max(1, int(settings.get("interval_hours", 24)))
        retention_count = max(1, int(settings.get("retention_count", 10)))
        existing = sorted(directory.glob("auto-*.json"), key=lambda item: item.stat().st_mtime)
        if existing:
            age_seconds = _utc_now().timestamp() - existing[-1].stat().st_mtime
            if age_seconds < interval_hours * 3600:
                return None

        path = await save_snapshot_file(await create_backup_snapshot(), prefix="auto")
        existing = sorted(directory.glob("auto-*.json"), key=lambda item: item.stat().st_mtime)
        for expired in existing[:-retention_count]:
            await asyncio.to_thread(expired.unlink, missing_ok=True)
        logger.info("Automatic backup created: %s", path)
        return path
    except Exception:
        logger.exception("Automatic backup failed; application startup will continue.")
        return None


async def get_backup_status() -> Dict[str, Any]:
    settings = _backup_settings()
    directory = get_backup_directory(create=False)
    files = (
        sorted(directory.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        if directory.exists()
        else []
    )
    return {
        "enabled": settings.get("enabled", True) is not False,
        "interval_hours": max(1, int(settings.get("interval_hours", 24))),
        "retention_count": max(1, int(settings.get("retention_count", 10))),
        "directory": str(directory),
        "latest_backup": (
            {
                "name": files[0].name,
                "created_at": datetime.fromtimestamp(files[0].stat().st_mtime, tz=timezone.utc),
                "size_bytes": files[0].stat().st_size,
            }
            if files
            else None
        ),
    }


async def build_novel_text(novel_id: str) -> tuple[str, str]:
    novel = await novel_repo.get_novel_by_id(novel_id)
    volumes = await volume_repo.get_volumes_by_novel(novel_id)
    chapters = await chapter_repo.get_chapters_by_novel(novel_id, include_content=True)
    chapters_by_volume: dict[str, list[dict]] = {}
    for chapter in chapters:
        chapters_by_volume.setdefault(str(chapter["volume_id"]), []).append(chapter)

    lines = [str(novel.get("title", "未命名小说"))]
    if novel.get("subtitle"):
        lines.append(str(novel["subtitle"]))
    lines.extend(["", f"类型：{novel.get('genre', '未分类')}"])
    if novel.get("summary"):
        lines.extend(["", str(novel["summary"])])

    for volume in sorted(volumes, key=lambda item: item.get("order_index", 0)):
        lines.extend(["", "", f"# {volume.get('title', '未命名卷')}"])
        if volume.get("summary"):
            lines.extend(["", str(volume["summary"])])
        volume_chapters = sorted(
            chapters_by_volume.get(str(volume["_id"]), []),
            key=lambda item: item.get("order_index", 0),
        )
        for chapter in volume_chapters:
            lines.extend(["", "", f"## {chapter.get('title', '未命名章节')}", ""])
            lines.append(str(chapter.get("content", "")))

    filename = f"{novel.get('title', 'novel')}.txt"
    return filename, "\n".join(lines).strip() + "\n"


async def build_novel_backup(novel_id: str) -> Dict[str, Any]:
    novel = await novel_repo.get_novel_by_id(novel_id)
    db = get_database()
    exported: Dict[str, list[dict]] = {NOVELS: [novel]}
    for collection_name in BACKUP_COLLECTIONS:
        if collection_name == NOVELS:
            continue
        exported[collection_name] = await db[collection_name].find(
            {"novel_id": novel["_id"]}
        ).to_list(length=None)
    return {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "scope": "novel",
        "created_at": _utc_now(),
        "collections": exported,
    }
