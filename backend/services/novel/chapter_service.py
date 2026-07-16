"""章节业务服务：维护章节、卷与小说之间的统计一致性。"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from backend.db.errors import DuplicateKeyError, NotFoundError
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db.repositories.volume_repository import volume_repo
from backend.db.transaction import run_mongo_write_unit
from backend.db.utils import to_object_id


_WORD_TOKEN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]|[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*")
VALID_CHAPTER_STATUSES = {"draft", "writing", "completed"}


def count_chapter_words(content: str) -> int:
    """按中文单字、英文单词和数字词组统计正文有效字数。"""
    return len(_WORD_TOKEN_RE.findall(content or ""))


class ChapterService:
    @staticmethod
    async def _validate_scope(novel_id: str, volume_id: str, session=None) -> None:
        await novel_repo.get_novel_by_id(novel_id, session=session)
        volume = await volume_repo.get_volume_by_id(volume_id, session=session)
        if volume["novel_id"] != to_object_id(novel_id):
            raise ValueError("Volume does not belong to the specified novel")

    @staticmethod
    async def create_chapter(data: Dict[str, Any]) -> str:
        novel_id = str(data.get("novel_id", ""))
        volume_id = str(data.get("volume_id", ""))
        auto_order = data.get("order_index") is None
        content = str(data.get("content", ""))
        prepared = {**data, "content": content, "word_count": count_chapter_words(content)}
        if prepared.get("status", "draft") not in VALID_CHAPTER_STATUSES:
            raise ValueError(f"Invalid chapter status: {prepared.get('status')}")

        async def _create(session):
            await ChapterService._validate_scope(novel_id, volume_id, session=session)
            chapter_id = await chapter_repo.create_chapter(prepared, session=session)
            await volume_repo.update_volume_stats(
                volume_id,
                chapter_count_delta=1,
                word_count_delta=prepared["word_count"],
                session=session,
            )
            await novel_repo.increment_novel_stats(
                novel_id,
                {
                    "current_chapter_count": 1,
                    "current_word_count": prepared["word_count"],
                },
                session=session,
            )
            return chapter_id

        try:
            return await run_mongo_write_unit(_create, "create_chapter")
        except DuplicateKeyError:
            if not auto_order:
                raise
            return await run_mongo_write_unit(_create, "create_chapter_retry")

    @staticmethod
    async def get_chapter(chapter_id: str) -> Dict[str, Any]:
        return await chapter_repo.get_chapter_by_id(chapter_id)

    @staticmethod
    async def get_chapters_by_novel(novel_id: str) -> List[Dict[str, Any]]:
        await novel_repo.get_novel_by_id(novel_id)
        return await chapter_repo.get_chapters_by_novel(novel_id)

    @staticmethod
    async def get_deleted_chapters(novel_id: str) -> List[Dict[str, Any]]:
        await novel_repo.get_novel_by_id(novel_id)
        chapters = await chapter_repo.get_chapters_by_novel(
            novel_id,
            include_deleted=True,
        )
        return [chapter for chapter in chapters if chapter.get("is_deleted")]

    @staticmethod
    async def get_chapters_by_volume(volume_id: str) -> List[Dict[str, Any]]:
        await volume_repo.get_volume_by_id(volume_id)
        return await chapter_repo.get_chapters_by_volume(volume_id)

    @staticmethod
    async def update_chapter(chapter_id: str, update_data: Dict[str, Any]) -> bool:
        if "status" in update_data and update_data["status"] not in VALID_CHAPTER_STATUSES:
            raise ValueError(f"Invalid chapter status: {update_data['status']}")

        async def _update(session):
            chapter = await chapter_repo.get_chapter_by_id(chapter_id, session=session)
            prepared = dict(update_data)
            previous_words = int(chapter.get("word_count", 0))
            if "content" in prepared:
                prepared["content"] = str(prepared["content"])
                prepared["word_count"] = count_chapter_words(prepared["content"])
            next_words = int(prepared.get("word_count", previous_words))
            delta = next_words - previous_words

            success = await chapter_repo.update_chapter(chapter_id, prepared, session=session)
            if success and delta:
                await volume_repo.update_volume_stats(
                    str(chapter["volume_id"]),
                    word_count_delta=delta,
                    session=session,
                )
                await novel_repo.increment_novel_stats(
                    str(chapter["novel_id"]),
                    {"current_word_count": delta},
                    session=session,
                )
            return success

        return await run_mongo_write_unit(_update, "update_chapter")

    @staticmethod
    async def soft_delete_chapter(chapter_id: str) -> bool:
        async def _delete(session):
            chapter = await chapter_repo.get_chapter_by_id(chapter_id, session=session)
            success = await chapter_repo.soft_delete_chapter(chapter_id, session=session)
            if success:
                word_count = int(chapter.get("word_count", 0))
                await volume_repo.update_volume_stats(
                    str(chapter["volume_id"]),
                    chapter_count_delta=-1,
                    word_count_delta=-word_count,
                    session=session,
                )
                await novel_repo.increment_novel_stats(
                    str(chapter["novel_id"]),
                    {"current_chapter_count": -1, "current_word_count": -word_count},
                    session=session,
                )
            return success

        return await run_mongo_write_unit(_delete, "soft_delete_chapter")

    @staticmethod
    async def restore_chapter(chapter_id: str) -> bool:
        async def _restore(session):
            chapter = await chapter_repo.get_chapter_by_id(
                chapter_id,
                include_deleted=True,
                session=session,
            )
            if not chapter.get("is_deleted"):
                raise ValueError("Chapter is not in deleted state")
            await ChapterService._validate_scope(
                str(chapter["novel_id"]),
                str(chapter["volume_id"]),
                session=session,
            )
            success = await chapter_repo.restore_chapter(chapter_id, session=session)
            if success:
                word_count = int(chapter.get("word_count", 0))
                await volume_repo.update_volume_stats(
                    str(chapter["volume_id"]),
                    chapter_count_delta=1,
                    word_count_delta=word_count,
                    session=session,
                )
                await novel_repo.increment_novel_stats(
                    str(chapter["novel_id"]),
                    {"current_chapter_count": 1, "current_word_count": word_count},
                    session=session,
                )
            return success

        return await run_mongo_write_unit(_restore, "restore_chapter")

    @staticmethod
    async def hard_delete_chapter(chapter_id: str) -> bool:
        async def _delete(session):
            chapter = await chapter_repo.get_chapter_by_id(
                chapter_id,
                include_deleted=True,
                session=session,
            )
            if not chapter.get("is_deleted"):
                raise ValueError("Only soft-deleted chapters can be permanently deleted")
            return await chapter_repo.hard_delete_chapter(chapter_id, session=session)

        return await run_mongo_write_unit(_delete, "hard_delete_chapter")
