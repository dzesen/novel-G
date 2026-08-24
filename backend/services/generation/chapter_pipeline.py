"""一章的批量管线：按 skip-existing 跑 细纲→正文→状态回填，自动接受每步。

生成与接受均以 ChapterPipelineDeps 注入，使本编排可脱离 LLM 与 MongoDB 用假件测。
真实依赖的装配在 headless_generation.py（Task 5）+ 服务层 accept（Task 6 组装）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List

from backend.services.generation.notices import (
    context_truncation_notice,
    partial_prose_blocks_state_notice,
    prose_incomplete_notice,
    outline_adherence_notice,
    reference_drop_notice,
    reference_remap_notice,
    state_all_character_updates_dropped_notice,
    step_outcome,
)
from backend.services.generation.outline_adherence import (
    ACCEPT_AND_CONTINUE,
    PAUSE_FOR_REWRITE,
    is_material_deviation,
    validate_outline_deviation_policy,
)
from backend.services.generation.job_planner import (
    REUSABLE_STATE_COMPLETION_STATUSES,
)
from backend.services.generation.candidate_repair_contracts import (
    JobMutationReceiptV1,
)


@dataclass
class ChapterOutcome:
    chapter_id: str
    order_index: int
    steps_done: List[str] = field(default_factory=list)
    steps_skipped: List[str] = field(default_factory=list)
    agents_used: List[str] = field(default_factory=list)
    tokens: int = 0
    consistency_issues: List[dict] = field(default_factory=list)
    outline_adherence: Dict[str, Any] = field(default_factory=dict)
    requires_outline_pause: bool = False
    facts_added: int = 0
    threads_advanced: int = 0
    summary_written: bool = False
    dropped_ids: Dict[str, Any] = field(default_factory=dict)
    truncations: List[dict] = field(default_factory=list)
    attempts: List[dict] = field(default_factory=list)
    step_outcomes: List[dict] = field(default_factory=list)
    notices: List[dict] = field(default_factory=list)
    prose_completion: Dict[str, Any] = field(default_factory=dict)
    authorization_recalculation: Dict[str, Any] = field(default_factory=dict)
    requires_authorization_confirmation: bool = False
    mutation_receipts: List[JobMutationReceiptV1] = field(default_factory=list)


class ChapterPipelineFailed(RuntimeError):
    """章节管线失败，同时保留失败前所有已发生的 attempt 与部分结果。"""

    def __init__(self, step: str, outcome: ChapterOutcome, cause: Exception) -> None:
        super().__init__(f"{step}: {cause}")
        self.step = step
        self.outcome = outcome
        self.attempts = list(outcome.attempts)
        self.__cause__ = cause


class IncompleteProseGeneration(ValueError):
    def __init__(self, completion: dict[str, Any]) -> None:
        reasons = ", ".join(completion.get("reason_codes") or []) or "unknown"
        super().__init__(f"正文未满足完整性要求: {reasons}")
        self.completion = dict(completion)


def _checkpoint_prose_completion(completion: dict[str, Any]) -> Dict[str, Any]:
    """Project only scalar prose result metadata into a batch checkpoint."""
    def non_negative_int(value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    return {
        "status": str(completion.get("status") or "unknown"),
        "requested_word_count": non_negative_int(
            completion.get("requested_word_count"),
        ),
        "actual_word_count": non_negative_int(completion.get("actual_word_count")),
    }


@dataclass(frozen=True)
class ChapterPipelineDeps:
    generate_outline: Callable[[str, dict], Awaitable[tuple]]
    generate_prose: Callable[[str, dict], Awaitable[tuple]]
    generate_state: Callable[[str, dict], Awaitable[tuple]]
    accept_outline: Callable[[str, dict], Awaitable[None]]
    write_prose: Callable[[str, str, dict[str, Any]], Awaitable[None]]
    accept_state: Callable[[str, dict], Awaitable[dict]]
    review_outline_adherence: (
        Callable[[str, dict], Awaitable[tuple]] | None
    ) = None
    recalculate_prose_authorization: (
        Callable[[str, dict], Awaitable[dict[str, Any]]] | None
    ) = None


def _has(chapter: Dict[str, Any], key: str) -> bool:
    if key == "outline":
        return bool(chapter.get("outline"))
    return bool(str(chapter.get(key) or "").strip())


def _record_truncation(outcome: ChapterOutcome, step: str, truncation: Dict[str, Any]) -> bool:
    """截断信号非空才记（设计 §5.1 的"本章在信息不全下生成"信号）；空的不进，常见路径无噪音。"""
    truncated_sections = truncation.get("truncated_sections") or []
    dropped_item_counts = truncation.get("dropped_item_counts") or {}
    if truncated_sections or dropped_item_counts:
        outcome.truncations.append({
            "step": step,
            "truncated_sections": truncated_sections,
            "dropped_item_counts": dropped_item_counts,
        })
        notice = context_truncation_notice(step, truncation)
        if notice is not None:
            outcome.notices.append(notice)
        return True
    return False


def _record_reference_drops(
    outcome: ChapterOutcome,
    step: str,
    dropped: Dict[str, Any],
) -> bool:
    outcome.dropped_ids.update(dropped)
    notice = reference_drop_notice(step, dropped)
    if notice is not None:
        outcome.notices.append(notice)
        return True
    return False


def _record_completed_step(outcome: ChapterOutcome, step: str, *, degraded: bool = False) -> None:
    outcome.step_outcomes.append(
        step_outcome(step, "degraded" if degraded else "generated")
    )


def _merge_attempts(outcome: ChapterOutcome, attempts: list[dict] | tuple[dict, ...]) -> None:
    existing = {str(item.get("attempt_id")) for item in outcome.attempts}
    for attempt in attempts:
        attempt_id = str(attempt.get("attempt_id") or "")
        if attempt_id and attempt_id not in existing:
            outcome.attempts.append(dict(attempt))
            existing.add(attempt_id)


def _capture_failure(outcome: ChapterOutcome, step: str, exc: Exception) -> ChapterPipelineFailed:
    _merge_attempts(outcome, list(getattr(exc, "attempts", []) or []))
    usage = getattr(exc, "usage", {}) or {}
    if not getattr(exc, "attempts", None):
        outcome.tokens += int(usage.get("total_tokens") or 0)
    return ChapterPipelineFailed(step, outcome, exc)


async def run_chapter(
    novel_id: str,
    chapter: Dict[str, Any],
    deps: ChapterPipelineDeps,
    *,
    outline_deviation_policy: str = PAUSE_FOR_REWRITE,
) -> ChapterOutcome:
    """跑一章剩余的管线子步；skip-existing 决策用入口快照（生成函数内部读库看得到本轮先前写入）。"""
    chapter_id = str(chapter["_id"])
    outcome = ChapterOutcome(chapter_id=chapter_id, order_index=int(chapter.get("order_index") or 0))
    deviation_policy = validate_outline_deviation_policy(
        outline_deviation_policy
    )

    # 1. 细纲
    if _has(chapter, "outline"):
        outcome.steps_skipped.append("outline")
        outcome.step_outcomes.append(step_outcome("outline", "reused", "existing_current"))
    else:
        try:
            generated = await deps.generate_outline(novel_id, chapter)
            result, dropped, tokens, truncation = generated[:4]
            attempts = generated[4] if len(generated) > 4 else []
            remapped = generated[5] if len(generated) > 5 else []
            _merge_attempts(outcome, attempts)
            outcome.tokens += tokens
            await deps.accept_outline(chapter_id, result)
        except Exception as exc:
            raise _capture_failure(outcome, "outline", exc) from exc
        outcome.steps_done.append("outline")
        outcome.agents_used.append("chapter_planner")
        degraded = _record_reference_drops(outcome, "outline", dropped)
        remap_notice = reference_remap_notice("outline", list(remapped or []))
        if remap_notice is not None:
            outcome.notices.append(remap_notice)
        degraded = _record_truncation(outcome, "outline", truncation) or degraded
        _record_completed_step(outcome, "outline", degraded=degraded)
        if deps.recalculate_prose_authorization is not None:
            try:
                recalculation = await deps.recalculate_prose_authorization(
                    chapter_id,
                    result,
                )
            except Exception as exc:
                raise _capture_failure(outcome, "outline", exc) from exc
            outcome.authorization_recalculation = dict(recalculation or {})
            if bool(recalculation.get("requires_confirmation")):
                outcome.requires_authorization_confirmation = True
                outcome.step_outcomes.append(
                    step_outcome(
                        "prose",
                        "blocked",
                        "authorization_scope_increased",
                    )
                )
                return outcome

    # 2. 正文。统一应用服务先持久化 ProseRun 分段，并只在完成契约通过后通过
    # ProseRun.accept mutation 写入正式正文；write_prose 保留为旧管线形状的兼容
    # 回调，生产装配中是无操作，避免第二条直接写库路径。
    if _has(chapter, "content"):
        outcome.steps_skipped.append("prose")
        outcome.step_outcomes.append(step_outcome("prose", "reused", "existing_current"))
    else:
        try:
            generated = await deps.generate_prose(novel_id, chapter)
            text, tokens, truncation = generated[:3]
            attempts = generated[3] if len(generated) > 3 else []
            completion = generated[4] if len(generated) > 4 else None
            if isinstance(completion, dict):
                # The checkpoint is metadata-only: no prose, prompt, or run identity.
                outcome.prose_completion = _checkpoint_prose_completion(completion)
            _merge_attempts(outcome, attempts)
            outcome.tokens += tokens
            if completion and not completion.get("can_write_formal_prose", False):
                reason_codes = list(completion.get("reason_codes") or [])
                outcome.step_outcomes.append(
                    step_outcome(
                        "prose",
                        "incomplete",
                        reason_codes[0] if reason_codes else "completion_contract_failed",
                    )
                )
                outcome.notices.append(prose_incomplete_notice(completion))
                raise IncompleteProseGeneration(completion)
            await deps.write_prose(chapter_id, text, dict(completion or {}))
        except Exception as exc:
            raise _capture_failure(outcome, "prose", exc) from exc
        outcome.steps_done.append("prose")
        outcome.agents_used.append("chapter_writer")
        degraded = _record_truncation(outcome, "prose", truncation)
        _record_completed_step(outcome, "prose", degraded=degraded)

    state_status = str(
        (chapter.get("state_completion") or {}).get("status") or "missing"
    )

    # 3. 细纲符合度审查。正文已经保存，便于用户在暂停后直接查看和重写；
    # 但在安全策略下，明显偏离不会进入状态回填，也不会影响下一章。
    if (
        state_status not in REUSABLE_STATE_COMPLETION_STATUSES
        and deps.review_outline_adherence is not None
    ):
        try:
            generated = await deps.review_outline_adherence(
                novel_id,
                chapter,
            )
            review, tokens, truncation = generated[:3]
            attempts = generated[3] if len(generated) > 3 else []
            _merge_attempts(outcome, attempts)
            outcome.tokens += tokens
            outcome.outline_adherence = dict(review)
        except Exception as exc:
            raise _capture_failure(
                outcome,
                "outline_adherence",
                exc,
            ) from exc
        outcome.steps_done.append("outline_adherence")
        if "continuity_editor" not in outcome.agents_used:
            outcome.agents_used.append("continuity_editor")
        degraded = _record_truncation(
            outcome,
            "outline_adherence",
            truncation,
        )
        decision = review.get("decision")
        policy_result = str(
            decision if decision is not None else review.get("verdict") or "warn"
        )
        passed = policy_result == "pass"
        _record_completed_step(
            outcome,
            "outline_adherence",
            degraded=degraded or not passed,
        )
        if not passed:
            requires_pause = (
                decision is not None
                or (
                    is_material_deviation(review)
                    and deviation_policy != ACCEPT_AND_CONTINUE
                )
            )
            outcome.notices.append(
                outline_adherence_notice(
                    review,
                    requires_pause=requires_pause,
                )
            )
            if requires_pause:
                outcome.requires_outline_pause = True
                outcome.step_outcomes.append(
                    step_outcome(
                        "state",
                        "blocked",
                        "outline_deviation",
                    )
                )
                return outcome

    # 4. 状态回填。摘要是作者文本，不再作为 accepted state delta 的替身。
    if state_status in REUSABLE_STATE_COMPLETION_STATUSES:
        outcome.steps_skipped.append("state")
        outcome.step_outcomes.append(step_outcome("state", "reused", "existing_current"))
    else:
        prose_state = str(
            (chapter.get("prose_acceptance") or {}).get("state")
            or (chapter.get("state_completion") or {}).get(
                "prose_acceptance_state"
            )
            or "unknown_legacy"
        )
        if prose_state == "partial_manual_required":
            notice = partial_prose_blocks_state_notice()
            outcome.notices.append(notice)
            outcome.step_outcomes.append(
                step_outcome("state", "blocked", "partial_prose")
            )
            raise _capture_failure(
                outcome,
                "state",
                ValueError(
                    "部分正文尚未人工补写并标记完成，不能执行状态回填"
                ),
            )
        try:
            generated = await deps.generate_state(novel_id, chapter)
            result, dropped, tokens, truncation = generated[:4]
            attempts = generated[4] if len(generated) > 4 else []
            accepted_report = generated[5] if len(generated) > 5 else None
            _merge_attempts(outcome, attempts)
            outcome.tokens += tokens
            outcome.consistency_issues = list(result.get("consistency_issues", []))
            degraded = _record_reference_drops(outcome, "state", dropped)
            degraded = _record_truncation(outcome, "state", truncation) or degraded
            report = (
                dict(accepted_report)
                if accepted_report is not None
                else await deps.accept_state(chapter_id, result)
            )
            reference_resolution = (
                (report.get("state_completion") or {}).get(
                    "reference_resolution"
                )
                or {}
            )
            remap_notice = reference_remap_notice(
                "state",
                list(reference_resolution.get("remapped") or [])
            )
            if remap_notice is not None:
                outcome.notices.append(remap_notice)
            completion_reason = str(
                (report.get("state_completion") or {}).get(
                    "completion_reason"
                )
                or ""
            )
            if completion_reason == "all_character_updates_dropped":
                outcome.notices.append(
                    state_all_character_updates_dropped_notice(dropped)
                )
                degraded = True
        except Exception as exc:
            raise _capture_failure(outcome, "state", exc) from exc
        outcome.steps_done.append("state")
        if "continuity_editor" not in outcome.agents_used:
            outcome.agents_used.append("continuity_editor")
        outcome.facts_added += int(report.get("facts_appended", 0))
        outcome.threads_advanced += int(report.get("threads_updated", 0))
        outcome.summary_written = True
        _record_completed_step(outcome, "state", degraded=degraded)

    return outcome
