"""Deep module around persisted prose-run lifecycle and stale checks."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from backend.db import collections
from backend.db.mongo import get_database
from backend.db.mutation import MutationCommand, commit_mutation
from backend.db.narrative_revision import narrative_revision_store
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.prose_run_repository import prose_run_repo
from backend.db.utils import get_utc_now, to_object_id
from backend.services.generation.prose_completion import ProseExecutionPlan
from backend.services.generation.prose_generation import UncertainProseAttempt
from backend.services.novel.chapter_service import count_chapter_words
from backend.services.novel.derived_stats import derived_stats
from backend.services.novel.state_completion import chapter_content_digest


def prose_revision(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def prose_run_draft_text(document: dict[str, Any]) -> str:
    """Return the finished assembly or deterministically rebuild a partial draft."""
    assembled = str(document.get("assembled_text") or "")
    if assembled.strip():
        return assembled
    ordered = sorted(
        (
            dict(segment)
            for segment in document.get("segments") or []
            if str(segment.get("text") or "").strip()
        ),
        key=lambda segment: int(segment.get("sequence_index") or 0),
    )
    return "\n\n".join(
        str(segment.get("text") or "").strip()
        for segment in ordered
    )


def serialize_prose_run(document: dict[str, Any] | None) -> dict[str, Any] | None:
    if document is None:
        return None
    result = dict(document)
    result["assembled_text"] = prose_run_draft_text(document)
    for field in ("_id", "owner_id", "novel_id", "chapter_id"):
        if result.get(field) is not None:
            result[field] = str(result[field])
    return result


class ProseRunModule:
    async def begin(
        self,
        *,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        outline: dict[str, Any],
        context_text: str,
        plan: ProseExecutionPlan,
        provider_plan: dict[str, Any],
        run_id: str | None = None,
        expected_revision: int | None = None,
        confirm_uncertain_retry: bool = False,
    ) -> dict[str, Any]:
        outline_revision = prose_revision(outline)
        context_revision = prose_revision(context_text)
        if run_id:
            existing = await prose_run_repo.get_run(run_id, owner_id)
            if (
                str(existing.get("chapter_id")) != str(chapter_id)
                or existing.get("outline_revision") != outline_revision
                or existing.get("context_revision") != context_revision
            ):
                await prose_run_repo.mark_status(
                    run_id=run_id,
                    owner_id=owner_id,
                    status="stale",
                )
                raise ValueError("正文草稿基于旧细纲或旧上下文，不能继续自动拼接")
            if dict(existing.get("plan") or {}) != plan.to_dict():
                await prose_run_repo.mark_status(
                    run_id=run_id,
                    owner_id=owner_id,
                    status="stale",
                )
                raise ValueError(
                    "正文草稿的分段或输出能力计划已经变化，不能静默续写；"
                    "请保留旧稿参考并重新生成"
                )
            stored_provider = existing.get("provider_plan") or {}
            provider_changed = any(
                str(stored_provider.get(field) or "")
                != str(provider_plan.get(field) or "")
                for field in (
                    "provider_alias",
                    "provider_model",
                    "config_revision",
                )
            )
            if provider_changed:
                await prose_run_repo.mark_status(
                    run_id=run_id,
                    owner_id=owner_id,
                    status="stale",
                )
                raise ValueError(
                    "正文草稿的 Provider 或模型已经变化，不能静默续写；"
                    "请保留旧稿参考并重新生成"
                )
            if any(
                segment.get("status") == "uncertain"
                for segment in existing.get("segments") or []
            ) and not confirm_uncertain_retry:
                raise UncertainProseAttempt(
                    "存在已派发但未确认结果的正文请求，可能已经计费；"
                    "请明确确认可能重复计费后再继续"
                )
            revision = (
                int(expected_revision)
                if expected_revision is not None
                else int(existing.get("revision") or 0)
            )
            return await prose_run_repo.claim(
                run_id=run_id,
                owner_id=owner_id,
                expected_revision=revision,
            )

        created = await prose_run_repo.create_run({
            "owner_id": owner_id,
            "novel_id": novel_id,
            "chapter_id": chapter_id,
            "outline_revision": outline_revision,
            "context_revision": context_revision,
            "plan": plan.to_dict(),
            "provider_plan": dict(provider_plan),
            "narrative_revision": await narrative_revision_store.current(
                novel_id
            ),
            "completion": None,
            "assembled_text": "",
            "acceptance_state": None,
        })
        return await prose_run_repo.claim(
            run_id=str(created["_id"]),
            owner_id=owner_id,
            expected_revision=int(created["revision"]),
        )

    async def inspect_active(
        self,
        *,
        owner_id: str,
        chapter_id: str,
        outline: dict[str, Any],
        context_text: str,
    ) -> dict[str, Any] | None:
        active = await prose_run_repo.find_active(
            chapter_id=chapter_id,
            owner_id=owner_id,
        )
        if active is None:
            return None
        if (
            active.get("outline_revision") != prose_revision(outline)
            or active.get("context_revision") != prose_revision(context_text)
        ):
            await prose_run_repo.mark_status(
                run_id=str(active["_id"]),
                owner_id=owner_id,
                status="stale",
            )
            active["status"] = "stale"
        return active

    async def accept(
        self,
        *,
        owner_id: str,
        run_id: str,
        chapter_id: str,
        expected_revision: int,
        accept_partial: bool,
        partial_acknowledgement: bool,
    ) -> dict[str, Any]:
        run = await prose_run_repo.get_run(run_id, owner_id)
        if int(run.get("revision") or 0) != int(expected_revision):
            raise ValueError("正文草稿版本已经变化，请刷新后再接受")
        if str(run.get("chapter_id")) != str(chapter_id):
            raise ValueError("正文草稿不属于指定章节")
        captured_narrative_revision = run.get("narrative_revision")
        current_narrative_revision = await narrative_revision_store.current(
            str(run["novel_id"])
        )
        if (
            captured_narrative_revision is None
            or int(captured_narrative_revision)
            != current_narrative_revision
        ):
            await prose_run_repo.mark_status(
                run_id=run_id,
                owner_id=owner_id,
                status="stale",
            )
            raise ValueError(
                "正文草稿生成后的小说上下文已经变化，旧稿只能查看或复制"
            )
        chapter = await chapter_repo.get_chapter_by_id(chapter_id)
        if prose_revision(chapter.get("outline") or {}) != run.get("outline_revision"):
            raise ValueError("章节细纲已经变化，旧正文草稿不能写入")
        completion = dict(run.get("completion") or {})
        can_write = bool(completion.get("can_write_formal_prose"))
        if not can_write and not accept_partial:
            raise ValueError("正文尚未完成；只能继续生成或明确接受部分正文")
        if accept_partial and not partial_acknowledgement:
            raise ValueError("接受部分正文前必须确认仍需人工补写")
        text = prose_run_draft_text(run)
        if not text.strip():
            raise ValueError("正文草稿为空，不能接受")

        acceptance_state = (
            "partial_manual_required" if accept_partial else "ai_complete"
        )
        text_digest = chapter_content_digest(text)
        command = MutationCommand(
            novel_id=str(run["novel_id"]),
            idempotency_key=(
                f"accept-prose-run:{run_id}:{expected_revision}:"
                f"{acceptance_state}:{text_digest[:16]}"
            ),
            operation="accept_prose_run",
            version=1,
            payload={
                "run_id": run_id,
                "owner_id": owner_id,
                "chapter_id": chapter_id,
                "expected_revision": int(expected_revision),
                "text_digest": text_digest,
                "acceptance_state": acceptance_state,
                "accepted_partial": bool(accept_partial),
            },
            before_image={"chapter": chapter, "prose_run": run},
        )
        return await commit_mutation(
            command,
            self._execute_accept,
            advances_narrative_revision=True,
        )

    async def discard(self, *, owner_id: str, run_id: str) -> None:
        await prose_run_repo.get_run(run_id, owner_id)
        changed = await prose_run_repo.mark_status(
            run_id=run_id,
            owner_id=owner_id,
            status="discarded",
        )
        if not changed:
            raise ValueError("正文草稿已经变化，请刷新后再放弃")

    @staticmethod
    async def _execute_accept(session, mutation) -> dict[str, Any]:
        command = mutation.journal["command"]["payload"]
        run_id = str(command["run_id"])
        chapter_id = str(command["chapter_id"])
        owner_id = str(command["owner_id"])
        run = await get_database()[collections.PROSE_RUNS].find_one(
            {
                "_id": to_object_id(run_id),
                "owner_id": to_object_id(owner_id),
                "is_deleted": False,
            },
            session=session,
        )
        if run is None:
            raise ValueError("正文草稿不存在或不属于当前用户")
        expected_revision = int(command["expected_revision"])
        already_applied = (
            run.get("status") == "accepted"
            and run.get("acceptance_state") == command["acceptance_state"]
            and run.get("accepted_text_digest") == command["text_digest"]
        )
        if not already_applied and int(run.get("revision") or 0) != expected_revision:
            raise ValueError("正文草稿版本已经变化")
        text = prose_run_draft_text(run)
        if chapter_content_digest(text) != command["text_digest"]:
            raise ValueError("正文草稿内容摘要已经变化")

        accepted_at = get_utc_now()
        acceptance = {
            "state": command["acceptance_state"],
            "accepted_partial": bool(command.get("accepted_partial")),
            "source_run_id": run_id,
            "content_digest": command["text_digest"],
            "completion_status": (run.get("completion") or {}).get("status"),
            "finish_reason": (run.get("completion") or {}).get("finish_reason"),
            "accepted_at": accepted_at,
        }
        await mutation.advance_phase("primary_writes")
        if not mutation.was_received("chapter"):
            await chapter_repo.update_chapter(
                chapter_id,
                {
                    "content": text,
                    "word_count": count_chapter_words(text),
                    "status": (
                        "writing"
                        if command["acceptance_state"] == "partial_manual_required"
                        else (mutation.journal["command"]["before_image"]["chapter"].get("status") or "draft")
                    ),
                    "prose_acceptance": acceptance,
                },
                session=session,
            )
            await mutation.receipt("chapter", {
                "chapter_id": chapter_id,
                "content_digest": command["text_digest"],
            })
        if not mutation.was_received("prose_run") and not already_applied:
            update = await get_database()[collections.PROSE_RUNS].update_one(
                {
                    "_id": to_object_id(run_id),
                    "owner_id": to_object_id(owner_id),
                    "revision": expected_revision,
                    "is_deleted": False,
                },
                {
                    "$set": {
                        "status": "accepted",
                        "acceptance_state": command["acceptance_state"],
                        "accepted_partial": bool(
                            command.get("accepted_partial")
                        ),
                        "accepted_text_digest": command["text_digest"],
                        "accepted_at": accepted_at,
                        "updated_at": accepted_at,
                        "lease": None,
                    },
                    "$inc": {"revision": 1},
                },
                session=session,
            )
            if update.modified_count != 1:
                raise ValueError("正文草稿接受发生并发冲突")
            await mutation.receipt("prose_run", {"run_id": run_id})
        await mutation.advance_phase("derived_data")
        stats = await derived_stats.refresh(str(run["novel_id"]), session=session)
        await mutation.receipt("derived_stats", stats)
        return {
            "chapter_id": chapter_id,
            "run_id": run_id,
            "acceptance_state": command["acceptance_state"],
            "accepted_partial": bool(command.get("accepted_partial")),
            "content_digest": command["text_digest"],
            "word_count": count_chapter_words(text),
        }


prose_run_module = ProseRunModule()
