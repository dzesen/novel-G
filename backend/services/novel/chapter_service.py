"""章节业务服务：维护章节、卷与小说之间的统计一致性。"""

from __future__ import annotations

import logging
import hashlib
import json
import re
from typing import Any, Dict, List

from pydantic import ValidationError
from bson import ObjectId

from backend.db.errors import DuplicateKeyError, NotFoundError
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db.repositories.plot_thread_repository import plot_thread_repo
from backend.db.repositories.volume_repository import volume_repo
from backend.db.mutation import MutationCommand, commit_mutation
from backend.db.utils import get_utc_now, to_object_id
from backend.llm.schemas.novel_pydantic import (
    ChapterOutlineEditSchema,
    ChapterOutlineResultSchema,
)
from backend.services.llm.context_builder import fetch_roster
from backend.services.novel.derived_stats import derived_stats
from backend.services.novel.outline_validation import validate_outline_ids
from backend.services.novel.state_timeline import (
    mark_downstream_stale,
    record_chapter_tombstone,
    record_plot_thread_event,
)


_WORD_TOKEN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]|[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*")
VALID_CHAPTER_STATUSES = {"draft", "writing", "completed"}

logger = logging.getLogger(__name__)


def _optional_object_id(value):
    """None 保持 None，其余转 ObjectId。存储 schema 的 id 一律 ObjectId（设计 §4.1）。"""
    return to_object_id(value) if value else None


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
    async def _refresh_v2_stats(session, mutation) -> bool:
        version = int(mutation.journal.get("command", {}).get("version") or 1)
        if version < 2:
            return False
        novel_id = str(mutation.journal["novel_id"])
        report = await derived_stats.refresh(novel_id, session=session)
        await mutation.receipt("derived_stats", report)
        return True

    @staticmethod
    async def _execute_create_chapter(session, mutation):
        command = mutation.journal["command"]["payload"]
        chapter_id = mutation.child_id("chapter")
        novel_id = str(mutation.journal["novel_id"])
        stored = await chapter_repo.find_one(
            {"_id": to_object_id(chapter_id)},
            include_deleted=True,
            session=session,
        )
        if stored is None:
            prepared = dict(command["chapter"])
            prepared["_id"] = to_object_id(chapter_id)
            await chapter_repo.create_chapter(prepared, session=session)
        await mutation.receipt("chapter", {"chapter_id": chapter_id})

        if not await ChapterService._refresh_v2_stats(session, mutation):
            await volume_repo.update_one(
                {"_id": to_object_id(command["volume_id"])},
                command["volume_stats_after"],
                session=session,
            )
            await mutation.receipt("volume_stats", command["volume_stats_after"])
            current_novel = await novel_repo.get_novel_by_id(novel_id, session=session)
            target = command["novel_stats_after"]
            deltas = {
                key: int(value) - int(current_novel.get(key, 0))
                for key, value in target.items()
                if int(value) != int(current_novel.get(key, 0))
            }
            if deltas:
                await novel_repo.increment_novel_stats(
                    novel_id, deltas, session=session
                )
            await mutation.receipt("novel_stats", target)
        return chapter_id

    @staticmethod
    async def create_chapter(data: Dict[str, Any]) -> str:
        novel_id = str(data.get("novel_id", ""))
        volume_id = str(data.get("volume_id", ""))
        auto_order = data.get("order_index") is None
        content = str(data.get("content", ""))
        prepared = {**data, "content": content, "word_count": count_chapter_words(content)}
        if prepared.get("status", "draft") not in VALID_CHAPTER_STATUSES:
            raise ValueError(f"Invalid chapter status: {prepared.get('status')}")
        await ChapterService._validate_scope(novel_id, volume_id)
        if not str(prepared.get("title") or "").strip():
            raise ValueError("Chapter title cannot be empty")
        if prepared.get("order_index") is not None:
            duplicate = await chapter_repo.find_one({
                "volume_id": to_object_id(volume_id),
                "order_index": int(prepared["order_index"]),
            })
            if duplicate:
                raise DuplicateKeyError(
                    f"同一卷下 order_index={prepared['order_index']} 已存在"
                )
        chapter_id = str(ObjectId())
        command = MutationCommand(
            novel_id=novel_id,
            idempotency_key=f"create-chapter:{chapter_id}",
            operation="create_chapter",
            version=2,
            payload={
                "chapter": prepared,
                "volume_id": volume_id,
            },
            child_ids={"chapter": chapter_id},
        )
        try:
            return await commit_mutation(command, ChapterService._execute_create_chapter)
        except DuplicateKeyError:
            if not auto_order:
                raise
            return await commit_mutation(command, ChapterService._execute_create_chapter)

    @staticmethod
    async def get_chapter(chapter_id: str) -> Dict[str, Any]:
        return await chapter_repo.get_chapter_by_id(chapter_id)

    @staticmethod
    async def get_chapters_by_novel(
        novel_id: str,
        *,
        include_content: bool = False,
    ) -> List[Dict[str, Any]]:
        await novel_repo.get_novel_by_id(novel_id)
        return await chapter_repo.get_chapters_by_novel(novel_id, include_content=include_content)

    @staticmethod
    async def get_deleted_chapters(novel_id: str) -> List[Dict[str, Any]]:
        await novel_repo.get_novel_by_id(novel_id)
        chapters = await chapter_repo.get_chapters_by_novel(
            novel_id,
            include_deleted=True,
        )
        return [chapter for chapter in chapters if chapter.get("is_deleted")]

    @staticmethod
    async def get_chapters_by_volume(
        volume_id: str,
        *,
        include_content: bool = False,
    ) -> List[Dict[str, Any]]:
        await volume_repo.get_volume_by_id(volume_id)
        return await chapter_repo.get_chapters_by_volume(
            volume_id,
            include_content=include_content,
        )

    @staticmethod
    async def update_chapter(chapter_id: str, update_data: Dict[str, Any]) -> bool:
        if "status" in update_data and update_data["status"] not in VALID_CHAPTER_STATUSES:
            raise ValueError(f"Invalid chapter status: {update_data['status']}")
        allowed = {
            "title", "summary", "content", "status", "order_index", "word_count", "outline"
        }
        prepared = {key: value for key, value in update_data.items() if key in allowed}
        if not prepared:
            return False
        if "title" in prepared:
            prepared["title"] = str(prepared["title"]).strip()
            if not prepared["title"]:
                raise ValueError("Chapter title cannot be empty")
        if "order_index" in prepared and int(prepared["order_index"]) < 1:
            raise ValueError("order_index must be greater than 0")
        chapter = await chapter_repo.get_chapter_by_id(chapter_id)
        if "order_index" in prepared:
            duplicate = await chapter_repo.find_one({
                "volume_id": chapter["volume_id"],
                "order_index": int(prepared["order_index"]),
                "_id": {"$ne": to_object_id(chapter_id)},
            })
            if duplicate:
                raise DuplicateKeyError("同一卷下已有相同章节序号")
        if "content" in prepared:
            prepared["content"] = str(prepared["content"])
            prepared["word_count"] = count_chapter_words(prepared["content"])
        novel_id = str(chapter["novel_id"])
        digest = hashlib.sha256(
            json.dumps(prepared, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:24]
        result = await commit_mutation(
            MutationCommand(
                novel_id=novel_id,
                idempotency_key=f"update-chapter:{chapter_id}:{chapter.get('updated_at')}:{digest}",
                operation="update_chapter",
                version=2,
                payload={
                    "chapter_id": chapter_id,
                    "update": prepared,
                    "volume_id": str(chapter["volume_id"]),
                    "mark_stale": "order_index" in prepared,
                },
                before_image={"chapter": chapter},
            ),
            ChapterService._execute_update_chapter,
        )
        return bool(result["updated"])

    @staticmethod
    async def _execute_update_chapter(session, mutation):
        command = mutation.journal["command"]["payload"]
        chapter_id = str(command["chapter_id"])
        novel_id = str(mutation.journal["novel_id"])
        await chapter_repo.update_chapter(
            chapter_id, command["update"], session=session
        )
        await mutation.receipt("chapter", {"chapter_id": chapter_id})
        if not await ChapterService._refresh_v2_stats(session, mutation):
            await volume_repo.update_one(
                {"_id": to_object_id(command["volume_id"])},
                command["volume_stats_after"],
                session=session,
            )
            await mutation.receipt("volume_stats", command["volume_stats_after"])
            current_novel = await novel_repo.get_novel_by_id(novel_id, session=session)
            target = command["novel_stats_after"]
            deltas = {
                key: int(value) - int(current_novel.get(key, 0))
                for key, value in target.items()
                if int(value) != int(current_novel.get(key, 0))
            }
            if deltas:
                await novel_repo.increment_novel_stats(
                    novel_id, deltas, session=session
                )
            await mutation.receipt("novel_stats", target)
        if command.get("mark_stale"):
            await mark_downstream_stale(novel_id, chapter_id, session=session)
            await mutation.receipt("stale", {"chapter_id": chapter_id})
        return {"chapter_id": chapter_id, "updated": True}

    @staticmethod
    async def _execute_accept_chapter_outline(session, mutation):
        """只依赖持久化 command/receipts，允许在进程重启后继续执行。"""
        command = mutation.journal["command"]["payload"]
        novel_id = str(mutation.journal["novel_id"])
        chapter_id = str(command["chapter_id"])
        chapter_order = int(command["chapter_order"])
        payload = command["outline"]
        previous_thread_ids = [str(item) for item in command["previous_thread_ids"]]
        created_thread_ids: List[str] = []
        try:
            for index, thread in enumerate(payload["new_threads"]):
                child_key = f"thread_{index}"
                stable_id = mutation.child_id(child_key)
                if mutation.was_received(child_key):
                    created_thread_ids.append(stable_id)
                    continue
                due_target = thread.get("due_target")
                if due_target is None and thread.get("due_chapter_order") is not None:
                    due_target = {
                        "kind": "planned_ordinal",
                        "ordinal": thread["due_chapter_order"],
                    }
                created_thread_ids.append(
                    await plot_thread_repo.create_thread(
                        novel_id,
                        {
                            "_id": stable_id,
                            "name": thread["name"],
                            "description": thread.get("description", ""),
                            "status": "planted",
                            "importance": thread.get("importance", "sub"),
                            "source": "outline",
                            "planted_chapter_order": chapter_order,
                            "planted_chapter_id": chapter_id,
                            "due_target": due_target,
                        },
                        session=session,
                    )
                )
                await record_plot_thread_event(
                    novel_id,
                    chapter_id,
                    stable_id,
                    "planted",
                    {"source": "outline"},
                    idempotency_key=f"outline:{chapter_id}:{stable_id}:planted",
                    session=session,
                )
                # 只有领域对象和时间线事件都成功后才记录子步骤完成。
                await mutation.receipt(child_key, {"thread_id": stable_id})

            stored = {
                "pov_character_card_id": _optional_object_id(payload["pov_character_card_id"]),
                "present_character_card_ids": [
                    to_object_id(cid) for cid in payload["present_character_card_ids"]
                ],
                "mentioned_character_card_ids": [
                    to_object_id(cid) for cid in payload["mentioned_character_card_ids"]
                ],
                "referenced_worldbook_card_ids": [
                    to_object_id(cid) for cid in payload["referenced_worldbook_card_ids"]
                ],
                "scenes": payload["scenes"],
                "core_conflict": payload["core_conflict"],
                "ending_hook": payload["ending_hook"],
                "target_word_count": payload["target_word_count"],
                "threads_planted": [to_object_id(tid) for tid in created_thread_ids],
                "threads_resolved": [
                    to_object_id(tid) for tid in payload["threads_resolved"]
                ],
                "generated_at": command["generated_at"],
                "edited_by_human": bool(command["edited_by_human"]),
            }
            await chapter_repo.update_chapter(chapter_id, {"outline": stored}, session=session)
            await mutation.receipt("outline", {"chapter_id": chapter_id})
            await mark_downstream_stale(novel_id, chapter_id, session=session)
            await mutation.receipt("stale", {"chapter_id": chapter_id})
            return {
                "chapter_id": chapter_id,
                "created_thread_ids": created_thread_ids,
                "previous_thread_ids": previous_thread_ids,
            }
        except Exception:
            logger.error(
                "accept_chapter_outline 中途失败：chapter_id=%s 已创建 %s 条伏笔 %s"
                "（非原子，将由 mutation journal 恢复）",
                chapter_id,
                len(created_thread_ids),
                created_thread_ids,
            )
            raise

    @staticmethod
    async def accept_chapter_outline(
        chapter_id: str,
        outline: Dict[str, Any],
        *,
        edited_by_human: bool = False,
    ) -> Dict[str, Any]:
        """接受章节细纲预览：创建 new_threads 并写入 chapter.outline（设计 §3.2 / §5.2）。

        这层的职责正是"LLM 输出 schema → 存储 schema"的映射：先建 new_threads
        拿到 id → 并入 threads_planted → id 字符串转 ObjectId → 补审计字段 → 落库。

        非原子（单机 mongod 无事务，run_mongo_write_unit 降级为顺序写）：
        ①写前全校验（形状 + id 存在性，第一次写之前）②run_mongo_write_unit(auto)
        ③失败精确上报已创建的伏笔 ④不谎报回滚。

        **允许重复接受**：chapter.outline 是单文档字段，覆盖没有"拼接两套方案"的
        风险（那条风险来自跨多卷多章的批量建库）。但上一次接受创建的伏笔会成为
        孤儿，以 previous_thread_ids 如实返回并告警——**不自动软删**，因为无法
        区分其中哪些已被人手工编辑过，自动删除是破坏性动作。

        Args:
            chapter_id: 目标章节 ObjectId 字符串。
            outline: 细纲 payload（LLM 输出或经人编辑），形状须符合 ChapterOutlineResultSchema。
            edited_by_human: 该细纲是否经人编辑，落库进 outline.edited_by_human。

        Returns:
            {"chapter_id", "created_thread_ids", "previous_thread_ids"}。

        Raises:
            ValueError: payload 形状非法，或引用了不存在于该小说 roster 的 id。两者
                都在任何写入之前抛出。
            NotFoundError: 章节不存在。
        """
        # 层 1a：形状校验。extra="forbid" + 必填项在此一次卡死，之后的字段访问才安全。
        try:
            parsed = ChapterOutlineResultSchema.model_validate(outline)
        except ValidationError as exc:
            raise ValueError(f"章节细纲数据非法，未做任何写入：{exc}") from exc
        payload = parsed.model_dump()

        chapter = await chapter_repo.get_chapter_by_id(chapter_id)
        novel_id = str(chapter["novel_id"])
        chapter_order = int(chapter.get("order_index") or 0)

        # 层 1b：id 存在性校验。roster 与预览端出自**同一个 build_roster**，故
        # 预览清洗过的 payload 必然通过这里；不通过只可能来自两次调用之间的真实
        # 变化（如伏笔已被回收），那正该报错。
        # 这里 raise 而非 drop：accept 没有预览可上报，静默剔除会以"细纲莫名其妙
        # 少了一半人物"的形式无声通过——正是设计 §5.3 要防的那种降级。
        roster = await fetch_roster(novel_id)
        _cleaned, dropped = validate_outline_ids(payload, roster)
        if dropped:
            details = "；".join(
                f"{field}: {', '.join(ids)}" for field, ids in sorted(dropped.items())
            )
            raise ValueError(f"细纲引用了该小说中不存在的 id，未做任何写入：{details}")

        previous_thread_ids = [
            str(tid) for tid in ((chapter.get("outline") or {}).get("threads_planted") or [])
        ]
        if previous_thread_ids:
            logger.warning(
                "章节 %s 已有细纲，本次接受将覆盖；上次创建的 %s 条伏笔不会被自动清理"
                "（无法区分其中哪些已被人手工编辑过）：%s",
                chapter_id,
                len(previous_thread_ids),
                previous_thread_ids,
            )

        child_ids = {
            f"thread_{index}": str(ObjectId())
            for index, _thread in enumerate(payload["new_threads"])
        }
        digest = hashlib.sha256(
            json.dumps(
                {"payload": payload, "previous_outline": chapter.get("outline")},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()

        return await commit_mutation(
            MutationCommand(
                novel_id=novel_id,
                idempotency_key=f"accept-outline:{chapter_id}:{digest}",
                operation="accept_chapter_outline",
                payload={
                    "chapter_id": chapter_id,
                    "chapter_order": chapter_order,
                    "outline": payload,
                    "previous_thread_ids": previous_thread_ids,
                    "edited_by_human": bool(edited_by_human),
                    "generated_at": get_utc_now(),
                },
                before_image={"outline": chapter.get("outline")},
                child_ids=child_ids,
            ),
            ChapterService._execute_accept_chapter_outline,
        )

    @staticmethod
    async def update_chapter_outline(
        chapter_id: str,
        edit_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """编辑已存 chapter.outline 的作者字段（设计 §4）。

        与 accept 的分工：accept 把 AI 预览物化（建 new_threads → 回填 threads_planted）；
        本方法只改作者字段，threads_planted 与 generated_at 从现有 outline **原样保留**，
        edited_by_human 强制 true。零 plot_thread 写、零字数/卷书统计联动。

        Raises:
            ValueError: payload 形状非法 / 引用了不存在的 id / 章节尚无 outline。三者均在写前。
            NotFoundError: 章节不存在。
        """
        # 层 1a：形状校验。extra="forbid" 在此挡下 new_threads / threads_planted /
        # generated_at / edited_by_human 等越界键——编辑不能建伏笔、不能篡改保留字段。
        try:
            parsed = ChapterOutlineEditSchema.model_validate(edit_payload)
        except ValidationError as exc:
            raise ValueError(f"细纲编辑数据非法，未做任何写入：{exc}") from exc
        payload = parsed.model_dump()

        chapter = await chapter_repo.get_chapter_by_id(chapter_id)
        existing = chapter.get("outline")
        if not existing:
            raise ValueError("本章尚无细纲，请先生成细纲后再编辑，未做任何写入")

        novel_id = str(chapter["novel_id"])

        # 层 1b：id 存在性校验（raise 模式，与 accept 同一函数、同一 roster）。
        roster = await fetch_roster(novel_id)
        _cleaned, dropped = validate_outline_ids(payload, roster)
        if dropped:
            details = "；".join(
                f"{field}: {', '.join(ids)}" for field, ids in sorted(dropped.items())
            )
            raise ValueError(f"细纲引用了该小说中不存在的 id，未做任何写入：{details}")

        stored = {
            "pov_character_card_id": _optional_object_id(payload["pov_character_card_id"]),
            "present_character_card_ids": [
                to_object_id(cid) for cid in payload["present_character_card_ids"]
            ],
            "mentioned_character_card_ids": [
                to_object_id(cid) for cid in payload["mentioned_character_card_ids"]
            ],
            "referenced_worldbook_card_ids": [
                to_object_id(cid) for cid in payload["referenced_worldbook_card_ids"]
            ],
            "scenes": payload["scenes"],
            "core_conflict": payload["core_conflict"],
            "ending_hook": payload["ending_hook"],
            "target_word_count": payload["target_word_count"],
            "threads_resolved": [to_object_id(tid) for tid in payload["threads_resolved"]],
            # 保留：threads_planted 与 generated_at 原样从现有 outline 并回（设计 §2.1/§2.3）。
            # 编辑 schema 不含这两个字段，故客户端无从篡改；这里是它们唯一的来源。
            # 库里它们本就是 ObjectId / datetime，不需要转换。
            "threads_planted": list(existing.get("threads_planted") or []),
            "generated_at": existing.get("generated_at"),
            "edited_by_human": True,
        }

        digest = hashlib.sha256(
            json.dumps(stored, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:24]
        return await commit_mutation(
            MutationCommand(
                novel_id=novel_id,
                idempotency_key=f"update-chapter-outline:{chapter_id}:{chapter.get('updated_at')}:{digest}",
                operation="update_chapter_outline",
                payload={"chapter_id": chapter_id, "outline": stored},
                before_image={"outline": existing},
            ),
            ChapterService._execute_update_chapter_outline,
        )

    @staticmethod
    async def _execute_update_chapter_outline(session, mutation):
        command = mutation.journal["command"]["payload"]
        chapter_id = str(command["chapter_id"])
        novel_id = str(mutation.journal["novel_id"])
        await chapter_repo.update_chapter(
            chapter_id, {"outline": command["outline"]}, session=session
        )
        await mutation.receipt("outline", {"chapter_id": chapter_id})
        await mark_downstream_stale(novel_id, chapter_id, session=session)
        await mutation.receipt("stale", {"chapter_id": chapter_id})
        return await chapter_repo.get_chapter_by_id(chapter_id, session=session)

    @staticmethod
    async def _execute_soft_delete_chapter(session, mutation):
        command = mutation.journal["command"]["payload"]
        chapter = command["chapter"]
        chapter_id = str(command["chapter_id"])
        novel_id = str(mutation.journal["novel_id"])
        if not mutation.was_received("stale"):
            await mark_downstream_stale(
                novel_id, chapter_id, session=session
            )
            await mutation.receipt("stale", {"chapter_id": chapter_id})

        stored = await chapter_repo.get_chapter_by_id(
            chapter_id, include_deleted=True, session=session
        )
        if not stored.get("is_deleted"):
            await chapter_repo.soft_delete_chapter(chapter_id, session=session)
        await mutation.receipt("chapter", {"chapter_id": chapter_id})

        if not await ChapterService._refresh_v2_stats(session, mutation):
            await volume_repo.update_one(
                {"_id": to_object_id(chapter["volume_id"])},
                command["volume_stats_after"],
                session=session,
            )
            await mutation.receipt("volume_stats", command["volume_stats_after"])
            await novel_repo.update_one(
                {"_id": to_object_id(novel_id)},
                command["novel_stats_after"],
                session=session,
            )
            await mutation.receipt("novel_stats", command["novel_stats_after"])
        return {"chapter_id": chapter_id, "deleted": True}

    @staticmethod
    async def soft_delete_chapter(chapter_id: str) -> bool:
        chapter = await chapter_repo.get_chapter_by_id(chapter_id)
        novel_id = str(chapter["novel_id"])
        command = MutationCommand(
            novel_id=novel_id,
            idempotency_key=f"soft-delete-chapter:{chapter_id}:{chapter.get('updated_at')}",
            operation="soft_delete_chapter",
            version=2,
            payload={
                "chapter_id": chapter_id,
                "chapter": chapter,
            },
            before_image={"chapter": chapter},
        )
        result = await commit_mutation(
            command, ChapterService._execute_soft_delete_chapter
        )
        return bool(result["deleted"])

    @staticmethod
    async def _execute_restore_chapter(session, mutation):
        command = mutation.journal["command"]["payload"]
        chapter = command["chapter"]
        chapter_id = str(command["chapter_id"])
        novel_id = str(mutation.journal["novel_id"])
        stored = await chapter_repo.get_chapter_by_id(
            chapter_id, include_deleted=True, session=session
        )
        if stored.get("is_deleted"):
            await chapter_repo.restore_chapter(chapter_id, session=session)
        await mutation.receipt("chapter", {"chapter_id": chapter_id})
        if not await ChapterService._refresh_v2_stats(session, mutation):
            await volume_repo.update_one(
                {"_id": to_object_id(chapter["volume_id"])},
                command["volume_stats_after"],
                session=session,
            )
            await mutation.receipt("volume_stats", command["volume_stats_after"])
            await novel_repo.update_one(
                {"_id": to_object_id(novel_id)},
                command["novel_stats_after"],
                session=session,
            )
            await mutation.receipt("novel_stats", command["novel_stats_after"])
        await mark_downstream_stale(novel_id, chapter_id, session=session)
        await mutation.receipt("stale", {"chapter_id": chapter_id})
        return {"chapter_id": chapter_id, "restored": True}

    @staticmethod
    async def restore_chapter(chapter_id: str) -> bool:
        chapter = await chapter_repo.get_chapter_by_id(chapter_id, include_deleted=True)
        if not chapter.get("is_deleted"):
            raise ValueError("Chapter is not in deleted state")
        novel_id = str(chapter["novel_id"])
        volume_id = str(chapter["volume_id"])
        await ChapterService._validate_scope(novel_id, volume_id)
        result = await commit_mutation(
            MutationCommand(
                novel_id=novel_id,
                idempotency_key=f"restore-chapter:{chapter_id}:{chapter.get('updated_at')}",
                operation="restore_chapter",
                version=2,
                payload={
                    "chapter_id": chapter_id,
                    "chapter": chapter,
                },
                before_image={"chapter": chapter},
            ),
            ChapterService._execute_restore_chapter,
        )
        return bool(result["restored"])

    @staticmethod
    async def _execute_hard_delete_chapter(session, mutation):
        command = mutation.journal["command"]["payload"]
        chapter = command["chapter"]
        chapter_id = str(command["chapter_id"])
        if not mutation.was_received("tombstone"):
            await record_chapter_tombstone(chapter, session=session)
            await mutation.receipt("tombstone", {"chapter_id": chapter_id})
        stored = await chapter_repo.find_one(
            {"_id": to_object_id(chapter_id)}, include_deleted=True, session=session
        )
        if stored is not None:
            await chapter_repo.hard_delete_chapter(chapter_id, session=session)
        await mutation.receipt("chapter", {"chapter_id": chapter_id})
        return {"chapter_id": chapter_id, "deleted": True}

    @staticmethod
    async def hard_delete_chapter(chapter_id: str) -> bool:
        chapter = await chapter_repo.get_chapter_by_id(chapter_id, include_deleted=True)
        if not chapter.get("is_deleted"):
            raise ValueError("Only soft-deleted chapters can be permanently deleted")
        result = await commit_mutation(
            MutationCommand(
                novel_id=str(chapter["novel_id"]),
                idempotency_key=f"hard-delete-chapter:{chapter_id}:{chapter.get('updated_at')}",
                operation="hard_delete_chapter",
                payload={"chapter_id": chapter_id, "chapter": chapter},
                before_image={"chapter": chapter},
            ),
            ChapterService._execute_hard_delete_chapter,
        )
        return bool(result["deleted"])
