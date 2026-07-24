from __future__ import annotations

from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

from backend.db import collections
from backend.db.errors import NotFoundError
from backend.db.mongo import get_database
from backend.db.utils import to_object_id
from backend.services.auth.identity_service import Actor


class NovelAccessService:
    """以小说为授权根，隐藏所有权查询和间接资源解析。"""

    def __init__(self, database: AsyncDatabase | None = None):
        self._database = database

    @property
    def db(self) -> AsyncDatabase:
        return self._database if self._database is not None else get_database()

    async def require_owned_novel(
        self,
        actor: Actor,
        novel_id: str,
        *,
        include_deleted: bool = False,
    ) -> dict[str, Any]:
        object_id = to_object_id(novel_id)
        query: dict[str, Any] = {
            "_id": object_id,
            "owner_id": to_object_id(actor.id),
        }
        if not include_deleted:
            query["is_deleted"] = False
        novel = await self.db[collections.NOVELS].find_one(query)
        if not novel:
            raise NotFoundError(f"Novel with id {novel_id} not found")
        return novel

    async def resolve_owned_resource(
        self,
        actor: Actor,
        *,
        resource_kind: str,
        resource_id: str,
    ) -> dict[str, Any]:
        collection_by_kind = {
            "volume": collections.VOLUMES,
            "chapter": collections.CHAPTERS,
            "job": collections.GENERATION_JOBS,
            "state_proposal": collections.STATE_PREVIEWS,
        }
        collection_name = collection_by_kind.get(resource_kind)
        if collection_name is None:
            raise ValueError(f"Unsupported resource kind: {resource_kind}")
        object_id = to_object_id(resource_id)
        resource = await self.db[collection_name].find_one({"_id": object_id})
        if not resource or resource.get("novel_id") is None:
            raise NotFoundError(f"{resource_kind} with id {resource_id} not found")
        return await self.require_owned_novel(
            actor,
            str(resource["novel_id"]),
            include_deleted=True,
        )


_novel_access_service = NovelAccessService()


def get_novel_access_service() -> NovelAccessService:
    return _novel_access_service
