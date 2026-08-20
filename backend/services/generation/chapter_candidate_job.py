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

from bson import ObjectId

from backend.services.generation.candidate_repair_contracts import (
    AdherenceCandidateCheckpointV1,
    CandidatePipelineCheckpointV1,
    CandidatePipelineProgressV1,
    ProseCandidateCheckpointV1,
    StateCandidateCheckpointV1,
    parse_candidate_pipeline_checkpoint,
    replay_candidate_pipeline_checkpoints,
)
from backend.services.generation.chapter_candidate_authorization import (
    parse_candidate_repair_authorization,
    readiness_chapter_uses_candidate_pipeline,
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
from backend.services.generation.chapter_finalization import (
    chapter_finalization_idempotency_key,
)
from backend.services.generation.job_engine import CandidateChapterOutcome


_MAX_TOKEN_COUNT = 1_000_000_000


class _FinalizationDependencyError(RuntimeError):
    """Expose a known zero-Provider boundary to the pipeline evidence collector."""

    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    attempts: list[dict[str, Any]] = []


class _NarrativeFencedAttemptScope:
    """Recheck the frozen narrative revision before every paid attempt claim."""

    def __init__(
        self,
        delegate: Any,
        guard: Callable[[], Awaitable[None]],
    ) -> None:
        self._delegate = delegate
        self._guard = guard

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def claim(self, provider_alias: str, phase: str) -> str:
        await self._guard()
        return await self._delegate.claim(provider_alias, phase)

    async def claim_with_budget(
        self,
        provider_alias: str,
        phase: str,
        conservative_tokens: int | None,
    ) -> str:
        await self._guard()
        claim = getattr(self._delegate, "claim_with_budget", None)
        if callable(claim):
            return await claim(provider_alias, phase, conservative_tokens)
        return await self._delegate.claim(provider_alias, phase)


@dataclass(frozen=True)
class CandidateJobExecution:
    """Frozen live adapters that have already matched the Job readiness."""

    max_repair_cycles: int
    outline_plan: Any | None
    prose_plan: Any
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
    get_volume: Callable[[str], Awaitable[Mapping[str, Any]]]
    get_chapter: Callable[[str], Awaitable[Mapping[str, Any]]]
    list_checkpoints: Callable[..., Awaitable[Sequence[Any]]]
    append_checkpoint: Callable[..., Awaitable[Any]]
    list_attempts: Callable[..., Awaitable[Sequence[Mapping[str, Any]]]]
    reserve_attempts: Callable[[str, str, int], Awaitable[Any]]
    attempt_scope_factory: Callable[[str, str, str, Sequence[Mapping[str, Any]]], Any]
    build_execution: Callable[..., CandidateJobExecution]
    current_narrative_revision: Callable[[str], Awaitable[int]]
    recover_mutation_revision: Callable[..., Awaitable[int | None]]
    advance_narrative_revision_cursor: Callable[..., Awaitable[bool]]
    generate_outline: Callable[..., Awaitable[Any]]
    generate_prose_candidate: Callable[..., Awaitable[GeneratedProseCandidate]]
    review_prose_candidate: Callable[..., Awaitable[ChapterGenerationResult]]
    generate_state_candidate: Callable[..., Awaitable[ChapterGenerationResult]]
    recover_state_candidate: Callable[..., Awaitable[Any]]
    finalize: Callable[..., Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True)
class CandidateJobScope:
    execution_id: str
    novel_id: str
    owner_id: str
    scope: str
    volume_id: str | None
    chapter_id: str
    chapter_volume_id: str
    order_index: int
    has_outline: bool
    scene_count: int
    expected_narrative_revision: int

    @classmethod
    def from_readiness(
        cls,
        readiness: Mapping[str, Any],
        *,
        execution_id: str,
        novel_id: str,
        chapter_id: str,
        expected_narrative_revision: int,
    ) -> "CandidateJobScope":
        if not all(
            ObjectId.is_valid(value)
            for value in (execution_id, novel_id, chapter_id)
        ):
            raise ChapterCandidatePipelineBlocked(
                "候选作业内部执行身份无效"
            )
        if not readiness_chapter_uses_candidate_pipeline(
            readiness,
            chapter_id=chapter_id,
        ):
            raise ChapterCandidatePipelineBlocked(
                "章节不在候选作业冻结范围内"
            )
        readiness_novel = readiness.get("novel_id")
        scope = readiness.get("scope")
        volume_id = readiness.get("volume_id")
        if readiness_novel != novel_id or scope not in {"volume", "book"}:
            raise ChapterCandidatePipelineBlocked("候选作业小说或范围无效")
        if scope == "volume":
            if not isinstance(volume_id, str) or not ObjectId.is_valid(volume_id):
                raise ChapterCandidatePipelineBlocked("候选作业卷身份无效")
        elif volume_id is not None:
            raise ChapterCandidatePipelineBlocked("整本候选作业不得绑定卷身份")
        resources = readiness.get("resources")
        owner_id = (
            resources.get("owner_id")
            if isinstance(resources, Mapping)
            else None
        )
        frozen_revision = (
            resources.get("narrative_revision")
            if isinstance(resources, Mapping)
            else None
        )
        if (
            not isinstance(owner_id, str)
            or not ObjectId.is_valid(owner_id)
            or type(frozen_revision) is not int
            or frozen_revision < 0
            or type(expected_narrative_revision) is not int
            or expected_narrative_revision < frozen_revision
        ):
            raise ChapterCandidatePipelineBlocked(
                "候选作业 narrative revision 无效"
            )
        work = readiness.get("work")
        raw_chapters = work.get("chapters") if isinstance(work, Mapping) else None
        if not isinstance(raw_chapters, list):
            raise ChapterCandidatePipelineBlocked("候选作业工作清单无效")
        matches = [
            item
            for item in raw_chapters
            if isinstance(item, Mapping)
            and item.get("chapter_id") == chapter_id
        ]
        if len(matches) != 1:
            raise ChapterCandidatePipelineBlocked("候选作业章节身份无效")
        snapshot = matches[0]
        chapter_volume_id = snapshot.get("volume_id")
        order_index = snapshot.get("order_index")
        has_outline = snapshot.get("has_outline")
        scene_count = snapshot.get("scene_count")
        if (
            type(order_index) is not int
            or not isinstance(chapter_volume_id, str)
            or not ObjectId.is_valid(chapter_volume_id)
            or type(has_outline) is not bool
            or type(scene_count) is not int
            or scene_count < 0
            or (has_outline and scene_count < 1)
            or snapshot.get("has_content") is not False
        ):
            raise ChapterCandidatePipelineBlocked("候选作业章节快照无效")
        return cls(
            execution_id=str(execution_id),
            novel_id=novel_id,
            owner_id=owner_id,
            scope=str(scope),
            volume_id=str(volume_id) if volume_id is not None else None,
            chapter_id=chapter_id,
            chapter_volume_id=chapter_volume_id,
            order_index=order_index,
            has_outline=has_outline,
            scene_count=scene_count,
            expected_narrative_revision=expected_narrative_revision,
        )

    def validate_documents(
        self,
        novel: Mapping[str, Any],
        volume: Mapping[str, Any],
        chapter: Mapping[str, Any],
    ) -> str:
        if str(novel.get("_id") or "") != self.novel_id:
            raise ChapterCandidatePipelineBlocked("候选作业读取了错误小说")
        owner_id = str(novel.get("owner_id") or "")
        if owner_id != self.owner_id:
            raise ChapterCandidatePipelineBlocked("候选作业缺少 owner-scoped 身份")
        if (
            str(chapter.get("_id") or "") != self.chapter_id
            or str(chapter.get("novel_id") or "") != self.novel_id
            or type(chapter.get("order_index")) is not int
            or int(chapter["order_index"]) != self.order_index
        ):
            raise ChapterCandidatePipelineBlocked("候选作业章节父子范围无效")
        chapter_volume = str(chapter.get("volume_id") or "")
        if chapter_volume != self.chapter_volume_id:
            raise ChapterCandidatePipelineBlocked("候选作业章节卷身份已变化")
        if self.scope == "volume" and chapter_volume != self.volume_id:
            raise ChapterCandidatePipelineBlocked("候选作业章节不属于冻结卷")
        if not chapter_volume:
            raise ChapterCandidatePipelineBlocked("候选作业章节缺少卷身份")
        if (
            str(volume.get("_id") or "") != chapter_volume
            or str(volume.get("novel_id") or "") != self.novel_id
        ):
            raise ChapterCandidatePipelineBlocked("候选作业卷父子范围无效")
        return owner_id


def candidate_outline_idempotency_key(execution_id: str, chapter_id: str) -> str:
    if not str(execution_id or "") or not str(chapter_id or ""):
        raise ValueError("candidate outline identity is incomplete")
    return f"candidate-job-outline:{execution_id}:{chapter_id}"


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
        expected_narrative_revision: int,
        authorized_attempt_slots: int,
        generation_params: Mapping[str, Any] | None,
        recalculate_after_outline: Callable[[str, dict[str, Any]], Awaitable[Any]],
        deps: ChapterCandidateJobRunnerDeps,
    ) -> None:
        self._execution_id = str(execution_id)
        self._readiness = dict(readiness)
        if (
            type(expected_narrative_revision) is not int
            or expected_narrative_revision < 0
        ):
            raise ValueError("candidate Job narrative revision cursor is invalid")
        self._expected_narrative_revision = expected_narrative_revision
        if (
            isinstance(authorized_attempt_slots, bool)
            or not isinstance(authorized_attempt_slots, int)
            or authorized_attempt_slots < 0
        ):
            raise ValueError("candidate Job attempt authority is invalid")
        self._authorized_attempt_slots = authorized_attempt_slots
        self._generation_params = dict(generation_params or {})
        self._recalculate_after_outline = recalculate_after_outline
        self._deps = deps

    async def _ensure_narrative_revision(
        self,
        scope: CandidateJobScope,
        expected: int,
    ) -> None:
        current = await self._deps.current_narrative_revision(scope.novel_id)
        if type(current) is not int or current != expected:
            raise ChapterCandidatePipelineBlocked(
                "候选作业 narrative revision 已变化",
                code="candidate_narrative_revision_changed",
            )

    async def _recover_mutation_revision(
        self,
        scope: CandidateJobScope,
        key: str,
        operation: str,
    ) -> int | None:
        return await self._deps.recover_mutation_revision(
            scope.novel_id,
            key,
            operation=operation,
        )

    async def _advance_revision_cursor(
        self,
        scope: CandidateJobScope,
        *,
        expected_revision: int,
        next_revision: int,
    ) -> None:
        if next_revision != expected_revision + 1:
            raise ChapterCandidatePipelineBlocked(
                "候选作业 mutation revision receipt 无效"
            )
        advanced = await self._deps.advance_narrative_revision_cursor(
            self._execution_id,
            chapter_id=scope.chapter_id,
            expected_revision=expected_revision,
            next_revision=next_revision,
        )
        if advanced is not True:
            raise ChapterCandidatePipelineBlocked(
                "候选作业 narrative revision cursor 更新失败"
            )

    @staticmethod
    def _repair_cycle_limit(readiness: Mapping[str, Any]) -> int:
        planning = readiness.get("planning")
        raw = (
            planning.get("chapter_candidate_repair_authorization")
            if isinstance(planning, Mapping)
            else None
        )
        if not isinstance(raw, Mapping):
            raise ChapterCandidatePipelineBlocked(
                "候选作业修复授权无效"
            )
        try:
            return parse_candidate_repair_authorization(
                raw
            ).max_repair_cycles_per_chapter
        except ValueError as exc:
            raise ChapterCandidatePipelineBlocked(
                "候选作业修复授权无效"
            ) from exc

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

    @staticmethod
    def _summaries_for_replay(
        replay: Any,
        slots: Sequence[Mapping[str, Any]],
    ) -> tuple[tuple[CandidateAttemptSummary, ...], int]:
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
        return tuple(summaries), tokens

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

        summaries, tokens = self._summaries_for_replay(replay, slots)

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
            attempts=summaries,
            truncations=tuple(
                CandidateTruncationSummary(
                    step=step,
                    truncated_section_count=truncated,
                    dropped_item_count=dropped,
                )
                for step, truncated, dropped in replay.truncations
            ),
            completed_steps=replay.completed_steps,
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

    @staticmethod
    def _terminal_outcome(
        *,
        checkpoints: tuple[CandidatePipelineCheckpointV1, ...],
        terminal: Any,
        order_index: int,
        tokens: int,
        expected_narrative_revision: int,
        next_narrative_revision: int,
    ) -> CandidateChapterOutcome:
        state_checkpoint = terminal.latest_state
        adherence_checkpoint = terminal.latest_adherence
        if state_checkpoint is None or adherence_checkpoint is None:
            raise ChapterCandidatePipelineBlocked(
                "候选作业终态检查点不完整"
            )
        return CandidateChapterOutcome(
            progress=CandidatePipelineProgressV1(
                schema_version="candidate_pipeline_progress.v1",
                status="completed",
                finalization_status="committed",
                chapter_id=terminal.current_prose.chapter_id,
                order_index=order_index,
                tokens=tokens,
                source=terminal.current_prose.source,
                state_proposal_id=state_checkpoint.proposal_id,
                repair_cycles_used=terminal.repair_cycles_used,
                attempt_count=len(terminal.attempt_ids),
                truncation_count=terminal.truncation_count,
                outline_issue_categories=(
                    adherence_checkpoint.issue_categories
                ),
                scene_coverage_count=len(
                    adherence_checkpoint.scene_coverage
                ),
                consistency_issue_count=(
                    state_checkpoint.consistency_issue_count
                ),
            ),
            checkpoints=checkpoints,
            expected_narrative_revision=expected_narrative_revision,
            next_narrative_revision=next_narrative_revision,
        )

    async def run(
        self,
        novel_id: str,
        chapter: Mapping[str, Any],
    ) -> CandidateChapterOutcome:
        chapter_id = str(chapter.get("_id") or "")
        if not chapter_id:
            raise ChapterCandidatePipelineBlocked("候选作业章节身份无效")
        scope = CandidateJobScope.from_readiness(
            self._readiness,
            execution_id=self._execution_id,
            novel_id=str(novel_id),
            chapter_id=chapter_id,
            expected_narrative_revision=self._expected_narrative_revision,
        )
        novel = await self._deps.get_novel(novel_id)
        current = dict(await self._deps.get_chapter(chapter_id))
        chapter_volume_id = str(current.get("volume_id") or "")
        if not chapter_volume_id:
            raise ChapterCandidatePipelineBlocked("候选作业章节缺少卷身份")
        volume = await self._deps.get_volume(chapter_volume_id)
        owner_id = scope.validate_documents(novel, volume, current)
        slots = await self._load_attempts(chapter_id)
        if len(slots) > self._authorized_attempt_slots:
            raise ChapterCandidatePipelineBlocked("候选作业调用容量已被扩大")
        checkpoints = await self._load_checkpoints(chapter_id)
        max_repair_cycles = self._repair_cycle_limit(self._readiness)
        expected_revision = scope.expected_narrative_revision
        outline = current.get("outline")
        scenes = outline.get("scenes") if isinstance(outline, Mapping) else None

        if checkpoints and isinstance(scenes, list) and scenes:
            prefix = replay_candidate_pipeline_checkpoints(
                checkpoints,
                chapter_id=chapter_id,
                expected_scene_count=len(scenes),
                max_repair_cycles=max_repair_cycles,
            )
            state_checkpoint = prefix.latest_state
            if (
                prefix.phase == "state"
                and state_checkpoint is not None
                and state_checkpoint.consistency_issue_count == 0
                and state_checkpoint.dropped_reference_count == 0
            ):
                finalization_key = chapter_finalization_idempotency_key(
                    prose_run_id=prefix.current_prose.source.source_run_id,
                    prose_run_revision=(
                        prefix.current_prose.source.source_run_revision
                    ),
                    state_proposal_id=state_checkpoint.proposal_id,
                )
                finalization_revision = await self._recover_mutation_revision(
                    scope,
                    finalization_key,
                    "finalize_chapter_generation",
                )
                if finalization_revision is not None:
                    if finalization_revision != expected_revision + 1:
                        raise ChapterCandidatePipelineBlocked(
                            "候选作业终态 mutation revision receipt 无效"
                        )
                    terminal = replay_candidate_pipeline_checkpoints(
                        checkpoints,
                        chapter_id=chapter_id,
                        expected_scene_count=len(scenes),
                        max_repair_cycles=max_repair_cycles,
                        require_terminal=True,
                    )
                    _summaries, tokens = self._summaries_for_replay(
                        terminal,
                        slots,
                    )
                    return self._terminal_outcome(
                        checkpoints=checkpoints,
                        terminal=terminal,
                        order_index=scope.order_index,
                        tokens=tokens,
                        expected_narrative_revision=expected_revision,
                        next_narrative_revision=finalization_revision,
                    )

        def fenced_scope(step: str) -> _NarrativeFencedAttemptScope:
            return _NarrativeFencedAttemptScope(
                self._scope(chapter_id, step, slots),
                lambda: self._ensure_narrative_revision(
                    scope,
                    expected_revision,
                ),
            )

        execution = self._deps.build_execution(
            chapter_id=chapter_id,
            attempt_scope_factory=fenced_scope,
        )
        if (
            not isinstance(execution, CandidateJobExecution)
            or execution.max_repair_cycles != max_repair_cycles
        ):
            raise ChapterCandidatePipelineBlocked("候选作业冻结执行计划无效")

        reserved = False

        async def ensure_reserved() -> None:
            nonlocal reserved
            if reserved:
                return
            await self._deps.reserve_attempts(
                self._execution_id,
                chapter_id,
                max(0, self._authorized_attempt_slots - len(slots)),
            )
            reserved = True

        if scope.has_outline:
            await self._ensure_narrative_revision(scope, expected_revision)
            if not isinstance(scenes, list) or len(scenes) != scope.scene_count:
                raise ChapterCandidatePipelineBlocked(
                    "候选作业章纲已偏离冻结快照",
                    code="candidate_narrative_revision_changed",
                )
        else:
            outline_key = candidate_outline_idempotency_key(
                self._execution_id,
                chapter_id,
            )
            outline_revision = await self._recover_mutation_revision(
                scope,
                outline_key,
                "accept_chapter_outline",
            )
            if outline_revision is None:
                await self._ensure_narrative_revision(scope, expected_revision)
                if isinstance(scenes, list) and scenes:
                    raise ChapterCandidatePipelineBlocked(
                        "冻结快照缺少章纲，但当前章纲并非本作业提交",
                        code="candidate_narrative_revision_changed",
                    )
                if any(
                    str(slot.get("step_id") or "") == "outline"
                    and str(slot.get("state") or "")
                    not in {
                        "released_pre_dispatch",
                        "uncertain_retry_acknowledged",
                    }
                    for slot in slots
                ):
                    raise ChapterCandidatePipelineBlocked(
                        "章纲 Provider 已调用但缺少正式 mutation receipt",
                        code="candidate_result_projection_missing",
                    )
                if execution.outline_plan is None:
                    raise ChapterCandidatePipelineBlocked(
                        "候选作业没有冻结章纲生成计划"
                    )
                await ensure_reserved()
                await self._deps.generate_outline(
                    novel_id,
                    current,
                    fenced_scope("outline"),
                    self._generation_params,
                    generation_plan=execution.outline_plan,
                    expected_narrative_revision=expected_revision,
                    mutation_idempotency_key=outline_key,
                )
                outline_revision = await self._recover_mutation_revision(
                    scope,
                    outline_key,
                    "accept_chapter_outline",
                )
                if outline_revision is None:
                    raise ChapterCandidatePipelineBlocked(
                        "候选作业章纲 mutation receipt 缺失"
                    )
            if outline_revision == expected_revision + 1:
                await self._advance_revision_cursor(
                    scope,
                    expected_revision=expected_revision,
                    next_revision=outline_revision,
                )
                expected_revision = outline_revision
            elif outline_revision != expected_revision:
                raise ChapterCandidatePipelineBlocked(
                    "候选作业章纲 mutation revision receipt 无效"
                )
            await self._ensure_narrative_revision(scope, expected_revision)
            current = dict(await self._deps.get_chapter(chapter_id))
            current_volume_id = str(current.get("volume_id") or "")
            if current_volume_id != chapter_volume_id:
                raise ChapterCandidatePipelineBlocked(
                    "候选作业章节卷身份已变化"
                )
            volume = await self._deps.get_volume(current_volume_id)
            scope.validate_documents(novel, volume, current)
            outline = current.get("outline")
            scenes = (
                outline.get("scenes") if isinstance(outline, Mapping) else None
            )
            if not isinstance(scenes, list) or not scenes:
                raise ChapterCandidatePipelineBlocked(
                    "候选作业章纲生成后仍不可恢复"
                )
            recalculation = await self._recalculate_after_outline(
                chapter_id,
                dict(outline),
            )
            recalculated_revision = (
                recalculation.get("expected_narrative_revision")
                if isinstance(recalculation, Mapping)
                else None
            )
            if recalculated_revision != expected_revision:
                raise ChapterCandidatePipelineBlocked(
                    "章纲后授权重算没有绑定当前 narrative revision"
                )
            if recalculation.get("requires_confirmation") is True:
                raise ChapterCandidatePipelineBlocked(
                    "章纲生成扩大了冻结授权范围",
                    code="authorization_scope_increased",
                )
        await self._ensure_narrative_revision(scope, expected_revision)
        await ensure_reserved()
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

        async def generate_prose_step(
            target_novel: str,
            target_chapter: dict[str, Any],
        ) -> GeneratedProseCandidate:
            await self._ensure_narrative_revision(scope, expected_revision)
            return await self._deps.generate_prose_candidate(
                target_novel,
                target_chapter,
                attempt_scope=fenced_scope("candidate-prose"),
                generation_params=self._generation_params,
                generation_plan=execution.prose_plan,
            )

        async def review_prose_step(
            target_novel: str,
            target_chapter: dict[str, Any],
            source: ProseCandidateSource,
        ) -> ChapterGenerationResult:
            await self._ensure_narrative_revision(scope, expected_revision)
            return await self._deps.review_prose_candidate(
                target_novel,
                target_chapter,
                source,
                attempt_scope=fenced_scope(
                    "candidate-outline-adherence"
                ),
                generation_params=self._generation_params,
                generation_plan=execution.adherence_plan,
            )

        async def generate_state_step(
            target_novel: str,
            target_chapter: dict[str, Any],
            source: ProseCandidateSource,
            **kwargs: Any,
        ) -> ChapterGenerationResult:
            await self._ensure_narrative_revision(scope, expected_revision)
            return await self._deps.generate_state_candidate(
                target_novel,
                target_chapter,
                source,
                attempt_scope=fenced_scope("candidate-state"),
                generation_params=self._generation_params,
                generation_plan=execution.state_plan,
                **kwargs,
            )

        repair_prose = execution.repair_prose_candidate
        if repair_prose is not None:
            async def guarded_repair_prose(*args: Any, **kwargs: Any) -> Any:
                await self._ensure_narrative_revision(scope, expected_revision)
                return await repair_prose(*args, **kwargs)
        else:
            guarded_repair_prose = None

        repair_state = execution.repair_state_candidate
        if repair_state is not None:
            async def guarded_repair_state(*args: Any, **kwargs: Any) -> Any:
                await self._ensure_narrative_revision(scope, expected_revision)
                return await repair_state(*args, **kwargs)
        else:
            guarded_repair_state = None

        async def finalize(
            target_novel: str,
            target_chapter: dict[str, Any],
            source: ProseCandidateSource,
            adherence: Mapping[str, Any],
            state: Mapping[str, Any],
            cycles: int,
        ) -> Mapping[str, Any]:
            try:
                await self._ensure_narrative_revision(
                    scope,
                    expected_revision,
                )
                return await self._deps.finalize(
                    owner_id=owner_id,
                    novel_id=target_novel,
                    chapter=target_chapter,
                    source=source,
                    adherence=adherence,
                    state=state,
                    repair_cycles_used=cycles,
                )
            except ChapterCandidatePipelineBlocked:
                raise
            except Exception as exc:
                raise _FinalizationDependencyError(
                    "candidate finalization dependency failed"
                ) from exc

        pipeline = ChapterCandidatePipeline(ChapterCandidatePipelineDeps(
            generate_prose_candidate=generate_prose_step,
            review_prose_candidate=review_prose_step,
            generate_state_candidate=generate_state_step,
            finalize=finalize,
            persist_checkpoint=persist,
            repair_prose_candidate=guarded_repair_prose,
            repair_state_candidate=guarded_repair_state,
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
        if state_checkpoint is None:
            raise ChapterCandidatePipelineBlocked("候选作业终态状态检查点缺失")
        finalization_key = chapter_finalization_idempotency_key(
            prose_run_id=terminal.current_prose.source.source_run_id,
            prose_run_revision=terminal.current_prose.source.source_run_revision,
            state_proposal_id=state_checkpoint.proposal_id,
        )
        finalization_revision = await self._recover_mutation_revision(
            scope,
            finalization_key,
            "finalize_chapter_generation",
        )
        if finalization_revision != expected_revision + 1:
            raise ChapterCandidatePipelineBlocked(
                "候选作业终态 mutation revision receipt 缺失或无效"
            )
        return self._terminal_outcome(
            checkpoints=final_checkpoints,
            terminal=terminal,
            order_index=scope.order_index,
            tokens=result.tokens,
            expected_narrative_revision=expected_revision,
            next_narrative_revision=finalization_revision,
        )
