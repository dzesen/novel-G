import hashlib
import json
from typing import Tuple, Dict, Any

from backend.db.mutation import MutationCommand, commit_mutation
from backend.db.repositories.novel_repository import novel_repo
from backend.db import collections
from backend.db.base import BaseRepository
from backend.db.errors import NotFoundError
from backend.db.transaction import run_mongo_write_unit
from backend.db.utils import to_object_id

class NovelService:
    CONTEXT_FIELDS = frozenset(
        {
            "core_seed",
            "worldview",
            "writing_style",
            "narrative_pov",
            "tone",
            "era_background",
        }
    )

    @staticmethod
    async def _execute_update_novel_info(session, mutation):
        command = mutation.journal["command"]["payload"]
        await novel_repo.update_novel_info(
            str(mutation.journal["novel_id"]),
            command["changes"],
            session=session,
        )
        return True

    @staticmethod
    async def update_novel_info(novel_id: str, update_data: Dict[str, Any]) -> bool:
        current = await novel_repo.get_novel_by_id(novel_id)
        protected = {"_id", "created_at", "updated_at", "is_deleted", "deleted_at"}
        changes = {
            key: value
            for key, value in update_data.items()
            if key not in protected and current.get(key) != value
        }
        if not changes:
            return False
        affects_context = bool(set(changes) & NovelService.CONTEXT_FIELDS)
        operation = (
            "update_novel_context" if affects_context else "update_novel_metadata"
        )
        digest = hashlib.sha256(
            json.dumps(changes, ensure_ascii=False, sort_keys=True, default=str).encode(
                "utf-8"
            )
        ).hexdigest()
        return await commit_mutation(
            MutationCommand(
                novel_id=novel_id,
                idempotency_key=(
                    f"update-novel-info:{novel_id}:{current.get('updated_at')}:{digest}"
                ),
                operation=operation,
                payload={"changes": changes},
                before_image={key: current.get(key) for key in changes},
            ),
            NovelService._execute_update_novel_info,
            advances_narrative_revision=affects_context,
        )

    @staticmethod
    async def _execute_novel_lifecycle(session, mutation):
        novel_id = str(mutation.journal["novel_id"])
        operation = str(mutation.journal["operation"])
        current = await novel_repo.get_novel_by_id(
            novel_id, include_deleted=True, session=session
        )
        if operation == "soft_delete_novel":
            if not current.get("is_deleted"):
                await novel_repo.soft_delete_novel(novel_id, session=session)
            return True
        if operation == "restore_novel":
            if current.get("is_deleted"):
                await novel_repo.restore_novel(novel_id, session=session)
            return True
        raise ValueError(f"Unsupported novel lifecycle mutation: {operation}")

    @staticmethod
    async def soft_delete_novel(novel_id: str) -> bool:
        current = await novel_repo.get_novel_by_id(novel_id)
        return await NovelService._commit_lifecycle(
            novel_id, current, "soft_delete_novel"
        )

    @staticmethod
    async def restore_novel(novel_id: str) -> bool:
        current = await novel_repo.get_novel_by_id(novel_id, include_deleted=True)
        if not current.get("is_deleted"):
            raise ValueError("Only deleted novels can be restored")
        return await NovelService._commit_lifecycle(novel_id, current, "restore_novel")

    @staticmethod
    async def _commit_lifecycle(
        novel_id: str, current: Dict[str, Any], operation: str
    ) -> bool:
        return await commit_mutation(
            MutationCommand(
                novel_id=novel_id,
                idempotency_key=f"{operation}:{novel_id}:{current.get('updated_at')}",
                operation=operation,
                payload={},
                before_image={
                    "is_deleted": bool(current.get("is_deleted")),
                    "updated_at": current.get("updated_at"),
                },
            ),
            NovelService._execute_novel_lifecycle,
        )

    @staticmethod
    async def check_novel_before_delete(novel_id: str) -> Tuple[bool, str]:
        """
        检查小说是否可以安全进行物理删除。
        返回：(是否安全, 提示信息)
        """
        try:
            # 首先检查小说是否存在（包括已软删除的）
            novel = await novel_repo.get_novel_by_id(novel_id, include_deleted=True)
            
            # 只有当小说已软删除时才允许物理删除
            if not novel.get("is_deleted", False):
                return False, "Only soft-deleted novels can be permanently deleted"
            
            return True, "Safe to delete"
        except Exception as e:
            return False, str(e)

    @staticmethod
    async def hard_delete_novel(novel_id: str) -> Dict[str, Any]:
        """
        物理删除小说及其所有关联记录。
        **仅允许用于整本小说永久删除**
        """
        async def _delete(session):
            """在同一个写入单元内删除小说及所有已知子集合。"""
            try:
                novel = await novel_repo.get_novel_by_id(novel_id, include_deleted=True, session=session)
            except NotFoundError as exc:
                raise ValueError(f"Cannot hard delete novel: {exc}") from exc

            if not novel.get("is_deleted", False):
                raise ValueError("Cannot hard delete novel: Only soft-deleted novels can be permanently deleted")

            obj_id = to_object_id(novel_id)
            query = {"novel_id": obj_id}

            volumes_repo = BaseRepository(collections.VOLUMES)
            chapters_repo = BaseRepository(collections.CHAPTERS)
            outlines_repo = BaseRepository(collections.OUTLINES)
            tasks_repo = BaseRepository(collections.GENERATION_TASKS)
            memories_repo = BaseRepository(collections.MEMORY_FRAGMENTS)
            factions_repo = BaseRepository(collections.FACTIONS)
            faction_relations_repo = BaseRepository(collections.FACTION_RELATIONS)
            characters_repo = BaseRepository(collections.CHARACTERS)
            worldbook_repo = BaseRepository(collections.WORLDBOOK)
            plot_threads_repo = BaseRepository(collections.PLOT_THREADS)
            character_states_repo = BaseRepository(collections.CHARACTER_STATES)
            generation_jobs_repo = BaseRepository(collections.GENERATION_JOBS)
            chapter_state_deltas_repo = BaseRepository(collections.CHAPTER_STATE_DELTAS)
            character_state_snapshots_repo = BaseRepository(collections.CHARACTER_STATE_SNAPSHOTS)
            plot_thread_events_repo = BaseRepository(collections.PLOT_THREAD_EVENTS)
            manual_corrections_repo = BaseRepository(collections.MANUAL_CORRECTIONS)
            state_previews_repo = BaseRepository(collections.STATE_PREVIEWS)
            mutation_journals_repo = BaseRepository(collections.MUTATION_JOURNALS)

            stats = {}

            # 这些集合当前有的还是空仓储，统一按 novel_id 清理即可。
            stats["volumes_deleted"] = await volumes_repo.hard_delete_many(query, session=session)
            stats["chapters_deleted"] = await chapters_repo.hard_delete_many(query, session=session)
            stats["outlines_deleted"] = await outlines_repo.hard_delete_many(query, session=session)
            stats["tasks_deleted"] = await tasks_repo.hard_delete_many(query, session=session)
            stats["memories_deleted"] = await memories_repo.hard_delete_many(query, session=session)
            stats["faction_relations_deleted"] = await faction_relations_repo.hard_delete_many(query, session=session)
            stats["factions_deleted"] = await factions_repo.hard_delete_many(query, session=session)
            stats["characters_deleted"] = await characters_repo.hard_delete_many(query, session=session)
            stats["worldbook_deleted"] = await worldbook_repo.hard_delete_many(query, session=session)
            stats["plot_threads_deleted"] = await plot_threads_repo.hard_delete_many(query, session=session)
            stats["character_states_deleted"] = await character_states_repo.hard_delete_many(query, session=session)
            stats["generation_jobs_deleted"] = await generation_jobs_repo.hard_delete_many(query, session=session)
            stats["chapter_state_deltas_deleted"] = await chapter_state_deltas_repo.hard_delete_many(query, session=session)
            stats["character_state_snapshots_deleted"] = await character_state_snapshots_repo.hard_delete_many(query, session=session)
            stats["plot_thread_events_deleted"] = await plot_thread_events_repo.hard_delete_many(query, session=session)
            stats["manual_corrections_deleted"] = await manual_corrections_repo.hard_delete_many(query, session=session)
            stats["state_previews_deleted"] = await state_previews_repo.hard_delete_many(query, session=session)
            stats["mutation_journals_deleted"] = await mutation_journals_repo.hard_delete_many(query, session=session)

            novel_deleted = await novel_repo.hard_delete_one({"_id": obj_id}, session=session)
            stats["novel_deleted"] = 1 if novel_deleted else 0

            return stats

        return await run_mongo_write_unit(_delete, "hard_delete_novel")
