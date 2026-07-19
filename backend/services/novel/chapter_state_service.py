"""状态回填的落库层：把 AI 提议 + 人的勾选映射成记忆层的写入（设计 §5）。

分治的依据是**可逆性**：
- summary / current_state 覆盖式，全盘接受，改错了重跑即可；
- permanent_facts 只增不改、无删除入口，故逐条勾选 + 服务层去重；
- 伏笔 status 可改，但当前没有伏笔管理 UI 能纠正，故逐条勾选。

非原子（单机 mongod 无事务，run_mongo_write_unit 降级为顺序写）：
①写前全校验（形状 + id 存在性，第一次写之前）②run_mongo_write_unit(auto)
③失败精确上报已写内容 ④不谎报回滚。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from pydantic import ValidationError

from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.character_state_repository import character_state_repo
from backend.db.repositories.plot_thread_repository import plot_thread_repo
from backend.db.transaction import run_mongo_write_unit
from backend.llm.schemas.novel_pydantic import ChapterStateAcceptSchema
from backend.services.llm.context_builder import fetch_roster
from backend.services.novel.state_validation import validate_state_ids

logger = logging.getLogger(__name__)


class ChapterStateService:
    @staticmethod
    async def accept_chapter_state(
        chapter_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """接受状态回填预览：写章摘要、回填人物状态、推进伏笔状态。

        Args:
            chapter_id: 目标章节 ObjectId 字符串。
            payload: 只含**勾选后**内容的 accept 入参，形状须符合 ChapterStateAcceptSchema。

        Returns:
            {"chapter_id", "states_updated", "facts_appended", "threads_updated",
             "skipped_duplicate_facts"}。

        Raises:
            ValueError: payload 形状非法，或引用了不存在于该小说 roster 的 id。
                两者都在任何写入之前抛出。
            NotFoundError: 章节不存在。
        """
        # 层 1a：形状校验。extra="forbid" + 必填项在此一次卡死。
        try:
            parsed = ChapterStateAcceptSchema.model_validate(payload)
        except ValidationError as exc:
            raise ValueError(f"状态回填数据非法，未做任何写入：{exc}") from exc
        data = parsed.model_dump()

        chapter = await chapter_repo.get_chapter_by_id(chapter_id)
        novel_id = str(chapter["novel_id"])
        chapter_order = int(chapter.get("order_index") or 0)

        # 层 1b：id 存在性校验。这里 raise 而非 drop：accept 没有预览可上报，
        # 静默剔除会以"回填莫名其妙少了一半角色"的形式无声通过。
        roster = await fetch_roster(novel_id)
        _cleaned, dropped = validate_state_ids(data, roster)
        if dropped:
            details = "；".join(
                f"{field}: {', '.join(ids)}" for field, ids in sorted(dropped.items())
            )
            raise ValueError(f"状态回填引用了该小说中不存在的 id，未做任何写入：{details}")

        # 层 1c：去重判定也放在写前——需要读库里现有的事实，而这是读不是写。
        # 同章同文本即视为重复（设计 §5.3）。跨章不去重：不同 chapter_order
        # 是不同的断言。
        planned: List[Dict[str, Any]] = []
        skipped_duplicate_facts: List[str] = []
        for update in data["character_updates"]:
            # get_state 对不存在的角色返回 None（**不抛异常**），故这里是普通的
            # None 判断，不需要 try/except——首次出场的角色自然没有重复项。
            state = await character_state_repo.get_state(novel_id, update["card_id"])
            existing_texts = {
                str(fact.get("fact", "")).strip()
                for fact in ((state or {}).get("permanent_facts") or [])
                if int(fact.get("chapter_order", 0)) == chapter_order
            }

            fresh = []
            for fact in update["accepted_permanent_facts"]:
                text = str(fact["fact"]).strip()
                if text in existing_texts:
                    # **不静默**：跳过了什么必须报回给调用方（设计 §5.3）。
                    skipped_duplicate_facts.append(text)
                    continue
                existing_texts.add(text)
                fresh.append({**fact, "chapter_order": chapter_order})
            planned.append({**update, "accepted_permanent_facts": fresh})

        async def _write(session):
            states_updated = 0
            facts_appended = 0
            threads_updated = 0
            try:
                await chapter_repo.update_chapter(
                    chapter_id, {"summary": data["summary"]}, session=session
                )

                for update in planned:
                    # **顺序是硬约束**：append_permanent_fact 在状态文档不存在时抛
                    # NotFoundError（阶段 1 刻意如此——永久事实绝不能被静默丢弃）。
                    # 因此无条件先 upsert_state，即使该角色只有事实被勾选、
                    # current_state 为空文本。颠倒过来，首次出场的角色永远存不进事实。
                    await character_state_repo.upsert_state(
                        novel_id,
                        update["card_id"],
                        update["current_state"],
                        chapter_order,
                        session=session,
                    )
                    states_updated += 1
                    for fact in update["accepted_permanent_facts"]:
                        await character_state_repo.append_permanent_fact(
                            novel_id, update["card_id"], fact, session=session
                        )
                        facts_appended += 1

                for thread in data["accepted_thread_updates"]:
                    changes: Dict[str, Any] = {"status": thread["status"]}
                    if thread["status"] == "resolved":
                        changes["resolved_chapter_order"] = chapter_order
                    await plot_thread_repo.update_thread(
                        novel_id, thread["thread_id"], changes, session=session
                    )
                    threads_updated += 1

                return {
                    "chapter_id": chapter_id,
                    "states_updated": states_updated,
                    "facts_appended": facts_appended,
                    "threads_updated": threads_updated,
                    "skipped_duplicate_facts": skipped_duplicate_facts,
                }
            except Exception:
                # 失败精确上报已写内容，不谎报回滚（单机无事务）。
                logger.error(
                    "accept_chapter_state 中途失败：chapter_id=%s 已写 %s 个角色状态、"
                    "%s 条永久事实、%s 个伏笔（非原子，未回滚）",
                    chapter_id,
                    states_updated,
                    facts_appended,
                    threads_updated,
                )
                raise

        return await run_mongo_write_unit(_write, "accept_chapter_state")
