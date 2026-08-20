"""Production adapter between one GenerationJob chapter and candidate pipeline.

This module owns the deliberately narrow seam between the durable Job ledger and
the candidate-only orchestration.  It reconstructs live candidates from the
append-only checkpoint prefix before allowing the pipeline to continue, so a
process restart never turns a persisted paid step into a second Provider call.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from backend.services.generation.candidate_repair_contracts import (
    AdherenceCandidateCheckpointV1,
    CandidatePipelineCheckpointV1,
    CandidatePipelineProgressV1,
    ProseCandidateCheckpointV1,
    StateCandidateCheckpointV1,
    parse_candidate_pipeline_checkpoint,
    replay_candidate_pipeline_checkpoints,
)
from backend.services.generation.chapter_candidate_pipeline import (
    CandidateAttemptPhase,
    CandidateAttemptState,
    CandidateAttemptSummary,
    CandidateTruncationSummary,
    CandidateUsageSummary,
    ChapterCandidatePipeline,
    ChapterCandidatePipelineBlocked,
    ChapterCandidatePipelineDeps,
    ChapterCandidatePipelineProgress,
    ChapterCandidatePipelineResume,
    ProseCandidateRepairReceipt,
    ProseCandidateRepairRequest,
    StateCandidateRepairReceipt,
    StateCandidateRepairRequest,
)
from backend.services.generation.chapter_generation_application import (
    ChapterGenerationResult,
    ChapterGenerationStage,
    ProseCandidateSource,
)
from backend.services.generation.headless_generation import GeneratedProseCandidate
from backend.services.generation.job_engine import CandidateChapterOutcome


_MAX_TOKEN_COUNT = 1_000_000_000


class _FinalizationDependencyError(RuntimeError):
    """Expose a known zero-Provider boundary to the pipeline evidence collector."""

    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    attempts: list[dict[str, Any]] = []


@dataclass(frozen=True)
class CandidateJobExecution:
    """Frozen live adapters that have already matched the Job readiness."""

    max_repair_cycles: int
    adherence_plan: Any
    state_plan: Any
    repair_prose_candidate: Callable[
        [str, str, str, ProseCandidateRepairRequest],
        Awaitable[ProseCandidateRepairReceipt],
    ] | None
    repair_state_candidate: Callable[
        [str, str, str, StateCandidateRepairRequest],
        Awaitable[StateCandidateRepairReceipt],
    ] | None
    recover_source: Callable[..., Awaitable[ProseCandidateSource]]


@dataclass(frozen=True)
class ChapterCandidateJobRunnerDeps:
    get_novel: Callable[[str], Awaitable[Mapping[str, Any]]]
    get_chapter: Callable[[str], Awaitable[Mapping[str, Any]]]
    list_checkpoints: Callable[..., Awaitable[Sequence[Any]]]
    append_checkpoint: Callable[..., Awaitable[Any]]
    list_attempts: Callable[..., Awaitable[Sequence[Mapping[str, Any]]]]
    attempt_scope_factory: Callable[[str, str, str, Sequence[Mapping[str, Any]]], Any]
    build_execution: Callable[..., CandidateJobExecution]
    generate_outline: Callable[..., Awaitable[Any]]
    generate_prose_candidate: Callable[..., Awaitable[GeneratedProseCandidate]]
    review_prose_candidate: Callable[..., Awaitable[ChapterGenerationResult]]
    generate_state_candidate: Callable[..., Awaitable[ChapterGenerationResult]]
    recover_state_candidate: Callable[..., Awaitable[Any]]
    finalize: Callable[..., Awaitable[Mapping[str, Any]]]


def _strict_usage(value: Any, *, conservative_tokens: Any = None) -> CandidateUsageSummary:
    raw = value if isinstance(value, Mapping) else {}
    components: dict[str, int] = {}
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        item = raw.get(field, 0)
        if type(item) is not int or item < 0 or item > _MAX_TOKEN_COUNT:
            raise ChapterCandidatePipelineBlocked(
                "候选作业持久调用的 Token 用量无效"
            )
        components[field] = item
    bound = conservative_tokens
    if bound is not None and (
        type(bound) is not int or bound < 0 or bound > _MAX_TOKEN_COUNT
    ):
        raise ChapterCandidatePipelineBlocked("候选作业持久调用上界无效")
    total = max(
        components["total_tokens"],
        components["input_tokens"] + components["output_tokens"],
        int(bound or 0),
    )
    if total > _MAX_TOKEN_COUNT:
        raise ChapterCandidatePipelineBlocked("候选作业持久调用超过 V1 Token 上限")
    return CandidateUsageSummary(
        input_tokens=components["input_tokens"],
        output_tokens=components["output_tokens"],
        total_tokens=total,
    )


def _attempt_summary(slot: Mapping[str, Any]) -> CandidateAttemptSummary:
    state_value = str(slot.get("state") or "")
    state = {
        "accounted": CandidateAttemptState.ACCOUNTED,
        "uncertain": CandidateAttemptState.UNCERTAIN,
        "claimed": CandidateAttemptState.UNCERTAIN,
        "released_pre_dispatch": CandidateAttemptState.RELEASED_PRE_DISPATCH,
        "uncertain_retry_acknowledged": CandidateAttemptState.RESOLVED_RETRY,
        "uncertain_skip_acknowledged": CandidateAttemptState.RESOLVED_SKIP,
    }.get(state_value)
    if state is None:
        raise ChapterCandidatePipelineBlocked("候选作业持久调用状态无效")
    raw_phase = slot.get("phase")
    try:
        phase = CandidateAttemptPhase(str(raw_phase))
    except ValueError:
        phase = CandidateAttemptPhase.UNKNOWN
    usage = _strict_usage(
        slot.get("usage"),
        conservative_tokens=(
            slot.get("conservative_tokens")
            if state is not CandidateAttemptState.ACCOUNTED
            else None
        ),
    )
    return CandidateAttemptSummary(
        attempt_id=str(slot.get("attempt_id") or ""),
        provider_alias=str(slot.get("provider_alias") or "unreported"),
        phase=phase,
        state=state,
        usage=usage,
    )


def _completed_steps(
    checkpoints: Sequence[CandidatePipelineCheckpointV1],
) -> tuple[str, ...]:
    steps: list[str] = []
    review_count = 0
    for checkpoint in checkpoints:
        if isinstance(checkpoint, ProseCandidateCheckpointV1):
            steps.append(
                "prose"
                if checkpoint.origin == "initial"
                else f"prose_repair_{checkpoint.cycle}"
            )
        elif isinstance(checkpoint, AdherenceCandidateCheckpointV1):
            review_count += 1
            steps.append(
                "outline_adherence"
                if review_count == 1
                else f"outline_adherence_recheck_{review_count}"
            )
        else:
            steps.append(
                "state"
                if checkpoint.origin == "initial"
                else f"state_repair_{checkpoint.cycle}"
            )
    return tuple(steps)


def _truncations(
    checkpoints: Sequence[CandidatePipelineCheckpointV1],
) -> tuple[CandidateTruncationSummary, ...]:
    steps = _completed_steps(checkpoints)
    return tuple(
        CandidateTruncationSummary(
            step=step,
            truncated_section_count=checkpoint.truncation.truncated_section_count,
            dropped_item_count=checkpoint.truncation.dropped_item_count,
        )
        for step, checkpoint in zip(steps, checkpoints, strict=True)
        if (
            checkpoint.truncation.truncated_section_count
            or checkpoint.truncation.dropped_item_count
        )
    )


def _adherence_result(
    checkpoint: AdherenceCandidateCheckpointV1,
) -> ChapterGenerationResult:
    source = checkpoint.source
    return ChapterGenerationResult(
        stage=ChapterGenerationStage.OUTLINE_ADHERENCE,
        value={
            "verdict": checkpoint.verdict,
            "issues": [
                {"category": category}
                for category in checkpoint.issue_categories
            ],
            "scene_coverage": [
                {
                    "scene_index": item.scene_index,
                    "status": item.status,
                }
                for item in checkpoint.scene_coverage
            ],
            "source_prose_run_id": source.source_run_id,
            "source_prose_run_revision": source.source_run_revision,
            "source_content_digest": source.source_content_digest,
        },
        usage={},
        attempts=[],
        truncation={},
        accepted=False,
    )


def _state_result(value: Any) -> ChapterGenerationResult:
    if isinstance(value, ChapterGenerationResult):
        return value
    candidate = getattr(value, "value", None)
    if not isinstance(candidate, Mapping):
        raise ChapterCandidatePipelineBlocked(
            "候选作业无法重建已持久化状态候选"
        )
    return ChapterGenerationResult(
        stage=ChapterGenerationStage.STATE,
        value=dict(candidate),
        usage={},
        attempts=[],
        truncation={
            "truncated_section_count": int(
                getattr(value, "truncated_section_count", 0)
            ),
            "dropped_item_count": int(getattr(value, "dropped_item_count", 0)),
        },
        dropped=(
            {
                "dropped_reference_count": int(
                    getattr(value, "dropped_reference_count", 0)
                )
            }
            if int(getattr(value, "dropped_reference_count", 0))
            else {}
        ),
        accepted=False,
    )


class ChapterCandidateJobRunner:
    """Run or resume one candidate chapter using only frozen Job authority."""

    def __init__(
        self,
        *,
        execution_id: str,
        readiness: Mapping[str, Any],
        generation_params: Mapping[str, Any] | None,
        recalculate_after_outline: Callable[[str, dict[str, Any]], Awaitable[Any]],
        deps: ChapterCandidateJobRunnerDeps,
    ) -> None:
        self._execution_id = str(execution_id)
        self._readiness = dict(readiness)
        self._generation_params = dict(generation_params or {})
        self._recalculate_after_outline = recalculate_after_outline
        self._deps = deps

    async def _load_checkpoints(
        self,
        chapter_id: str,
    ) -> tuple[CandidatePipelineCheckpointV1, ...]:
        raw = await self._deps.list_checkpoints(
            self._execution_id,
            chapter_id=chapter_id,
        )
        return tuple(parse_candidate_pipeline_checkpoint(item) for item in raw)

    async def _load_attempts(self, chapter_id: str) -> list[Mapping[str, Any]]:
        raw = await self._deps.list_attempts(
            self._execution_id,
            chapter_id=chapter_id,
            step_prefix="",
        )
        return [dict(item) for item in raw]

    def _scope(
        self,
        chapter_id: str,
        step: str,
        slots: Sequence[Mapping[str, Any]],
    ) -> Any:
        return self._deps.attempt_scope_factory(
            self._execution_id,
            chapter_id,
            step,
            tuple(slots),
        )

    async def _resume(
        self,
        *,
        owner_id: str,
        novel_id: str,
        chapter: Mapping[str, Any],
        execution: CandidateJobExecution,
        checkpoints: tuple[CandidatePipelineCheckpointV1, ...],
        slots: Sequence[Mapping[str, Any]],
    ) -> ChapterCandidatePipelineResume:
        chapter_id = str(chapter.get("_id") or "")
        scenes = (chapter.get("outline") or {}).get("scenes")
        if not isinstance(scenes, list) or not scenes:
            raise ChapterCandidatePipelineBlocked(
                "候选作业恢复章节缺少有效章纲"
            )
        replay = replay_candidate_pipeline_checkpoints(
            checkpoints,
            chapter_id=chapter_id,
            expected_scene_count=len(scenes),
            max_repair_cycles=execution.max_repair_cycles,
        )
        source_identity = replay.current_prose.source
        source = await execution.recover_source(
            owner_id=owner_id,
            novel_id=novel_id,
            chapter_id=chapter_id,
            run_id=source_identity.source_run_id,
            revision=source_identity.source_run_revision,
            digest=source_identity.source_content_digest,
        )

        slot_by_id: dict[str, Mapping[str, Any]] = {}
        for slot in slots:
            attempt_id = str(slot.get("attempt_id") or "")
            if not attempt_id or attempt_id in slot_by_id:
                raise ChapterCandidatePipelineBlocked(
                    "候选作业持久调用身份重复或缺失"
                )
            slot_by_id[attempt_id] = slot
        summaries: list[CandidateAttemptSummary] = []
        for attempt_id in replay.attempt_ids:
            slot = slot_by_id.get(attempt_id)
            if slot is None:
                raise ChapterCandidatePipelineBlocked(
                    "候选检查点缺少持久调用证据"
                )
            summaries.append(_attempt_summary(slot))
        checkpoint_ids = set(replay.attempt_ids)
        for slot in slots:
            attempt_id = str(slot.get("attempt_id") or "")
            if attempt_id in checkpoint_ids:
                continue
            state = str(slot.get("state") or "")
            if state in {"released_pre_dispatch", "uncertain_retry_acknowledged"}:
                continue
            if str(slot.get("step_id") or "").startswith("candidate-"):
                summaries.append(_attempt_summary(slot))

        tokens = 0
        for summary in summaries:
            tokens += summary.usage.total_tokens
            if tokens > _MAX_TOKEN_COUNT:
                raise ChapterCandidatePipelineBlocked(
                    "候选作业恢复 Token 超过 V1 上限"
                )

        adherence = (
            _adherence_result(replay.latest_adherence)
            if replay.latest_adherence is not None
            else None
        )
        state = None
        state_proposal_id = None
        if replay.latest_state is not None:
            state_checkpoint = replay.latest_state
            recovered = await self._deps.recover_state_candidate(
                owner_id=owner_id,
                novel_id=novel_id,
                chapter_id=chapter_id,
                proposal_id=state_checkpoint.proposal_id,
                request_id=state_checkpoint.request_id,
                source_run_id=source.source_run_id,
                source_run_revision=source.source_run_revision,
                source_content_digest=source.source_content_digest,
            )
            state = _state_result(recovered)
            state_proposal_id = state_checkpoint.proposal_id

        progress = ChapterCandidatePipelineProgress(
            tokens=tokens,
            attempts=tuple(summaries),
            truncations=_truncations(checkpoints),
            completed_steps=_completed_steps(checkpoints),
            repair_cycles_used=replay.repair_cycles_used,
            prose_run_id=source.source_run_id,
            prose_run_revision=source.source_run_revision,
            prose_content_digest=source.source_content_digest,
            state_proposal_id=state_proposal_id,
        )
        return ChapterCandidatePipelineResume(
            progress=progress,
            checkpoints=checkpoints,
            source=source,
            adherence=adherence,
            state=state,
        )

    async def run(
        self,
        novel_id: str,
        chapter: Mapping[str, Any],
    ) -> CandidateChapterOutcome:
        chapter_id = str(chapter.get("_id") or "")
        if not chapter_id:
            raise ChapterCandidatePipelineBlocked("候选作业章节身份无效")
        novel = await self._deps.get_novel(novel_id)
        owner_id = str(novel.get("owner_id") or "")
        if not owner_id:
            raise ChapterCandidatePipelineBlocked("候选作业缺少 owner-scoped 身份")

        current = dict(await self._deps.get_chapter(chapter_id))
        slots = await self._load_attempts(chapter_id)
        outline = current.get("outline")
        if not isinstance(outline, Mapping) or not isinstance(
            outline.get("scenes"), list
        ) or not outline.get("scenes"):
            await self._deps.generate_outline(
                novel_id,
                current,
                self._scope(chapter_id, "outline", slots),
                self._generation_params,
            )
            current = dict(await self._deps.get_chapter(chapter_id))
            outline = current.get("outline")
            if not isinstance(outline, Mapping):
                raise ChapterCandidatePipelineBlocked(
                    "候选作业章纲生成后仍不可恢复"
                )
            recalculation = await self._recalculate_after_outline(
                chapter_id,
                dict(outline),
            )
            if (
                isinstance(recalculation, Mapping)
                and recalculation.get("requires_confirmation") is True
            ):
                raise ChapterCandidatePipelineBlocked(
                    "章纲生成扩大了冻结授权范围"
                )

        execution = self._deps.build_execution(
            chapter_id=chapter_id,
            attempt_scope_factory=lambda step: self._scope(
                chapter_id,
                step,
                slots,
            ),
        )
        if not isinstance(execution, CandidateJobExecution):
            raise ChapterCandidatePipelineBlocked("候选作业冻结执行计划无效")
        checkpoints = await self._load_checkpoints(chapter_id)
        resume = (
            await self._resume(
                owner_id=owner_id,
                novel_id=novel_id,
                chapter=current,
                execution=execution,
                checkpoints=checkpoints,
                slots=slots,
            )
            if checkpoints
            else None
        )
        if not checkpoints and any(
            str(slot.get("step_id") or "").startswith("candidate-")
            and str(slot.get("state") or "")
            not in {"released_pre_dispatch", "uncertain_retry_acknowledged"}
            for slot in slots
        ):
            raise ChapterCandidatePipelineBlocked(
                "候选作业已有付费调用但缺少结果检查点",
                code="candidate_result_projection_missing",
            )

        async def persist(checkpoint: CandidatePipelineCheckpointV1) -> None:
            await self._deps.append_checkpoint(self._execution_id, checkpoint)

        async def finalize(
            target_novel: str,
            target_chapter: dict[str, Any],
            source: ProseCandidateSource,
            adherence: Mapping[str, Any],
            state: Mapping[str, Any],
            cycles: int,
        ) -> Mapping[str, Any]:
            try:
                return await self._deps.finalize(
                    owner_id=owner_id,
                    novel_id=target_novel,
                    chapter=target_chapter,
                    source=source,
                    adherence=adherence,
                    state=state,
                    repair_cycles_used=cycles,
                )
            except Exception as exc:
                raise _FinalizationDependencyError(
                    "candidate finalization dependency failed"
                ) from exc

        pipeline = ChapterCandidatePipeline(ChapterCandidatePipelineDeps(
            generate_prose_candidate=lambda target_novel, target_chapter: (
                self._deps.generate_prose_candidate(
                    target_novel,
                    target_chapter,
                    attempt_scope=self._scope(
                        chapter_id,
                        "candidate-prose",
                        slots,
                    ),
                    generation_params=self._generation_params,
                )
            ),
            review_prose_candidate=lambda target_novel, target_chapter, source: (
                self._deps.review_prose_candidate(
                    target_novel,
                    target_chapter,
                    source,
                    attempt_scope=self._scope(
                        chapter_id,
                        "candidate-outline-adherence",
                        slots,
                    ),
                    generation_params=self._generation_params,
                    generation_plan=execution.adherence_plan,
                )
            ),
            generate_state_candidate=(
                lambda target_novel, target_chapter, source, **kwargs:
                self._deps.generate_state_candidate(
                    target_novel,
                    target_chapter,
                    source,
                    attempt_scope=self._scope(
                        chapter_id,
                        "candidate-state",
                        slots,
                    ),
                    generation_params=self._generation_params,
                    generation_plan=execution.state_plan,
                    **kwargs,
                )
            ),
            finalize=finalize,
            persist_checkpoint=persist,
            repair_prose_candidate=execution.repair_prose_candidate,
            repair_state_candidate=execution.repair_state_candidate,
        ))
        result = await pipeline.run(
            owner_id=owner_id,
            novel_id=novel_id,
            chapter=current,
            max_repair_cycles=execution.max_repair_cycles,
            resume=resume,
        )
        final_checkpoints = await self._load_checkpoints(chapter_id)
        scenes = (current.get("outline") or {}).get("scenes")
        if not isinstance(scenes, list) or not scenes:
            raise ChapterCandidatePipelineBlocked("候选作业终态章纲无效")
        terminal = replay_candidate_pipeline_checkpoints(
            final_checkpoints,
            chapter_id=chapter_id,
            expected_scene_count=len(scenes),
            max_repair_cycles=execution.max_repair_cycles,
            require_terminal=True,
        )
        state_checkpoint = terminal.latest_state
        adherence_checkpoint = terminal.latest_adherence
        if state_checkpoint is None or adherence_checkpoint is None:
            raise ChapterCandidatePipelineBlocked("候选作业终态检查点不完整")
        progress = CandidatePipelineProgressV1(
            schema_version="candidate_pipeline_progress.v1",
            status="completed",
            finalization_status="committed",
            chapter_id=chapter_id,
            order_index=int(current.get("order_index") or 0),
            tokens=result.tokens,
            source=terminal.current_prose.source,
            state_proposal_id=state_checkpoint.proposal_id,
            repair_cycles_used=terminal.repair_cycles_used,
            attempt_count=len(terminal.attempt_ids),
            truncation_count=terminal.truncation_count,
            outline_issue_categories=adherence_checkpoint.issue_categories,
            scene_coverage_count=len(adherence_checkpoint.scene_coverage),
            consistency_issue_count=state_checkpoint.consistency_issue_count,
        )
        return CandidateChapterOutcome(
            progress=progress,
            checkpoints=final_checkpoints,
        )
