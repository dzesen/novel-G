"""章节业务服务：维护章节、卷与小说之间的统计一致性。"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

from pydantic import ValidationError

from backend.db.errors import DuplicateKeyError, NotFoundError
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db.repositories.plot_thread_repository import plot_thread_repo
from backend.db.repositories.volume_repository import volume_repo
from backend.db.transaction import run_mongo_write_unit
from backend.db.utils import get_utc_now, to_object_id
from backend.llm.schemas.novel_pydantic import (
    ChapterOutlineEditSchema,
    ChapterOutlineResultSchema,
)
from backend.services.llm.context_builder import fetch_roster
from backend.services.novel.outline_validation import validate_outline_ids


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

        async def _write(session):
            created_thread_ids: List[str] = []
            try:
                # 伏笔表在本链之前没有任何创建入口，threads_planted/threads_resolved
                # 因此一直是死字段（设计 §2）。这里是它们的第一个真实写入方。
                for thread in payload["new_threads"]:
                    created_thread_ids.append(
                        await plot_thread_repo.create_thread(
                            novel_id,
                            {
                                "name": thread["name"],
                                "description": thread.get("description", ""),
                                "status": "planted",
                                "importance": thread.get("importance", "sub"),
                                "source": "outline",
                                "planted_chapter_order": chapter_order,
                                "due_chapter_order": thread.get("due_chapter_order"),
                            },
                            session=session,
                        )
                    )

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
                    "generated_at": get_utc_now(),
                    "edited_by_human": bool(edited_by_human),
                }
                # 直接走仓储、不经 ChapterService.update_chapter：后者要算字数增量
                # 并联动卷/书统计，而细纲不动正文，没有增量可算。
                await chapter_repo.update_chapter(chapter_id, {"outline": stored}, session=session)

                return {
                    "chapter_id": chapter_id,
                    "created_thread_ids": created_thread_ids,
                    "previous_thread_ids": previous_thread_ids,
                }
            except Exception:
                # 失败精确上报已创建内容，不谎报回滚（单机无事务，见设计 §7）。
                logger.error(
                    "accept_chapter_outline 中途失败：chapter_id=%s 已创建 %s 条伏笔 %s"
                    "（非原子，未回滚）",
                    chapter_id,
                    len(created_thread_ids),
                    created_thread_ids,
                )
                raise

        return await run_mongo_write_unit(_write, "accept_chapter_outline")

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

        async def _write(session):
            await chapter_repo.update_chapter(chapter_id, {"outline": stored}, session=session)
            return await chapter_repo.get_chapter_by_id(chapter_id, session=session)

        return await run_mongo_write_unit(_write, "update_chapter_outline")

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
