"""Monotonic, idempotent narrative revision storage for generation leases."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from bson import ObjectId
from pymongo import ReturnDocument

from backend.db import collections
from backend.db.mongo import get_database
from backend.db.utils import get_utc_now, to_object_id


class NarrativeRevisionConflict(ValueError):
    """A context mutation was prepared against a stale narrative revision."""


class NarrativeRevisionFenceConflict(NarrativeRevisionConflict):
    """A narrative write fence could not be acquired."""


class NarrativeRevisionStore:
    @staticmethod
    def _persistent_mutation_fence_matches(
        actual: dict[str, Any],
        expected: dict[str, Any],
    ) -> bool:
        fields = (
            "token",
            "resource_kind",
            "resource_id",
            "idempotency_key",
            "command_digest",
            "operation",
            "operation_key",
        )
        return all(actual.get(field) == expected.get(field) for field in fields)

    async def advance_with_persistent_mutation_fence(
        self,
        novel_id: str,
        operation_id: str,
        *,
        expected_revision: int,
        fence_token: str,
        journal_id: str,
        idempotency_key: str,
        command_digest: str,
        operation: str,
    ) -> tuple[int, dict[str, Any]]:
        """Atomically advance once and hold a no-TTL standalone mutation fence."""
        required = {
            "operation_id": operation_id,
            "fence_token": fence_token,
            "journal_id": journal_id,
            "idempotency_key": idempotency_key,
            "command_digest": command_digest,
            "operation": operation,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise ValueError(
                "persistent mutation fence identity is incomplete: "
                + ", ".join(sorted(missing))
            )
        if not ObjectId.is_valid(str(journal_id)):
            raise ValueError("journal_id must be a valid ObjectId")
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise ValueError("expected_revision must be a non-negative integer")
        operation_key = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        marker = f"narrative_revision_operations.{operation_key}"
        now = get_utc_now()
        fence = {
            "token": str(fence_token),
            "resource_kind": "mutation_journal",
            "resource_id": str(journal_id),
            "idempotency_key": str(idempotency_key),
            "command_digest": str(command_digest),
            "operation": str(operation),
            "operation_key": operation_key,
            "installed_at": now,
        }
        novel = await get_database()[collections.NOVELS].find_one_and_update(
            {
                "_id": to_object_id(novel_id),
                marker: {"$exists": False},
                "$expr": {
                    "$eq": [
                        {"$ifNull": ["$narrative_revision", 0]},
                        int(expected_revision),
                    ]
                },
                "$or": [
                    {"narrative_write_fence": {"$exists": False}},
                    {"narrative_write_fence": None},
                ],
            },
            {
                "$inc": {"narrative_revision": 1},
                "$set": {
                    marker: now,
                    "narrative_write_fence": fence,
                },
            },
            return_document=ReturnDocument.AFTER,
        )
        if novel is not None:
            return int(novel.get("narrative_revision") or 0), fence

        current = await get_database()[collections.NOVELS].find_one(
            {"_id": to_object_id(novel_id)},
            projection={
                "narrative_revision": 1,
                "narrative_revision_operations": 1,
                "narrative_write_fence": 1,
            },
        )
        if current is None:
            raise ValueError(f"Novel {novel_id} does not exist")
        current_fence = dict(current.get("narrative_write_fence") or {})
        current_revision = int(current.get("narrative_revision") or 0)
        operation_was_applied = operation_key in (
            current.get("narrative_revision_operations") or {}
        )
        if (
            operation_was_applied
            and current_revision == expected_revision + 1
            and self._persistent_mutation_fence_matches(current_fence, fence)
        ):
            return current_revision, current_fence
        raise NarrativeRevisionFenceConflict(
            "Narrative revision changed or is fenced before the persistent mutation"
        )

    async def release_persistent_mutation_fence(
        self,
        novel_id: str,
        *,
        fence: dict[str, Any],
    ) -> bool:
        """Release only the exact no-TTL fence supplied by its mutation journal."""
        return await self._release_completed_persistent_mutation_fence(
            novel_id,
            fence,
        )

    async def _release_completed_persistent_mutation_fence(
        self,
        novel_id: str,
        fence: dict[str, Any],
        *,
        session: Any = None,
    ) -> bool:
        """Recover the completed-journal-before-fence-clear crash window."""
        if fence.get("resource_kind") != "mutation_journal":
            return False
        journal_id = str(fence.get("resource_id") or "")
        idempotency_key = str(fence.get("idempotency_key") or "")
        command_digest = str(fence.get("command_digest") or "")
        operation = str(fence.get("operation") or "")
        operation_key = str(fence.get("operation_key") or "")
        if (
            not ObjectId.is_valid(journal_id)
            or not idempotency_key
            or not command_digest
            or not operation
            or not operation_key
        ):
            return False
        journal = await get_database()[collections.MUTATION_JOURNALS].find_one(
            {
                "_id": ObjectId(journal_id),
                "novel_id": to_object_id(novel_id),
                "idempotency_key": idempotency_key,
                "command_digest": command_digest,
                "operation": operation,
                "status": "completed",
            },
            projection={"operation": 1, "command": 1},
            session=session,
        )
        if journal is None:
            return False
        command = dict(journal.get("command") or {})
        version = command.get("version")
        expected_revision = command.get("expected_narrative_revision")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            return False
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            return False
        operation_id = (
            f"{operation}@{version}:{idempotency_key}:{command_digest}"
        )
        if hashlib.sha256(operation_id.encode("utf-8")).hexdigest() != operation_key:
            return False
        result = await get_database()[collections.NOVELS].update_one(
            {
                "_id": to_object_id(novel_id),
                **{
                    f"narrative_write_fence.{field}": fence.get(field)
                    for field in (
                        "token",
                        "resource_kind",
                        "resource_id",
                        "idempotency_key",
                        "command_digest",
                        "operation",
                        "operation_key",
                    )
                },
                f"narrative_revision_operations.{operation_key}": {
                    "$exists": True
                },
                "$expr": {
                    "$eq": [
                        {"$ifNull": ["$narrative_revision", 0]},
                        expected_revision + 1,
                    ]
                },
            },
            {"$unset": {"narrative_write_fence": ""}},
            session=session,
        )
        return result.modified_count == 1

    async def acquire_write_fence(
        self,
        novel_id: str,
        *,
        expected_revision: int,
        fence_token: str,
        resource_kind: str,
        resource_id: str,
        ttl_seconds: int = 30,
    ) -> datetime:
        """Fence context writers while one candidate CAS validates its basis."""
        if not fence_token:
            raise ValueError("fence_token is required")
        if resource_kind != "prose_run" or not resource_id:
            raise ValueError("a prose_run fence resource is required")
        now = get_utc_now()
        expires_at = now + timedelta(seconds=max(1, int(ttl_seconds)))
        novel = await get_database()[collections.NOVELS].find_one_and_update(
            {
                "_id": to_object_id(novel_id),
                "$expr": {
                    "$eq": [
                        {"$ifNull": ["$narrative_revision", 0]},
                        int(expected_revision),
                    ]
                },
                "$or": [
                    {"narrative_write_fence": {"$exists": False}},
                    {"narrative_write_fence": None},
                    {"narrative_write_fence.expires_at": {"$lte": now}},
                    {
                        "$and": [
                            {
                                "narrative_write_fence.token": str(
                                    fence_token
                                )
                            },
                            {"narrative_write_fence.resource_kind": "prose_run"},
                            {
                                "narrative_write_fence.resource_id": str(
                                    resource_id
                                )
                            },
                            {
                                "narrative_write_fence.expires_at": {
                                    "$exists": True
                                }
                            },
                        ]
                    },
                ],
            },
            {
                "$set": {
                    "narrative_write_fence": {
                        "token": str(fence_token),
                        "expires_at": expires_at,
                        "resource_kind": str(resource_kind),
                        "resource_id": str(resource_id),
                    }
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if novel is None:
            raise NarrativeRevisionFenceConflict(
                "Narrative revision changed before the candidate mutation"
            )
        return expires_at

    async def release_write_fence(
        self,
        novel_id: str,
        *,
        fence_token: str,
    ) -> None:
        if not fence_token:
            return
        await get_database()[collections.NOVELS].update_one(
            {
                "_id": to_object_id(novel_id),
                "narrative_write_fence.token": str(fence_token),
                "narrative_write_fence.resource_kind": "prose_run",
                "narrative_write_fence.expires_at": {"$exists": True},
            },
            {"$unset": {"narrative_write_fence": ""}},
        )

    async def current(self, novel_id: str, *, session: Any = None) -> int:
        novel = await get_database()[collections.NOVELS].find_one(
            {"_id": to_object_id(novel_id)},
            projection={"narrative_revision": 1},
            session=session,
        )
        if novel is None:
            raise ValueError(f"Novel {novel_id} does not exist")
        return int(novel.get("narrative_revision") or 0)

    async def advance(
        self,
        novel_id: str,
        operation_id: str,
        *,
        expected_revision: int | None = None,
        session: Any = None,
    ) -> int:
        if not operation_id:
            raise ValueError("operation_id is required to advance narrative revision")
        operation_key = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        marker = f"narrative_revision_operations.{operation_key}"
        now = get_utc_now()
        query: dict[str, Any] = {
            "_id": to_object_id(novel_id),
            marker: {"$exists": False},
            "$or": [
                {"narrative_write_fence": {"$exists": False}},
                {"narrative_write_fence": None},
            ],
        }
        if expected_revision is not None:
            query["$expr"] = {
                "$eq": [
                    {"$ifNull": ["$narrative_revision", 0]},
                    int(expected_revision),
                ]
            }
        novel = await get_database()[collections.NOVELS].find_one_and_update(
            query,
            {
                "$inc": {"narrative_revision": 1},
                "$set": {marker: now},
            },
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if novel is not None:
            return int(novel.get("narrative_revision") or 0)
        current = await get_database()[collections.NOVELS].find_one(
            {"_id": to_object_id(novel_id)},
            projection={
                "narrative_revision": 1,
                "narrative_revision_operations": 1,
                "narrative_write_fence": 1,
            },
            session=session,
        )
        if current is None:
            raise ValueError(f"Novel {novel_id} does not exist")
        operation_was_applied = operation_key in (
            current.get("narrative_revision_operations") or {}
        )
        fence = dict(current.get("narrative_write_fence") or {})
        if not fence:
            if operation_was_applied:
                return int(current.get("narrative_revision") or 0)
            raise NarrativeRevisionConflict(
                "Narrative revision changed before the authorized mutation"
            )
        if fence.get("resource_kind") == "mutation_journal":
            released = await self._release_completed_persistent_mutation_fence(
                novel_id,
                fence,
                session=session,
            )
            if released:
                if operation_was_applied:
                    return int(current.get("narrative_revision") or 0)
                novel = await get_database()[collections.NOVELS].find_one_and_update(
                    query,
                    {
                        "$inc": {"narrative_revision": 1},
                        "$set": {marker: now},
                    },
                    return_document=ReturnDocument.AFTER,
                    session=session,
                )
                if novel is not None:
                    return int(novel.get("narrative_revision") or 0)
                current = await get_database()[collections.NOVELS].find_one(
                    {"_id": to_object_id(novel_id)},
                    projection={
                        "narrative_revision": 1,
                        "narrative_revision_operations": 1,
                    },
                    session=session,
                )
                if current is not None and operation_key in (
                    current.get("narrative_revision_operations") or {}
                ):
                    return int(current.get("narrative_revision") or 0)
            raise NarrativeRevisionConflict(
                "Narrative revision changed or is fenced before the authorized mutation"
            )
        expires_at = fence.get("expires_at")
        if not fence or not isinstance(expires_at, datetime) or expires_at > now:
            raise NarrativeRevisionConflict(
                "Narrative revision changed or is fenced before the authorized mutation"
            )

        # Revoke the resource-local token before an expired global fence can
        # be cleared. Therefore an old candidate CAS either wins before this
        # author mutation or fails its own token query afterwards; it cannot
        # land against the newly advanced narrative revision.
        if (
            fence.get("resource_kind") == "prose_run"
            and str(fence.get("resource_id") or "")
        ):
            await get_database()[collections.PROSE_RUNS].update_one(
                {
                    "_id": to_object_id(str(fence["resource_id"])),
                    "remediation_write_fence.token": str(
                        fence.get("token") or ""
                    ),
                },
                {"$unset": {"remediation_write_fence": ""}},
                session=session,
            )

        if operation_was_applied:
            cleared = await get_database()[collections.NOVELS].update_one(
                {
                    "_id": to_object_id(novel_id),
                    "narrative_write_fence.token": str(
                        fence.get("token") or ""
                    ),
                    "narrative_write_fence.expires_at": expires_at,
                },
                {"$unset": {"narrative_write_fence": ""}},
                session=session,
            )
            if cleared.modified_count == 1:
                return int(current.get("narrative_revision") or 0)
            raise NarrativeRevisionConflict(
                "Narrative revision changed or is fenced before the authorized mutation"
            )

        expired_query: dict[str, Any] = {
            "_id": to_object_id(novel_id),
            marker: {"$exists": False},
            "narrative_write_fence.token": str(fence.get("token") or ""),
            "narrative_write_fence.expires_at": expires_at,
        }
        if expected_revision is not None:
            expired_query["$expr"] = {
                "$eq": [
                    {"$ifNull": ["$narrative_revision", 0]},
                    int(expected_revision),
                ]
            }
        novel = await get_database()[collections.NOVELS].find_one_and_update(
            expired_query,
            {
                "$inc": {"narrative_revision": 1},
                "$set": {marker: now},
                "$unset": {"narrative_write_fence": ""},
            },
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if novel is not None:
            return int(novel.get("narrative_revision") or 0)
        current = await get_database()[collections.NOVELS].find_one(
            {"_id": to_object_id(novel_id)},
            projection={"narrative_revision": 1, "narrative_revision_operations": 1},
            session=session,
        )
        if current is not None and operation_key in (
            current.get("narrative_revision_operations") or {}
        ):
            return int(current.get("narrative_revision") or 0)
        raise NarrativeRevisionConflict(
            "Narrative revision changed or is fenced before the authorized mutation"
        )


narrative_revision_store = NarrativeRevisionStore()
