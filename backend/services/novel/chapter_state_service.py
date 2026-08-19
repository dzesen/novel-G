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
import hashlib
import json
from copy import deepcopy
from typing import Any, Dict, List

from pydantic import ValidationError
from bson import ObjectId

from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.character_state_repository import character_state_repo
from backend.db.repositories.plot_thread_repository import plot_thread_repo
from backend.db.transaction import run_mongo_write_unit
from backend.db.mutation import MutationCommand, commit_mutation
from backend.llm.schemas.novel_pydantic import ChapterStateAcceptSchema
from backend.services.llm.context_builder import fetch_roster
from backend.services.novel.state_validation import validate_state_ids
from backend.services.novel.state_completion import (
    chapter_content_digest,
    prose_acceptance_state,
)
from backend.services.novel.state_proposal import state_proposal_module
from backend.services.novel.state_timeline import record_acceptance
from backend.services.novel.narrative_timeline import narrative_timeline

logger = logging.getLogger(__name__)


class ChapterStateService:
    @staticmethod
    async def _execute_accept_chapter_state(session, mutation):
        """从持久化命令恢复状态接受，不重新计算去重或生成新的事实 ID。"""
        command = mutation.journal["command"]["payload"]
        novel_id = str(mutation.journal["novel_id"])
        chapter_id = str(command["chapter_id"])
        chapter_order = int(command["chapter_order"])
        data = command["state"]
        planned = data["character_updates"]
        skipped_duplicate_facts = list(command.get("skipped_duplicate_facts") or [])
        acceptance_metadata = command.get("acceptance_metadata") or {}
        proposal_claim = command.get("proposal_claim")
        states_updated = 0
        facts_appended = 0
        threads_updated = 0
        try:
            if proposal_claim:
                revision_receipt = (mutation.journal.get("receipts") or {}).get(
                    "narrative_revision"
                ) or {}
                if "revision" not in revision_receipt:
                    raise RuntimeError(
                        "Proposal acceptance requires a narrative revision receipt"
                    )
                await state_proposal_module.claim_for_mutation(
                    proposal_claim,
                    mutation_revision=int(revision_receipt["revision"]),
                    session=session,
                )
            await chapter_repo.update_chapter(
                chapter_id, {"summary": data["summary"]}, session=session
            )

            for character_index, update in enumerate(planned):
                await character_state_repo.upsert_state(
                    novel_id,
                    update["card_id"],
                    update["current_state"],
                    chapter_order,
                    session=session,
                    as_of_chapter_id=chapter_id,
                )
                states_updated += 1
                for fact_index, fact in enumerate(update["accepted_permanent_facts"]):
                    child_key = f"fact_{character_index}_{fact_index}"
                    if mutation.was_received(child_key):
                        facts_appended += 1
                        continue
                    await character_state_repo.append_permanent_fact(
                        novel_id,
                        update["card_id"],
                        {**fact, "id": mutation.child_id(child_key)},
                        session=session,
                    )
                    await mutation.receipt(
                        child_key, {"fact_id": mutation.child_id(child_key)}
                    )
                    facts_appended += 1

            for thread_index, thread in enumerate(data["accepted_thread_updates"]):
                child_key = f"thread_update_{thread_index}"
                if mutation.was_received(child_key):
                    threads_updated += 1
                    continue
                changes: Dict[str, Any] = {"status": thread["status"]}
                if thread["status"] == "resolved":
                    changes["resolved_chapter_order"] = chapter_order
                    changes["resolved_chapter_id"] = chapter_id
                await plot_thread_repo.update_thread(
                    novel_id, thread["thread_id"], changes, session=session
                )
                await mutation.receipt(child_key, {"thread_id": thread["thread_id"]})
                threads_updated += 1

            if mutation.was_received("timeline"):
                timeline_revision = int(
                    mutation.journal["receipts"]["timeline"]["revision"]
                )
            else:
                timeline_updates = []
                for character_index, update in enumerate(planned):
                    facts = list(update.get("retained_permanent_facts") or [])
                    for fact_index, fact in enumerate(update["accepted_permanent_facts"]):
                        child_key = f"fact_{character_index}_{fact_index}"
                        facts.append({**fact, "id": mutation.child_id(child_key)})
                    timeline_updates.append({
                        **{
                            key: value
                            for key, value in update.items()
                            if key != "retained_permanent_facts"
                        },
                        "accepted_permanent_facts": facts,
                    })
                await mutation.advance_phase("timeline_writes")
                timeline_revision = await record_acceptance(
                    novel_id,
                    chapter_id,
                    {**data, "character_updates": timeline_updates},
                    evaluation=acceptance_metadata,
                    session=session,
                )
                await mutation.receipt("timeline", {"revision": timeline_revision})

            if mutation.was_received("projection"):
                projection_digest = str(
                    mutation.journal["receipts"]["projection"]["digest"]
                )
            else:
                await mutation.advance_phase("derived_data")
                refresh_report = await narrative_timeline.refresh(
                    novel_id, session=session
                )
                projection_digest = str(refresh_report["digest"])
                await mutation.receipt(
                    "projection", {"digest": projection_digest}
                )

            result = {
                "chapter_id": chapter_id,
                "states_updated": states_updated,
                "facts_appended": facts_appended,
                "threads_updated": threads_updated,
                "skipped_duplicate_facts": skipped_duplicate_facts,
                "timeline_revision": timeline_revision,
                "projection_digest": projection_digest,
                "state_completion": deepcopy(
                    (acceptance_metadata or {}).get("state_completion") or {}
                ),
            }
            if proposal_claim:
                await state_proposal_module.mark_applied(
                    proposal_claim, result, session=session
                )
            return result
        except Exception:
            logger.error(
                "accept_chapter_state 中途失败：chapter_id=%s 已写 %s 个角色状态、"
                "%s 条永久事实、%s 个伏笔（非原子，将由 mutation journal 恢复）",
                chapter_id,
                states_updated,
                facts_appended,
                threads_updated,
            )
            raise

    @staticmethod
    async def prepare_chapter_state_mutation(
        chapter_id: str,
        payload: Dict[str, Any],
        *,
        acceptance_metadata: Dict[str, Any] | None = None,
        proposal_claim: Dict[str, Any] | None = None,
    ) -> MutationCommand:
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
        metadata = deepcopy(acceptance_metadata or {})
        completion_evidence = deepcopy(metadata.get("state_completion") or {})
        resolution = deepcopy(
            completion_evidence.get("reference_resolution") or {}
        )
        resolution.setdefault(
            "proposed_character_update_count",
            len(data.get("character_updates") or []),
        )
        resolution.setdefault(
            "accepted_character_update_count",
            len(data.get("character_updates") or []),
        )
        resolution.setdefault("dropped_character_update_count", 0)
        resolution.setdefault(
            "proposed_thread_update_count",
            len(data.get("accepted_thread_updates") or []),
        )
        resolution.setdefault(
            "accepted_thread_update_count",
            len(data.get("accepted_thread_updates") or []),
        )
        resolution.setdefault("dropped_thread_update_count", 0)
        proposed_characters = int(
            resolution.get("proposed_character_update_count") or 0
        )
        accepted_characters = int(
            resolution.get("accepted_character_update_count") or 0
        )
        dropped_characters = int(
            resolution.get("dropped_character_update_count") or 0
        )
        if (
            proposed_characters > 0
            and accepted_characters == 0
            and dropped_characters >= proposed_characters
        ):
            completion_reason = "all_character_updates_dropped"
        elif data.get("character_updates") or data.get("accepted_thread_updates"):
            completion_reason = "accepted_updates"
        else:
            completion_reason = "legitimate_empty"
        metadata["state_completion"] = {
            **completion_evidence,
            "source_content_digest": completion_evidence.get(
                "source_content_digest"
            )
            or chapter_content_digest(chapter.get("content") or ""),
            "source_prose_acceptance_state": completion_evidence.get(
                "source_prose_acceptance_state"
            )
            or prose_acceptance_state(chapter),
            "completion_reason": completion_reason,
            "reference_resolution": resolution,
        }

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
        # 同一稳定 chapter_id + 同文本才视为重复；跨卷同号不再互相吞掉。
        planned: List[Dict[str, Any]] = []
        skipped_duplicate_facts: List[str] = []
        for update in data["character_updates"]:
            # get_state 对不存在的角色返回 None（**不抛异常**），故这里是普通的
            # None 判断，不需要 try/except——首次出场的角色自然没有重复项。
            state = await character_state_repo.get_state(novel_id, update["card_id"])
            existing_by_text = {
                str(fact.get("fact", "")).strip(): fact
                for fact in ((state or {}).get("permanent_facts") or [])
                if str(fact.get("source_chapter_id") or "") == chapter_id
            }

            fresh = []
            retained = []
            for fact in update["accepted_permanent_facts"]:
                text = str(fact["fact"]).strip()
                if text in existing_by_text:
                    # **不静默**：跳过了什么必须报回给调用方（设计 §5.3）。
                    skipped_duplicate_facts.append(text)
                    existing = existing_by_text[text]
                    retained.append(
                        {
                            "id": str(existing["id"]),
                            "chapter_order": chapter_order,
                            "source_chapter_id": chapter_id,
                            "fact": text,
                            "kind": str(existing["kind"]),
                        }
                    )
                    continue
                existing_by_text[text] = fact
                fresh.append(
                    {
                        **fact,
                        "chapter_order": chapter_order,
                        "source_chapter_id": chapter_id,
                    }
                )
            planned.append(
                {
                    **update,
                    "accepted_permanent_facts": fresh,
                    "retained_permanent_facts": retained,
                }
            )

        child_ids = {}
        for character_index, update in enumerate(planned):
            for fact_index, _fact in enumerate(update["accepted_permanent_facts"]):
                child_ids[f"fact_{character_index}_{fact_index}"] = str(ObjectId())
        digest = hashlib.sha256(
            json.dumps(
                {
                    **data,
                    "character_updates": planned,
                    "acceptance_metadata": metadata,
                    "proposal_claim": proposal_claim,
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()

        idempotency_key = (
            f"accept-state-proposal:{proposal_claim['proposal_id']}"
            if proposal_claim
            else f"accept-state:{chapter_id}:{digest}"
        )
        return MutationCommand(
            novel_id=novel_id,
            idempotency_key=idempotency_key,
            operation="accept_chapter_state",
            payload={
                "chapter_id": chapter_id,
                "chapter_order": chapter_order,
                "state": {**data, "character_updates": planned},
                "skipped_duplicate_facts": skipped_duplicate_facts,
                "acceptance_metadata": metadata,
                "proposal_claim": proposal_claim,
            },
            before_image={"chapter_summary": chapter.get("summary", "")},
            child_ids=child_ids,
        )

    @staticmethod
    async def _commit_chapter_state(
        chapter_id: str,
        payload: Dict[str, Any],
        *,
        acceptance_metadata: Dict[str, Any] | None = None,
        proposal_claim: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        command = await ChapterStateService.prepare_chapter_state_mutation(
            chapter_id,
            payload,
            acceptance_metadata=acceptance_metadata,
            proposal_claim=proposal_claim,
        )
        return await commit_mutation(
            command,
            ChapterStateService._execute_accept_chapter_state,
        )

    @staticmethod
    async def _accept_proposal_state(
        chapter_id: str,
        payload: Dict[str, Any],
        *,
        acceptance_metadata: Dict[str, Any],
        proposal_claim: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Internal Proposal-only entrypoint; callers cannot omit claim identity."""
        if not proposal_claim.get("proposal_id"):
            raise ValueError("Proposal acceptance requires proposal_id")
        return await ChapterStateService._commit_chapter_state(
            chapter_id,
            payload,
            acceptance_metadata=acceptance_metadata,
            proposal_claim=proposal_claim,
        )

    @staticmethod
    async def import_legacy_chapter_state(
        chapter_id: str,
        payload: Dict[str, Any],
        *,
        import_metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Explicit offline migration seam; it is intentionally not exposed by HTTP."""
        metadata = {
            "confidence": "unrated",
            **(import_metadata or {}),
            "source": "legacy_import",
        }
        return await ChapterStateService._commit_chapter_state(
            chapter_id,
            payload,
            acceptance_metadata=metadata,
            proposal_claim=None,
        )
