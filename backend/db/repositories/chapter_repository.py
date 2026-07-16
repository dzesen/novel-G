"""章节仓储：负责章节文档的集合内 CRUD 与顺序约束。"""

from __future__ import annotations

from typing import Any, Dict, List

import pymongo.errors
from pymongo.asynchronous.client_session import AsyncClientSession

from backend.db.base import BaseRepository
from backend.db.collections import CHAPTERS
from backend.db.errors import DuplicateKeyError, NotFoundError
from backend.db.utils import to_object_id


class ChapterRepository(BaseRepository):
    def __init__(self) -> None:
        super().__init__(CHAPTERS)

    async def _get_next_order_index(
        self,
        volume_id,
        session: AsyncClientSession | None = None,
    ) -> int:
        cursor = self.collection.find(
            {"volume_id": volume_id, "is_deleted": False},
            projection={"order_index": 1},
            session=session,
        ).sort("order_index", -1).limit(1)
        docs = await cursor.to_list(length=1)
        return docs[0].get("order_index", 0) + 1 if docs else 1

    async def create_chapter(
        self,
        data: Dict[str, Any],
        session: AsyncClientSession | None = None,
    ) -> str:
        if not data.get("novel_id"):
            raise ValueError("novel_id is required")
        if not data.get("volume_id"):
            raise ValueError("volume_id is required")
        if not str(data.get("title", "")).strip():
            raise ValueError("Chapter title cannot be empty")

        prepared = dict(data)
        prepared["novel_id"] = to_object_id(prepared["novel_id"])
        prepared["volume_id"] = to_object_id(prepared["volume_id"])
        prepared["title"] = str(prepared["title"]).strip()
        if prepared.get("order_index") is None:
            prepared["order_index"] = await self._get_next_order_index(
                prepared["volume_id"],
                session=session,
            )
        if int(prepared["order_index"]) < 1:
            raise ValueError("order_index must be greater than 0")

        prepared.setdefault("summary", "")
        prepared.setdefault("content", "")
        prepared.setdefault("status", "draft")
        prepared.setdefault("word_count", 0)

        try:
            return await self.insert_one(prepared, session=session)
        except pymongo.errors.DuplicateKeyError as exc:
            raise DuplicateKeyError(
                f"同一卷下 order_index={prepared['order_index']} 已存在"
            ) from exc

    async def get_chapter_by_id(
        self,
        chapter_id: str,
        *,
        include_deleted: bool = False,
        session: AsyncClientSession | None = None,
    ) -> Dict[str, Any]:
        chapter = await self.find_one(
            {"_id": to_object_id(chapter_id)},
            include_deleted=include_deleted,
            session=session,
        )
        if not chapter:
            raise NotFoundError(f"Chapter with id {chapter_id} not found")
        return chapter

    async def _list_chapters(
        self,
        query: Dict[str, Any],
        *,
        include_deleted: bool = False,
        include_content: bool = False,
        session: AsyncClientSession | None = None,
    ) -> List[Dict[str, Any]]:
        prepared = dict(query)
        if not include_deleted:
            prepared["is_deleted"] = False
        projection = None if include_content else {"content": 0}
        cursor = self.collection.find(prepared, projection=projection, session=session).sort(
            [("volume_id", 1), ("order_index", 1)]
        )
        return await cursor.to_list(length=None)

    async def get_chapters_by_novel(
        self,
        novel_id: str,
        *,
        include_deleted: bool = False,
        include_content: bool = False,
        session: AsyncClientSession | None = None,
    ) -> List[Dict[str, Any]]:
        return await self._list_chapters(
            {"novel_id": to_object_id(novel_id)},
            include_deleted=include_deleted,
            include_content=include_content,
            session=session,
        )

    async def get_chapters_by_volume(
        self,
        volume_id: str,
        *,
        session: AsyncClientSession | None = None,
    ) -> List[Dict[str, Any]]:
        return await self._list_chapters(
            {"volume_id": to_object_id(volume_id)},
            session=session,
        )

    async def update_chapter(
        self,
        chapter_id: str,
        update_data: Dict[str, Any],
        session: AsyncClientSession | None = None,
    ) -> bool:
        allowed = {"title", "summary", "content", "status", "order_index", "word_count"}
        filtered = {key: value for key, value in update_data.items() if key in allowed}
        if not filtered:
            return False
        if "title" in filtered:
            filtered["title"] = str(filtered["title"]).strip()
            if not filtered["title"]:
                raise ValueError("Chapter title cannot be empty")
        if "order_index" in filtered and int(filtered["order_index"]) < 1:
            raise ValueError("order_index must be greater than 0")

        try:
            return await self.update_one(
                {"_id": to_object_id(chapter_id)},
                filtered,
                session=session,
            )
        except pymongo.errors.DuplicateKeyError as exc:
            raise DuplicateKeyError("同一卷下已有相同章节序号") from exc

    async def soft_delete_chapter(
        self,
        chapter_id: str,
        session: AsyncClientSession | None = None,
    ) -> bool:
        return await self.soft_delete_one({"_id": to_object_id(chapter_id)}, session=session)

    async def restore_chapter(
        self,
        chapter_id: str,
        session: AsyncClientSession | None = None,
    ) -> bool:
        try:
            return await self.restore_one({"_id": to_object_id(chapter_id)}, session=session)
        except pymongo.errors.DuplicateKeyError as exc:
            raise DuplicateKeyError("同一卷下已有相同章节序号，无法恢复") from exc

    async def hard_delete_chapter(
        self,
        chapter_id: str,
        session: AsyncClientSession | None = None,
    ) -> bool:
        return await self.hard_delete_one({"_id": to_object_id(chapter_id)}, session=session)


chapter_repo = ChapterRepository()
