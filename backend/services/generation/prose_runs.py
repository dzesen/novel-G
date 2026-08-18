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
from backend.db.repositories.prose_run_repository import (
    CURRENT_PROSE_RUN_STATUSES,
    prose_run_repo,
)
from backend.db.utils import get_utc_now, to_object_id
from backend.services.generation.prose_completion import ProseExecutionPlan
from backend.services.generation.prose_protocol import (
    is_scene_continuation_v3_family,
)
from backend.services.generation.prose_generation import UncertainProseAttempt
from backend.services.llm.context_builder import normalize_outline_references
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
    is_v3 = is_scene_continuation_v3_family(
        (document.get("plan") or {}).get("protocol_revision")
    )

    def sort_key(segment: dict[str, Any]) -> tuple[int, int, int]:
        sequence = int(segment.get("sequence_index") or 0)
        if not is_v3:
            return (sequence, 0, 0)
        return (
            int(segment.get("scene_index") or 0),
            int(segment.get("scene_call_index", segment.get("part_index") or 0) or 0),
            sequence,
        )
    ordered = sorted(
        (
            dict(segment)
            for segment in document.get("segments") or []
            if str(segment.get("text") or "").strip()
        ),
        key=sort_key,
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


def _has_exhausted_segment(
    document: dict[str, Any],
    plan: ProseExecutionPlan,
) -> bool:
    if plan.protocol_revision != "scene-target-priority-v2":
        return False
    return any(
        segment.get("status") != "completed"
        and int(segment.get("continuation_count") or 0)
        >= plan.max_continuations
        for segment in document.get("segments") or []
    )


def _stored_run_has_exhausted_segment(document: dict[str, Any]) -> bool:
    plan = document.get("plan") or {}
    if str(plan.get("protocol_revision") or "") != "scene-target-priority-v2":
        return False
    max_continuations = int(
        plan.get("max_continuations") or 0
    )
    if max_continuations <= 0:
        return False
    return any(
        segment.get("status") != "completed"
        and int(segment.get("continuation_count") or 0)
        >= max_continuations
        for segment in document.get("segments") or []
    )


def _run_has_uncertain_attempt(document: dict[str, Any]) -> bool:
    return any(
        segment.get("status") == "uncertain"
        for segment in document.get("segments") or []
    )


def _leftover_reason_codes(document: dict[str, Any]) -> list[str]:
    codes = [
        str(code)
        for code in (document.get("completion") or {}).get("reason_codes") or []
        if str(code).strip()
    ]
    codes.extend(
        str(progress.get("pause_reason") or "")
        for progress in document.get("scene_progress") or []
        if str(progress.get("pause_reason") or "").strip()
    )
    if _run_has_uncertain_attempt(document):
        codes.append("uncertain_provider_attempt")
    if _stored_run_has_exhausted_segment(document):
        codes.append("continuation_limit_reached")
    if not codes:
        finish_reasons = {
            str(segment.get("finish_reason") or "")
            for segment in document.get("segments") or []
            if segment.get("status") != "completed"
        }
        for finish_reason in (
            "length",
            "content_filter",
            "tool_call",
            "cancelled",
            "error",
        ):
            if finish_reason in finish_reasons:
                codes.append(f"finish_reason_{finish_reason}")
    return list(dict.fromkeys(codes))


def _run_has_live_lease(document: dict[str, Any]) -> bool:
    expires_at = (document.get("lease") or {}).get("expires_at")
    return bool(expires_at and expires_at > get_utc_now())


def _outline_revision_is_current(
    stored_revision: Any,
    outline: dict[str, Any],
) -> bool:
    normalized = normalize_outline_references(outline) or {}
    return stored_revision in {
        prose_revision(outline),
        prose_revision(normalized),
    }


def _safe_non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _scene_count_telemetry(item: dict[str, Any]) -> tuple[int, int, int]:
    """Project raw/effective/replay counts while keeping legacy runs readable."""
    word_count = _safe_non_negative_int(item.get("word_count"))
    raw_word_count = (
        _safe_non_negative_int(item.get("raw_word_count"))
        if item.get("raw_word_count") is not None
        else word_count
    )
    effective_word_count = min(
        raw_word_count,
        (
            _safe_non_negative_int(item.get("effective_word_count"))
            if item.get("effective_word_count") is not None
            else raw_word_count
        ),
    )
    return (
        raw_word_count,
        effective_word_count,
        _safe_non_negative_int(item.get("replayed_characters_total")),
    )


def _telemetry_scene_progress(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Return persisted scene counters without prose, prompt, or raw error data."""
    result: list[dict[str, Any]] = []
    for item in document.get("scene_progress") or []:
        if not isinstance(item, dict):
            continue
        (
            raw_word_count,
            effective_word_count,
            replayed_characters_total,
        ) = _scene_count_telemetry(item)
        result.append(
            {
                "scene_index": _safe_non_negative_int(item.get("scene_index")),
                "status": str(item.get("status") or "pending"),
                "base_calls_used": _safe_non_negative_int(
                    item.get("base_calls_used")
                ),
                "automatic_continuations_used": _safe_non_negative_int(
                    item.get("automatic_continuations_used")
                ),
                "manual_continuations_used": _safe_non_negative_int(
                    item.get("manual_continuations_used")
                ),
                "word_count": _safe_non_negative_int(item.get("word_count")),
                "raw_word_count": raw_word_count,
                "effective_word_count": effective_word_count,
                "replayed_characters_total": replayed_characters_total,
                "scene_target_words": _safe_non_negative_int(
                    item.get("scene_target_words")
                ),
                "converge_attempts": _safe_non_negative_int(
                    item.get("converge_attempts")
                ),
                "converge_attempts_without_stop": _safe_non_negative_int(
                    item.get("converge_attempts_without_stop")
                ),
                "continues_truncated_output_count": _safe_non_negative_int(
                    item.get("continues_truncated_output_count")
                ),
                "max_cross_call_repeat_characters": _safe_non_negative_int(
                    item.get("max_cross_call_repeat_characters")
                ),
                "pause_reason": (
                    str(item.get("pause_reason"))
                    if item.get("pause_reason") is not None
                    else None
                ),
                "last_prompt_mode": (
                    str(item.get("last_prompt_mode"))
                    if item.get("last_prompt_mode") is not None
                    else None
                ),
                "last_finish_reason": str(
                    item.get("last_finish_reason") or "unreported"
                ),
                "consecutive_no_progress": _safe_non_negative_int(
                    item.get("consecutive_no_progress")
                ),
            }
        )
    return sorted(result, key=lambda item: item["scene_index"])


def serialize_prose_run_telemetry(document: dict[str, Any]) -> dict[str, Any]:
    """Serialize operational metadata while deliberately excluding prose and prompts."""
    plan = document.get("plan") or {}
    completion = document.get("completion") or {}
    provider_plan = document.get("provider_plan") or {}
    authorization = document.get("prose_continuation_authorization") or {}
    policy = authorization.get("policy") or {}
    scene_progress = _telemetry_scene_progress(document)
    continuation_exhausted = _stored_run_has_exhausted_segment(document) or any(
        item.get("pause_reason") == "automatic_continuations_exhausted"
        for item in scene_progress
    )
    return {
        "run_id": str(document["_id"]),
        "novel_id": str(document["novel_id"]),
        "chapter_id": str(document["chapter_id"]),
        "revision": _safe_non_negative_int(document.get("revision")),
        "status": str(document.get("status") or "unknown"),
        "provider": {
            "alias": str(provider_plan.get("provider_alias") or ""),
            "model": str(provider_plan.get("provider_model") or ""),
        },
        "plan": {
            "mode": str(plan.get("mode") or "single_call"),
            "requested_word_count": _safe_non_negative_int(
                plan.get("requested_word_count")
            ),
            "scene_count": _safe_non_negative_int(plan.get("scene_count")),
            "scheduled_base_call_count": _safe_non_negative_int(
                plan.get("scheduled_base_call_count", plan.get("call_count"))
            ),
            "protocol_revision": str(plan.get("protocol_revision") or ""),
        },
        "completion": {
            "status": str(completion.get("status") or "pending"),
            "requested_word_count": _safe_non_negative_int(
                completion.get("requested_word_count")
            ),
            "actual_word_count": _safe_non_negative_int(
                completion.get("actual_word_count")
            ),
            "scene_count": _safe_non_negative_int(completion.get("scene_count")),
            "completed_scene_count": _safe_non_negative_int(
                completion.get("completed_scene_count")
            ),
            "finish_reason": str(completion.get("finish_reason") or "unreported"),
            "reason_codes": [
                str(code)
                for code in completion.get("reason_codes") or []
                if str(code).strip()
            ][:20],
        },
        "scene_progress": scene_progress,
        "usage": {
            "provider_attempt_count": _safe_non_negative_int(
                document.get("provider_attempt_count")
            ),
            "tokens_used": _safe_non_negative_int(document.get("tokens_used")),
            "tokens_reserved": _safe_non_negative_int(
                document.get("tokens_reserved")
            ),
            "token_budget": (
                _safe_non_negative_int(document.get("token_budget"))
                if document.get("token_budget") is not None
                else None
            ),
        },
        "authorization": {
            # This is a one-way SHA-256 identity for the inputs used to make
            # the run, not prose or its readiness digest.  It lets users see
            # whether a record belongs to the expected content snapshot.
            "content_identity": str(authorization.get("content_identity") or ""),
            "authorization_revision": _safe_non_negative_int(
                authorization.get(
                    "authorization_revision",
                    document.get("authorization_revision"),
                )
            ),
            "automatic_continuations_per_scene": _safe_non_negative_int(
                policy.get("automatic_continuations_per_scene")
            ),
            "continuation_target_words": _safe_non_negative_int(
                policy.get("continuation_target_words")
            ),
            "max_base_calls": _safe_non_negative_int(
                authorization.get("max_base_calls")
            ),
            "max_automatic_continuation_calls": _safe_non_negative_int(
                authorization.get("max_automatic_continuation_calls")
            ),
            "max_logical_prose_calls": _safe_non_negative_int(
                authorization.get("max_logical_prose_calls")
            ),
            "conservative_token_bound": _safe_non_negative_int(
                authorization.get("conservative_token_bound")
            ),
        },
        "has_uncertain_attempt": _run_has_uncertain_attempt(document),
        "continuation_exhausted": continuation_exhausted,
        "created_at": document.get("created_at"),
        "updated_at": document.get("updated_at"),
    }


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
        replace_exhausted: bool = False,
        authorization: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        outline_revision = prose_revision(outline)
        context_revision = prose_revision(context_text)
        replace_run_id: str | None = None
        replace_revision: int | None = None
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
                    "thinking_mode",
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
            has_uncertain_attempt = bool(
                existing.get("has_uncertain_attempt")
                or (existing.get("active_token_reservation") or {}).get("state")
                == "uncertain"
                or any(
                    segment.get("status") == "uncertain"
                    for segment in existing.get("segments") or []
                )
            )
            if has_uncertain_attempt and not confirm_uncertain_retry:
                raise UncertainProseAttempt(
                    "存在已派发但未确认结果的正文请求，可能已经计费；"
                    "请明确确认可能重复计费后再继续"
                )
            # Replacement is a new explicit headless attempt, but it must never
            # bypass the uncertain-call billing acknowledgement above.
            revision = (
                int(expected_revision)
                if expected_revision is not None
                else int(existing.get("revision") or 0)
            )
            if not (
                replace_exhausted
                and _has_exhausted_segment(existing, plan)
            ):
                claimed = await prose_run_repo.claim(
                    run_id=run_id,
                    owner_id=owner_id,
                    expected_revision=revision,
                )
                lease_token = str(
                    (claimed.get("lease") or {}).get("token") or ""
                )
                if authorization is not None:
                    claimed = await prose_run_repo.update_authorization(
                        run_id=run_id,
                        owner_id=owner_id,
                        lease_token=lease_token,
                        authorization=dict(authorization),
                    )
                if has_uncertain_attempt:
                    acknowledged = await prose_run_repo.acknowledge_uncertain_call_budget(
                        run_id=run_id,
                        owner_id=owner_id,
                        lease_token=lease_token,
                        action="retry",
                    )
                    if not acknowledged:
                        current = await prose_run_repo.get_run(run_id, owner_id)
                        if (
                            (current.get("active_token_reservation") or {}).get("state")
                            == "uncertain"
                        ):
                            raise UncertainProseAttempt(
                                "正文不确定调用的预算状态已变化"
                            )
                    claimed = await prose_run_repo.get_run(run_id, owner_id)
                return claimed
            replace_run_id = run_id
            replace_revision = revision

        if run_id is None:
            active = await prose_run_repo.find_active(
                chapter_id=chapter_id,
                owner_id=owner_id,
            )
            if active is not None and str(
                ((active.get("plan") or {}).get("protocol_revision") or "")
            ) != plan.protocol_revision:
                # Legacy execution state is preserved for inspection only. A
                # clean request starts a new v3 draft; it never overwrites it.
                await prose_run_repo.mark_status(
                    run_id=str(active["_id"]),
                    owner_id=owner_id,
                    status="stale",
                )
        created = await prose_run_repo.create_run(
            {
                "owner_id": owner_id,
                "novel_id": novel_id,
                "chapter_id": chapter_id,
                "outline_revision": outline_revision,
                "prose_continuation_authorization": dict(authorization or {}),
                "authorization_revision": int(
                    (authorization or {}).get("authorization_revision") or 0
                ),
                "token_budget": (authorization or {}).get("token_budget"),
                "context_revision": context_revision,
                "plan": plan.to_dict(),
                "provider_plan": dict(provider_plan),
                "narrative_revision": (
                    await narrative_revision_store.current(novel_id)
                ),
                "completion": None,
                "assembled_text": "",
                "acceptance_state": None,
            },
            replace_run_id=replace_run_id,
            expected_revision=replace_revision,
        )
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

    async def list_leftovers(
        self,
        *,
        owner_id: str,
        novel_id: str,
    ) -> list[dict[str, Any]]:
        """Return read-only recovery summaries for unresolved prose drafts."""
        runs = await prose_run_repo.list_leftovers(
            novel_id=novel_id,
            owner_id=owner_id,
        )
        chapters = {
            str(chapter["_id"]): chapter
            for chapter in await chapter_repo.get_chapters_by_novel(novel_id)
        }
        narrative_revision = await narrative_revision_store.current(novel_id)
        summaries: list[dict[str, Any]] = []
        for run in runs:
            text = prose_run_draft_text(run)
            chapter = chapters.get(str(run.get("chapter_id")))
            narrative_current = bool(
                run.get("status") != "stale"
                and run.get("narrative_revision") is not None
                and int(run["narrative_revision"]) == narrative_revision
            )
            outline_current = bool(
                chapter is not None
                and _outline_revision_is_current(
                    run.get("outline_revision"),
                    chapter.get("outline") or {},
                )
            )
            continuation_exhausted = _stored_run_has_exhausted_segment(run)
            has_uncertain_attempt = _run_has_uncertain_attempt(run)
            has_live_lease = _run_has_live_lease(run)
            status = str(run.get("status") or "")
            summaries.append(
                {
                    "run_id": str(run["_id"]),
                    "novel_id": str(run["novel_id"]),
                    "chapter_id": str(run["chapter_id"]),
                    "revision": int(run.get("revision") or 0),
                    "status": status,
                    "assembled_text": text,
                    "draft_word_count": count_chapter_words(text),
                    "completion": (
                        dict(run["completion"])
                        if run.get("completion") is not None
                        else None
                    ),
                    "reason_codes": _leftover_reason_codes(run),
                    "continuation_exhausted": continuation_exhausted,
                    "has_uncertain_attempt": has_uncertain_attempt,
                    "can_resume": bool(
                        status in CURRENT_PROSE_RUN_STATUSES
                        and status == "incomplete"
                        and narrative_current
                        and outline_current
                        and not has_live_lease
                    ),
                    "can_accept_partial": bool(
                        text.strip()
                        and narrative_current
                        and outline_current
                        and not has_live_lease
                    ),
                    "can_discard": not has_live_lease,
                    "created_at": run.get("created_at"),
                    "updated_at": run.get("updated_at"),
                }
            )
        return summaries

    async def list_telemetry(
        self,
        *,
        owner_id: str,
        novel_id: str,
        limit: int = 100,
        skip: int = 0,
        chapter_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return user-owned, metadata-only prose-run inspection records."""
        runs = await prose_run_repo.list_telemetry_by_novel(
            owner_id=owner_id,
            novel_id=novel_id,
            limit=limit,
            skip=skip,
            chapter_id=chapter_id,
        )
        return [serialize_prose_run_telemetry(run) for run in runs]

    async def inspect_telemetry(
        self,
        *,
        owner_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        """Return one owned run's metadata without exposing prose or prompts."""
        run = await prose_run_repo.get_run(run_id, owner_id)
        return serialize_prose_run_telemetry(run)

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
        if not _outline_revision_is_current(
            run.get("outline_revision"),
            chapter.get("outline") or {},
        ):
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

    async def discard(
        self,
        *,
        owner_id: str,
        novel_id: str,
        chapter_id: str,
        run_id: str,
        expected_revision: int,
    ) -> None:
        await prose_run_repo.get_run(run_id, owner_id)
        await prose_run_repo.discard(
            run_id=run_id,
            owner_id=owner_id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            expected_revision=expected_revision,
        )

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
