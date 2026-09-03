"""Deep module around persisted prose-run lifecycle and stale checks."""
from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from datetime import datetime
from typing import Any, Mapping

from backend.db import collections
from backend.db.errors import InvalidIdError, NotFoundError
from backend.db.mongo import get_database
from backend.db.mutation import MutationCommand, commit_mutation
from backend.db.narrative_revision import narrative_revision_store
from backend.db.repositories.agent_runtime_repository import (
    agent_runtime_repository,
)
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.generation_job_repository import generation_job_repo
from backend.db.repositories.prose_run_repository import (
    CURRENT_PROSE_RUN_STATUSES,
    prose_run_repo,
)
from backend.db.utils import get_utc_now, to_object_id
from backend.services.generation.prose_completion import ProseExecutionPlan
from backend.services.generation.chapter_completion_certificate import (
    ChapterCompletionCertificate,
)
from backend.services.generation.prose_protocol import (
    is_scene_continuation_v3_family,
)
from backend.services.generation.prose_generation import UncertainProseAttempt
from backend.services.generation.required_initial_prose_contracts import (
    RequiredInitialProseOrigin,
)
from backend.scene_contract_versions import require_known_scene_contract_version
from backend.services.generation.job_relations import related_prose_run_ids
from backend.services.llm.context_builder import normalize_outline_references
from backend.services.novel.chapter_service import count_chapter_words
from backend.services.novel.derived_stats import derived_stats
from backend.services.novel.state_completion import chapter_content_digest


ACCEPT_PROSE_RUN_COMMAND_VERSION = 2


def prose_revision(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


_REQUIRED_COMPLETION_PROMOTION_KEYS = frozenset({
    "schema_version",
    "finalization_job_id",
    "readiness_digest",
    "authorization_contract_digest",
    "reviewed_job_id",
    "reviewed_result_digest",
    "state_job_id",
    "state_result_digest",
    "source_run_id",
    "source_run_revision",
    "source_content_digest",
    "stored_completion_digest",
    "effective_completion_digest",
    "promotion_digest",
})


def _validated_required_completion_promotion(
    value: Mapping[str, Any],
    *,
    run_id: str,
    run_revision: int,
    text_digest: str,
    effective_completion: Mapping[str, Any],
    stored_completion: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Validate the one-way reviewed-candidate completion promotion proof."""

    from backend.services.generation.required_chapter_state_contracts import (
        required_state_digest,
    )

    if not isinstance(value, Mapping) or set(value) != _REQUIRED_COMPLETION_PROMOTION_KEYS:
        raise ValueError("已审查正文完成升级证明格式无效")
    parsed = deepcopy(dict(value))
    promotion_digest = parsed.pop("promotion_digest", None)
    effective = deepcopy(dict(effective_completion))
    if (
        parsed.get("schema_version")
        != "required_reviewed_completion_promotion.v1"
        or parsed.get("source_run_id") != str(run_id)
        or parsed.get("source_run_revision") != int(run_revision)
        or parsed.get("source_content_digest") != str(text_digest)
        or required_state_digest(parsed) != promotion_digest
        or parsed.get("effective_completion_digest")
        != required_state_digest(effective)
        or effective.get("status") != "complete"
        or effective.get("finish_reason") != "stop"
        or effective.get("can_write_formal_prose") is not True
    ):
        raise ValueError("已审查正文完成升级证明已经漂移")
    if stored_completion is not None:
        stored = deepcopy(dict(stored_completion))
        expected_effective = {**stored, "can_write_formal_prose": True}
        if (
            stored.get("status") != "complete"
            or stored.get("finish_reason") != "stop"
            or stored.get("can_write_formal_prose") is not False
            or parsed.get("stored_completion_digest")
            != required_state_digest(stored)
            or effective != expected_effective
        ):
            raise ValueError("非正式正文不满足完成升级前置条件")
    return dict(value)


def _normalize_context_lineage(
    value: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Keep persisted context evidence metadata-only and structurally closed."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("正文上下文 lineage 必须是对象")
    if value.get("schema_version") != "narrative_context_lineage.v1":
        raise ValueError("正文上下文 lineage 版本无效")
    projection_digest = str(value.get("projection_digest") or "")
    if len(projection_digest) != 64 or any(
        character not in "0123456789abcdef" for character in projection_digest
    ):
        raise ValueError("正文上下文 projection 摘要无效")
    target_book_ordinal = value.get("target_book_ordinal")
    if (
        type(target_book_ordinal) is not int
        or target_book_ordinal < 1
    ):
        raise ValueError("正文上下文目标章节序号无效")
    prior: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for raw in value.get("prior_state_deltas") or []:
        if not isinstance(raw, dict):
            raise ValueError("正文上下文前序状态 lineage 无效")
        source_chapter_id = str(raw.get("chapter_id") or "")
        try:
            to_object_id(source_chapter_id)
        except (InvalidIdError, TypeError, ValueError) as exc:
            raise ValueError("正文上下文前序章节 ID 无效") from exc
        book_ordinal = raw.get("book_ordinal")
        delta_revision = raw.get("delta_revision")
        if (
            type(book_ordinal) is not int
            or book_ordinal < 1
            or book_ordinal >= target_book_ordinal
            or type(delta_revision) is not int
            or delta_revision < 1
        ):
            raise ValueError("正文上下文前序状态 revision 无效")
        identity = (source_chapter_id, delta_revision)
        if identity in seen:
            raise ValueError("正文上下文前序状态 lineage 重复")
        seen.add(identity)
        normalized_prior: dict[str, Any] = {
            "chapter_id": source_chapter_id,
            "book_ordinal": book_ordinal,
            "delta_revision": delta_revision,
        }
        delta_digest_keys = (
            "delta_projection_digest",
            "delta_summary_digest",
        )
        if any(key in raw for key in delta_digest_keys) and not all(
            key in raw for key in delta_digest_keys
        ):
            raise ValueError("正文上下文前序状态摘要证据不完整")
        for key in delta_digest_keys:
            if key not in raw:
                continue
            digest = str(raw.get(key) or "")
            if len(digest) != 64 or any(
                character not in "0123456789abcdef"
                for character in digest
            ):
                raise ValueError("正文上下文前序状态摘要证据无效")
            normalized_prior[key] = digest
        model_evidence_keys = (
            "model_context_section",
            "model_context_summary_digest",
            "model_context_item_digest",
            "model_context_material_digest",
        )
        if any(key in raw for key in model_evidence_keys):
            if not all(key in raw for key in model_evidence_keys) or not all(
                key in normalized_prior for key in delta_digest_keys
            ):
                raise ValueError("正文上下文逐章模型证据不完整")
            section = str(raw.get("model_context_section") or "")
            model_summary_digest = str(
                raw.get("model_context_summary_digest") or ""
            )
            item_digest = str(raw.get("model_context_item_digest") or "")
            material_digest = str(
                raw.get("model_context_material_digest") or ""
            )
            if (
                section != "recent_chapters"
                or model_summary_digest
                != normalized_prior["delta_summary_digest"]
                or any(
                    len(digest) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for character in digest
                    )
                    for digest in (
                        model_summary_digest,
                        item_digest,
                        material_digest,
                    )
                )
                or material_digest
                != prose_revision({
                    "delta_projection_digest": normalized_prior[
                        "delta_projection_digest"
                    ],
                    "delta_summary_digest": normalized_prior[
                        "delta_summary_digest"
                    ],
                    "model_context_item_digest": item_digest,
                    "model_context_section": section,
                })
            ):
                raise ValueError("正文上下文逐章模型证据无效")
            normalized_prior.update({
                "model_context_section": section,
                "model_context_summary_digest": model_summary_digest,
                "model_context_item_digest": item_digest,
                "model_context_material_digest": material_digest,
            })
        prior.append(normalized_prior)
    if prior != sorted(
        prior,
        key=lambda item: (
            item["book_ordinal"],
            item["delta_revision"],
            item["chapter_id"],
        ),
    ):
        raise ValueError("正文上下文前序状态 lineage 顺序无效")
    result: dict[str, Any] = {
        "schema_version": "narrative_context_lineage.v1",
        "projection_digest": projection_digest,
        "target_book_ordinal": target_book_ordinal,
        "prior_state_deltas": prior,
    }
    for key in (
        "state_projection_digest",
        "thread_projection_digest",
        "prompt_context_digest",
    ):
        if key not in value:
            continue
        digest = str(value.get(key) or "")
        if len(digest) != 64 or any(
            character not in "0123456789abcdef"
            for character in digest
        ):
            raise ValueError("正文上下文 lineage 摘要无效")
        result[key] = digest
    if "state_sections" in value:
        raw_sections = value.get("state_sections")
        if not isinstance(raw_sections, list) or len(raw_sections) > 5:
            raise ValueError("正文上下文状态段 lineage 无效")
        sections: list[dict[str, str]] = []
        seen_names: set[str] = set()
        allowed_names = {
            "recent_chapters",
            "present_states",
            "permanent_facts",
            "threads_to_resolve",
            "other_threads",
        }
        for raw in raw_sections:
            if not isinstance(raw, dict):
                raise ValueError("正文上下文状态段 lineage 无效")
            name = str(raw.get("name") or "")
            digest = str(raw.get("section_digest") or "")
            if (
                name not in allowed_names
                or name in seen_names
                or len(digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in digest
                )
            ):
                raise ValueError("正文上下文状态段 lineage 无效")
            seen_names.add(name)
            sections.append({"name": name, "section_digest": digest})
        result["state_sections"] = sections
    return result


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
    for field in (
        "_id",
        "owner_id",
        "novel_id",
        "chapter_id",
        "generation_job_id",
    ):
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


_CONTINUATION_EXHAUSTION_PAUSE_REASONS = frozenset({
    "automatic_continuations_exhausted",
    "provider_length_continuation_capacity_exhausted",
})


def _stored_run_has_exhausted_continuation(document: dict[str, Any]) -> bool:
    return _stored_run_has_exhausted_segment(document) or any(
        str(item.get("pause_reason") or "")
        in _CONTINUATION_EXHAUSTION_PAUSE_REASONS
        for item in document.get("scene_progress") or []
        if isinstance(item, Mapping)
    )


def _run_has_uncertain_attempt(document: dict[str, Any]) -> bool:
    return any(
        segment.get("status") == "uncertain"
        for segment in document.get("segments") or []
    )


def _stored_plan_identity_for_resume(
    stored_plan: dict[str, Any],
    *,
    outline: dict[str, Any],
) -> dict[str, Any]:
    """Backfill only derivable fields absent from pre-V2 legacy-outline runs."""

    normalized = dict(stored_plan)
    if require_known_scene_contract_version(outline) != "legacy_v1":
        return normalized
    budgets = normalized.get("segment_budgets")
    ratio = normalized.get("minimum_completion_ratio")
    if "segment_minimums" not in normalized:
        try:
            normalized["segment_minimums"] = [
                math.ceil(int(budget) * float(ratio))
                for budget in list(budgets)
            ]
        except (TypeError, ValueError, OverflowError):
            return normalized
    if "segment_maximums" not in normalized:
        normalized["segment_maximums"] = []
    return normalized


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
                "word_budget_trimmed_segments": _safe_non_negative_int(
                    item.get("word_budget_trimmed_segments")
                ),
                "word_budget_discarded_words": _safe_non_negative_int(
                    item.get("word_budget_discarded_words")
                ),
                "word_budget_word_boundary_fallbacks": _safe_non_negative_int(
                    item.get("word_budget_word_boundary_fallbacks")
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
                "advisory_codes": [
                    str(code)
                    for code in item.get("advisory_codes") or []
                    if str(code).strip()
                ][:10],
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
    scheduled_base_calls = _safe_non_negative_int(
        plan.get("scheduled_base_call_count", plan.get("call_count"))
    )
    maximum_base_calls = _safe_non_negative_int(
        plan.get(
            "maximum_base_call_count",
            plan.get("call_count", scheduled_base_calls),
        )
    )
    reserved_length_calls = _safe_non_negative_int(
        plan.get(
            "reserved_length_continuation_call_count",
            max(0, maximum_base_calls - scheduled_base_calls),
        )
    )
    scene_progress = _telemetry_scene_progress(document)
    continuation_exhausted = _stored_run_has_exhausted_continuation(document)
    return {
        "run_id": str(document["_id"]),
        "novel_id": str(document["novel_id"]),
        "chapter_id": str(document["chapter_id"]),
        "generation_job_id": (
            str(document["generation_job_id"])
            if document.get("generation_job_id") is not None
            else None
        ),
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
            "scheduled_base_call_count": scheduled_base_calls,
            "reserved_length_continuation_call_count": reserved_length_calls,
            "maximum_base_call_count": maximum_base_calls,
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
            "advisory_codes": [
                str(code)
                for code in completion.get("advisory_codes") or []
                if str(code).strip()
            ][:10],
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
        context_lineage: dict[str, Any] | None = None,
        generation_job_id: str | None = None,
        run_id: str | None = None,
        expected_revision: int | None = None,
        confirm_uncertain_retry: bool = False,
        replace_exhausted: bool = False,
        authorization: dict[str, Any] | None = None,
        required_initial_origin: RequiredInitialProseOrigin | None = None,
    ) -> dict[str, Any]:
        initial_origin = (
            None
            if required_initial_origin is None
            else RequiredInitialProseOrigin.model_validate_json(
                required_initial_origin.model_dump_json()
            )
        )
        if initial_origin is not None and (
            generation_job_id != initial_origin.job_id
            or chapter_id != initial_origin.chapter_id
            or prose_revision(outline) != initial_origin.outline_revision
        ):
            raise ValueError("初稿来源与当前作业或章纲不匹配")
        outline_revision = prose_revision(outline)
        context_revision = prose_revision(context_text)
        normalized_context_lineage = _normalize_context_lineage(
            context_lineage
        )
        replace_run_id: str | None = None
        replace_revision: int | None = None
        if run_id:
            existing = await prose_run_repo.get_run(run_id, owner_id)
            try:
                existing_context_lineage = _normalize_context_lineage(
                    existing.get("context_lineage")
                )
            except ValueError as exc:
                await prose_run_repo.mark_status(
                    run_id=run_id,
                    owner_id=owner_id,
                    status="stale",
                )
                raise ValueError(
                    "正文草稿缺少有效的上下文来源证据，不能继续自动拼接"
                ) from exc
            if (
                generation_job_id is not None
                and str(existing.get("generation_job_id") or "")
                != generation_job_id
            ):
                raise ValueError("正文草稿不属于当前生成作业")
            if initial_origin is not None and (
                existing.get("required_initial_origin") is None
                or RequiredInitialProseOrigin.model_validate(
                    existing.get("required_initial_origin")
                ) != initial_origin
            ):
                raise ValueError("正文草稿不属于当前初稿请求")
            if (
                str(existing.get("chapter_id")) != str(chapter_id)
                or existing.get("outline_revision") != outline_revision
                or existing.get("context_revision") != context_revision
                or existing_context_lineage != normalized_context_lineage
            ):
                await prose_run_repo.mark_status(
                    run_id=run_id,
                    owner_id=owner_id,
                    status="stale",
                )
                raise ValueError(
                    "正文草稿基于旧细纲、旧上下文或旧来源证据，"
                    "不能静默续写；请保留旧稿参考并重新生成"
                )
            if _stored_plan_identity_for_resume(
                dict(existing.get("plan") or {}),
                outline=outline,
            ) != plan.to_dict():
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
        document = {
            "owner_id": owner_id,
            "novel_id": novel_id,
            "chapter_id": chapter_id,
            "generation_job_id": generation_job_id,
            "outline_revision": outline_revision,
            "prose_continuation_authorization": dict(authorization or {}),
            "authorization_revision": int(
                (authorization or {}).get("authorization_revision") or 0
            ),
            "token_budget": (authorization or {}).get("token_budget"),
            "context_revision": context_revision,
            **(
                {"context_lineage": normalized_context_lineage}
                if normalized_context_lineage is not None
                else {}
            ),
            "plan": plan.to_dict(),
            "provider_plan": dict(provider_plan),
            "narrative_revision": (
                await narrative_revision_store.current(novel_id)
            ),
            "completion": None,
            "assembled_text": "",
            "acceptance_state": None,
            **(
                {
                    "required_initial_origin": initial_origin.model_dump(
                        mode="json"
                    )
                }
                if initial_origin is not None
                else {}
            ),
        }
        created = await prose_run_repo.create_run(
            document,
            replace_run_id=replace_run_id,
            expected_revision=replace_revision,
            require_no_current=(
                initial_origin is not None and replace_run_id is None
            ),
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
            continuation_exhausted = _stored_run_has_exhausted_continuation(run)
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
        generation_job_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return user-owned, metadata-only prose-run inspection records."""
        related_run_ids: tuple[str, ...] = ()
        if generation_job_id is not None:
            job = await generation_job_repo.get_job(generation_job_id)
            if str(job.get("novel_id") or "") != novel_id:
                raise NotFoundError(
                    f"Generation job not found for novel: {generation_job_id}"
                )
            related_run_ids = related_prose_run_ids(job)
        runs = await prose_run_repo.list_telemetry_by_novel(
            owner_id=owner_id,
            novel_id=novel_id,
            limit=limit,
            skip=skip,
            chapter_id=chapter_id,
            generation_job_id=generation_job_id,
            related_run_ids=related_run_ids,
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

    async def load_candidate_text_for_validation(
        self,
        *,
        owner_id: str,
        run_id: str,
        chapter_id: str,
        expected_revision: int,
        expected_digest: str,
    ) -> str:
        """Read the exact draft used to revalidate evidence before mutation."""

        run = await prose_run_repo.get_run(run_id, owner_id)
        if (
            str(run.get("chapter_id") or "") != str(chapter_id)
            or int(run.get("revision") or 0) != int(expected_revision)
        ):
            raise ValueError("正文候选验证快照已经变化")
        text = prose_run_draft_text(run)
        if chapter_content_digest(text) != str(expected_digest):
            raise ValueError("正文候选验证快照摘要已经变化")
        return text

    async def _load_accept_context(
        self,
        *,
        owner_id: str,
        run_id: str,
        chapter_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        run = await prose_run_repo.get_run(run_id, owner_id)
        fence = dict(run.get("remediation_write_fence") or {})
        if fence:
            expires_at = fence.get("expires_at")
            if (
                not str(fence.get("token") or "")
                or not isinstance(expires_at, datetime)
                or expires_at > get_utc_now()
            ):
                raise ValueError("正文候选正在提交修复检查，请稍后重试")
            released = (
                await prose_run_repo.release_expired_remediation_write_fence(
                    run_id=run_id,
                    owner_id=owner_id,
                    novel_id=str(run["novel_id"]),
                    fence_token=str(fence["token"]),
                    expires_at=expires_at,
                )
            )
            if not released:
                run = await prose_run_repo.get_run(run_id, owner_id)
                current_fence = dict(
                    run.get("remediation_write_fence") or {}
                )
                current_expiry = current_fence.get("expires_at")
                if (
                    current_fence
                    and (
                        not isinstance(current_expiry, datetime)
                        or current_expiry > get_utc_now()
                    )
                ):
                    raise ValueError(
                        "正文候选正在提交修复检查，请稍后重试"
                    )
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
        remediation = dict(run.get("remediation") or {})
        if remediation:
            if remediation.get("schema_version") != "prose_run_remediation.v1":
                raise ValueError("正文修复证据版本未知，不能正式接受")
            verification = dict(remediation.get("verification") or {})
            text_digest = chapter_content_digest(prose_run_draft_text(run))
            if (
                verification.get("schema_version")
                != "prose_remediation_verification.v1"
                or int(verification.get("candidate_revision") or 0)
                != int(expected_revision)
                or str(verification.get("content_digest") or "")
                != text_digest
                or not str(verification.get("agent_run_id") or "")
            ):
                raise ValueError("正文修复复检证据与当前候选不一致")
            try:
                await agent_runtime_repository.assert_completed_remediation(
                    run_id=str(verification["agent_run_id"]),
                    owner_id=owner_id,
                    novel_id=str(run.get("novel_id") or ""),
                    prose_run_id=str(run["_id"]),
                )
            except (NotFoundError, ValueError) as exc:
                raise ValueError(
                    "正文修复 Agent 尚未完成，不能正式接受"
                ) from exc
        text = prose_run_draft_text(run)
        if not text.strip():
            raise ValueError("正文草稿为空，不能接受")
        return {
            "run": run,
            "chapter": chapter,
            "completion": completion,
            "text": text,
            "text_digest": chapter_content_digest(text),
            "captured_narrative_revision": int(captured_narrative_revision),
        }

    async def inspect_ai_completion_candidate(
        self,
        *,
        owner_id: str,
        run_id: str,
        chapter_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Return the exact current candidate without creating a mutation intent."""

        context = await self._load_accept_context(
            owner_id=owner_id,
            run_id=run_id,
            chapter_id=chapter_id,
            expected_revision=expected_revision,
        )
        if not bool(context["completion"].get("can_write_formal_prose")):
            raise ValueError("正文尚未完成；不能进入 AI 完成评估")
        return context

    async def inspect_required_reviewed_completion_candidate(
        self,
        *,
        owner_id: str,
        run_id: str,
        chapter_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Read a complete, independently reviewed candidate before promotion.

        This Interface does not grant write authority.  It only recognizes the
        deliberately deferred completion shape produced by the review
        successor; the finalization Module must supply a current certificate
        and a separate promotion proof before ``prepare_accept_mutation`` can
        turn it into formal prose.
        """

        context = await self._load_accept_context(
            owner_id=owner_id,
            run_id=run_id,
            chapter_id=chapter_id,
            expected_revision=expected_revision,
        )
        completion = dict(context["completion"] or {})
        if (
            completion.get("status") != "complete"
            or completion.get("finish_reason") != "stop"
            or completion.get("can_write_formal_prose") is not False
        ):
            raise ValueError("正文不是可由 successor 升级的已审查完整候选")
        return context

    async def prepare_accept_mutation(
        self,
        *,
        owner_id: str,
        run_id: str,
        chapter_id: str,
        expected_revision: int,
        accept_partial: bool,
        partial_acknowledgement: bool,
        chapter_completion_certificate: Mapping[str, Any] | None = None,
        required_completion_promotion: Mapping[str, Any] | None = None,
    ) -> MutationCommand:
        context = await self._load_accept_context(
            owner_id=owner_id,
            run_id=run_id,
            chapter_id=chapter_id,
            expected_revision=expected_revision,
        )
        run = context["run"]
        chapter = context["chapter"]
        stored_completion = dict(context["completion"] or {})
        text_digest = str(context["text_digest"])
        captured_narrative_revision = int(
            context["captured_narrative_revision"]
        )
        serialized_promotion: dict[str, Any] | None = None
        completion = stored_completion
        if required_completion_promotion is not None:
            if accept_partial:
                raise ValueError("部分正文不能携带 successor 完成升级证明")
            effective = {
                **stored_completion,
                "can_write_formal_prose": True,
            }
            serialized_promotion = _validated_required_completion_promotion(
                required_completion_promotion,
                run_id=run_id,
                run_revision=expected_revision,
                text_digest=text_digest,
                effective_completion=effective,
                stored_completion=stored_completion,
            )
            completion = effective
        can_write = bool(completion.get("can_write_formal_prose"))
        if not can_write and not accept_partial:
            raise ValueError("正文尚未完成；只能继续生成或明确接受部分正文")
        if accept_partial and not partial_acknowledgement:
            raise ValueError("接受部分正文前必须确认仍需人工补写")

        serialized_certificate: dict[str, Any] | None = None
        if accept_partial:
            if chapter_completion_certificate is not None:
                raise ValueError("部分正文不能绑定章节完成证书")
        else:
            if chapter_completion_certificate is None:
                raise ValueError("AI 完整正文必须先取得当前章节完成证书")
            try:
                certificate = ChapterCompletionCertificate.model_validate(
                    chapter_completion_certificate
                )
            except ValueError as exc:
                raise ValueError("章节完成证书格式或摘要无效") from exc
            source = certificate.source_binding
            binding = certificate.chapter_binding
            if (
                source.prose_run_id != str(run_id)
                or source.prose_run_revision != int(expected_revision)
                or source.content_digest != text_digest
                or source.expected_narrative_revision_before_commit
                != captured_narrative_revision
                or binding.owner_id != str(owner_id)
                or binding.novel_id != str(run["novel_id"])
                or binding.chapter_id != str(chapter_id)
                or binding.volume_id != str(chapter.get("volume_id") or "")
            ):
                raise ValueError("章节完成证书没有绑定当前正文候选")
            if serialized_promotion is not None and (
                certificate.authorization_binding.kind != "job_readiness"
                or certificate.authorization_binding.job_id
                != serialized_promotion["finalization_job_id"]
                or certificate.authorization_binding.readiness_digest
                != serialized_promotion["readiness_digest"]
            ):
                raise ValueError("章节完成证书没有绑定 successor 正式授权")
            serialized_certificate = certificate.model_dump(mode="json")

        acceptance_state = (
            "partial_manual_required" if accept_partial else "ai_complete"
        )
        command = MutationCommand(
            novel_id=str(run["novel_id"]),
            idempotency_key=(
                f"accept-prose-run:{run_id}:{expected_revision}:"
                f"{acceptance_state}:{text_digest[:16]}"
            ),
            operation="accept_prose_run",
            version=ACCEPT_PROSE_RUN_COMMAND_VERSION,
            payload={
                "run_id": run_id,
                "owner_id": owner_id,
                "chapter_id": chapter_id,
                "expected_revision": int(expected_revision),
                "text_digest": text_digest,
                "acceptance_state": acceptance_state,
                "accepted_partial": bool(accept_partial),
                "content_origin": "ai",
                "completion": completion,
                "required_completion_promotion": serialized_promotion,
                "captured_narrative_revision": int(captured_narrative_revision),
                "chapter_completion_certificate": serialized_certificate,
            },
            before_image={
                "chapter": {"status": chapter.get("status")},
                "prose_run": {
                    "status": run.get("status"),
                    "revision": int(run.get("revision") or 0),
                },
            },
        )
        return command

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
        command = await self.prepare_accept_mutation(
            owner_id=owner_id,
            run_id=run_id,
            chapter_id=chapter_id,
            expected_revision=expected_revision,
            accept_partial=accept_partial,
            partial_acknowledgement=partial_acknowledgement,
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
        command_envelope = mutation.journal["command"]
        command_version = int(command_envelope.get("version") or 0)
        if command_version not in {1, 2}:
            raise ValueError("正文接受命令版本未知")
        command = command_envelope["payload"]
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

        command_completion = command.get("completion")
        if not isinstance(command_completion, Mapping):
            raise ValueError("正文接受命令缺少完成证据")
        effective_completion = deepcopy(dict(command_completion))
        raw_promotion = command.get("required_completion_promotion")
        if raw_promotion is None:
            if effective_completion != dict(run.get("completion") or {}):
                raise ValueError("正文接受命令的完成证据已经变化")
        else:
            _validated_required_completion_promotion(
                raw_promotion,
                run_id=run_id,
                run_revision=expected_revision,
                text_digest=command["text_digest"],
                effective_completion=effective_completion,
                stored_completion=(
                    None
                    if already_applied
                    else dict(run.get("completion") or {})
                ),
            )
            if already_applied and dict(run.get("completion") or {}) != effective_completion:
                raise ValueError("已完成升级的正文证据与重放命令不一致")

        serialized_certificate: dict[str, Any] | None = None
        if command_version == 2:
            raw_certificate = command.get("chapter_completion_certificate")
            if command["acceptance_state"] == "ai_complete":
                if not isinstance(raw_certificate, Mapping):
                    raise ValueError("AI 完整正文缺少章节完成证书")
                try:
                    certificate = ChapterCompletionCertificate.model_validate(
                        raw_certificate
                    )
                except ValueError as exc:
                    raise ValueError("章节完成证书格式或摘要无效") from exc
                if (
                    certificate.source_binding.prose_run_id != run_id
                    or certificate.source_binding.prose_run_revision
                    != expected_revision
                    or certificate.source_binding.content_digest
                    != command["text_digest"]
                    or certificate.chapter_binding.chapter_id != chapter_id
                    or certificate.chapter_binding.owner_id != owner_id
                    or certificate.chapter_binding.novel_id
                    != str(run["novel_id"])
                ):
                    raise ValueError("章节完成证书没有绑定当前正文候选")
                if raw_promotion is not None and (
                    certificate.authorization_binding.kind != "job_readiness"
                    or certificate.authorization_binding.job_id
                    != raw_promotion["finalization_job_id"]
                    or certificate.authorization_binding.readiness_digest
                    != raw_promotion["readiness_digest"]
                ):
                    raise ValueError("章节完成证书没有绑定 successor 正式授权")
                serialized_certificate = certificate.model_dump(mode="json")
            elif raw_certificate is not None:
                raise ValueError("部分正文不能绑定章节完成证书")

        accepted_at = get_utc_now()
        acceptance = {
            "state": command["acceptance_state"],
            "content_origin": str(command.get("content_origin") or "ai"),
            "accepted_partial": bool(command.get("accepted_partial")),
            "source_run_id": run_id,
            "content_digest": command["text_digest"],
            "completion_status": effective_completion.get("status"),
            "finish_reason": effective_completion.get("finish_reason"),
            "accepted_at": accepted_at,
        }
        if serialized_certificate is not None:
            acceptance["chapter_completion_certificate"] = (
                serialized_certificate
            )
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
                        "completion": effective_completion,
                    },
                    "$inc": {"revision": 1},
                },
                session=session,
            )
            if update.modified_count != 1:
                raise ValueError("正文草稿接受发生并发冲突")
            await mutation.receipt("prose_run", {"run_id": run_id})
        if not command.get("defer_derived_stats"):
            await mutation.advance_phase("derived_data")
            stats = await derived_stats.refresh(
                str(run["novel_id"]), session=session
            )
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
