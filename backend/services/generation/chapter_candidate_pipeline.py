"""Deferred chapter tail: candidates first, one deterministic formal commit last."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Mapping

from backend.services.generation.chapter_generation_application import (
    ChapterGenerationResult,
    ChapterGenerationStage,
    ProseCandidateSource,
)
from backend.services.generation.chapter_finalization import (
    MAX_FINALIZATION_REPAIR_CYCLES,
)
from backend.services.generation.headless_generation import (
    GeneratedProseCandidate,
)


_OUTLINE_ISSUE_CATEGORIES = frozenset({
    "scene_coverage",
    "scene_order",
    "core_conflict",
    "ending_hook",
    "unplanned_major_event",
    "volume_arc",
})


class ChapterCandidatePipelineBlocked(ValueError):
    """A candidate gate failed before the formal chapter commit."""


@dataclass(frozen=True)
class ProseCandidateRepairRequest:
    cycle: int
    trigger: Literal["completion", "outline_adherence"]
    reason_codes: tuple[str, ...]
    issue_categories: tuple[str, ...]
    scene_indexes: tuple[int, ...]


@dataclass(frozen=True)
class StateCandidateRepairRequest:
    cycle: int
    consistency_issue_count: int
    affected_card_ids: tuple[str, ...]
    dropped_reference_count: int


@dataclass(frozen=True)
class ChapterCandidatePipelineDeps:
    generate_prose_candidate: Callable[
        [str, dict[str, Any]],
        Awaitable[GeneratedProseCandidate],
    ]
    review_prose_candidate: Callable[
        [str, dict[str, Any], ProseCandidateSource],
        Awaitable[ChapterGenerationResult],
    ]
    generate_state_candidate: Callable[
        [str, dict[str, Any], ProseCandidateSource],
        Awaitable[ChapterGenerationResult],
    ]
    finalize: Callable[
        [
            str,
            dict[str, Any],
            ProseCandidateSource,
            Mapping[str, Any],
            Mapping[str, Any],
            int,
        ],
        Awaitable[Mapping[str, Any]],
    ]
    repair_prose_candidate: Callable[
        [
            str,
            dict[str, Any],
            ProseCandidateSource,
            ProseCandidateRepairRequest,
        ],
        Awaitable[GeneratedProseCandidate],
    ] | None = None
    repair_state_candidate: Callable[
        [
            str,
            dict[str, Any],
            ProseCandidateSource,
            Mapping[str, Any],
            StateCandidateRepairRequest,
        ],
        Awaitable[ChapterGenerationResult],
    ] | None = None


@dataclass(frozen=True)
class ChapterCandidatePipelineResult:
    tokens: int
    attempts: tuple[dict[str, Any], ...]
    truncations: tuple[dict[str, Any], ...]
    outline_adherence: dict[str, Any]
    consistency_issues: tuple[dict[str, Any], ...]
    prose_run_id: str
    prose_run_revision: int
    prose_content_digest: str
    state_proposal_id: str
    repair_cycles_used: int
    finalization: dict[str, Any]


def _truncation(
    step: str,
    result: ChapterGenerationResult,
) -> dict[str, Any] | None:
    value = dict(result.truncation or {})
    if not (
        list(value.get("truncated_sections") or [])
        or dict(value.get("dropped_item_counts") or {})
    ):
        return None
    return {"step": step, **value}


def _adherence_matches_source(
    adherence: Mapping[str, Any],
    source: ProseCandidateSource,
) -> bool:
    run_id = adherence.get("source_prose_run_id")
    revision = adherence.get("source_prose_run_revision")
    digest = adherence.get("source_content_digest")
    return bool(
        isinstance(run_id, str)
        and run_id == source.source_run_id
        and type(revision) is int
        and revision >= 0
        and revision == source.source_run_revision
        and isinstance(digest, str)
        and digest == source.source_content_digest
    )


def _validate_adherence_gate(
    adherence: Mapping[str, Any],
    chapter: Mapping[str, Any],
) -> None:
    if adherence.get("verdict") != "pass":
        raise ChapterCandidatePipelineBlocked("正文候选未精确通过章纲符合度")
    issues = adherence.get("issues")
    if not isinstance(issues, list) or issues:
        raise ChapterCandidatePipelineBlocked("章纲符合度仍包含偏离问题")
    outline = chapter.get("outline")
    scenes = outline.get("scenes") if isinstance(outline, Mapping) else None
    coverage = adherence.get("scene_coverage")
    if (
        not isinstance(scenes, list)
        or not scenes
        or not isinstance(coverage, list)
        or len(coverage) != len(scenes)
    ):
        raise ChapterCandidatePipelineBlocked("章纲符合度没有覆盖全部场景")
    scene_indexes: list[int] = []
    for item in coverage:
        if not isinstance(item, Mapping):
            raise ChapterCandidatePipelineBlocked("章纲场景覆盖证据格式无效")
        scene_index = item.get("scene_index")
        if type(scene_index) is not int or item.get("status") != "covered":
            raise ChapterCandidatePipelineBlocked("章纲场景尚未全部落实")
        scene_indexes.append(scene_index)
    if (
        len(set(scene_indexes)) != len(scene_indexes)
        or set(scene_indexes) != set(range(1, len(scenes) + 1))
    ):
        raise ChapterCandidatePipelineBlocked("章纲场景覆盖不是完整唯一集合")


def _adherence_metadata(adherence: Mapping[str, Any]) -> dict[str, Any]:
    issues = list(adherence.get("issues") or [])
    categories = sorted(
        {
            str(item.get("category"))
            for item in issues
            if isinstance(item, Mapping) and item.get("category")
        }
    )
    return {
        "verdict": "pass",
        "scene_count": len(list(adherence.get("scene_coverage") or [])),
        "issue_count": len(issues),
        "issue_categories": categories,
        "source_prose_run_id": adherence["source_prose_run_id"],
        "source_prose_run_revision": adherence[
            "source_prose_run_revision"
        ],
        "source_content_digest": adherence["source_content_digest"],
    }


def _strict_repair_limit(value: Any) -> int:
    if (
        type(value) is not int
        or value < 0
        or value > MAX_FINALIZATION_REPAIR_CYCLES
    ):
        raise ValueError(
            "max_repair_cycles must be an integer between 0 and "
            f"{MAX_FINALIZATION_REPAIR_CYCLES}"
        )
    return value


def _outline_scene_indexes(chapter: Mapping[str, Any]) -> tuple[int, ...]:
    outline = chapter.get("outline")
    scenes = outline.get("scenes") if isinstance(outline, Mapping) else None
    if not isinstance(scenes, list) or not scenes:
        return ()
    return tuple(range(1, len(scenes) + 1))


def _completion_repair_request(
    *,
    cycle: int,
    source: ProseCandidateSource,
    chapter: Mapping[str, Any],
) -> ProseCandidateRepairRequest:
    raw_reasons = source.completion.get("reason_codes")
    reasons = (
        tuple(
            dict.fromkeys(
                item[:160]
                for item in raw_reasons[:20]
                if isinstance(item, str) and item
            )
        )
        if isinstance(raw_reasons, (list, tuple))
        else ()
    )
    return ProseCandidateRepairRequest(
        cycle=cycle,
        trigger="completion",
        reason_codes=reasons or ("completion_contract_failed",),
        issue_categories=("scene_coverage",),
        scene_indexes=_outline_scene_indexes(chapter),
    )


def _adherence_repair_request(
    *,
    cycle: int,
    adherence: Mapping[str, Any],
    chapter: Mapping[str, Any],
) -> ProseCandidateRepairRequest:
    categories: list[str] = []
    issues = adherence.get("issues")
    if isinstance(issues, list):
        categories.extend(
            str(item.get("category"))
            for item in issues
            if (
                isinstance(item, Mapping)
                and item.get("category") in _OUTLINE_ISSUE_CATEGORIES
            )
        )
    expected_indexes = set(_outline_scene_indexes(chapter))
    covered_indexes: set[int] = set()
    repair_indexes: set[int] = set()
    coverage = adherence.get("scene_coverage")
    if isinstance(coverage, list):
        for item in coverage:
            if not isinstance(item, Mapping):
                continue
            index = item.get("scene_index")
            if type(index) is not int or index not in expected_indexes:
                continue
            if item.get("status") == "covered":
                covered_indexes.add(index)
            else:
                repair_indexes.add(index)
    repair_indexes.update(expected_indexes - covered_indexes)
    if repair_indexes:
        categories.append("scene_coverage")
    stable_categories = tuple(dict.fromkeys(categories))
    return ProseCandidateRepairRequest(
        cycle=cycle,
        trigger="outline_adherence",
        reason_codes=("outline_adherence_failed",),
        issue_categories=stable_categories or ("scene_coverage",),
        scene_indexes=tuple(sorted(repair_indexes)),
    )


def _dropped_reference_count(value: Any) -> int:
    if isinstance(value, Mapping):
        return min(
            1_000,
            sum(_dropped_reference_count(item) for item in value.values()),
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return min(1_000, len(value))
    return int(bool(value))


def _state_repair_request(
    *,
    cycle: int,
    state: Mapping[str, Any],
    dropped: Mapping[str, Any],
) -> StateCandidateRepairRequest:
    raw_issues = state.get("consistency_issues")
    issues = raw_issues if isinstance(raw_issues, list) else []
    card_ids = tuple(
        sorted({
            str(item.get("card_id"))
            for item in issues
            if (
                isinstance(item, Mapping)
                and isinstance(item.get("card_id"), str)
                and item.get("card_id")
            )
        })
    )
    return StateCandidateRepairRequest(
        cycle=cycle,
        consistency_issue_count=(
            len(issues) if isinstance(raw_issues, list) else 1
        ),
        affected_card_ids=card_ids,
        dropped_reference_count=_dropped_reference_count(dropped),
    )


def _validate_prose_candidate(
    generated: GeneratedProseCandidate,
) -> tuple[ChapterGenerationResult, ProseCandidateSource]:
    prose = generated.generation
    if prose.stage is not ChapterGenerationStage.PROSE or prose.accepted:
        raise ChapterCandidatePipelineBlocked(
            "正文候选不是未接受的延迟生成结果"
        )
    return prose, generated.source


def _completion_passed(source: ProseCandidateSource) -> bool:
    completion = source.completion
    return bool(
        completion.get("can_write_formal_prose") is True
        and completion.get("status") == "complete"
    )


def _prose_repair_made_progress(
    previous: ProseCandidateSource,
    current: ProseCandidateSource,
) -> bool:
    return bool(
        current.source_run_id == previous.source_run_id
        and current.source_run_revision > previous.source_run_revision
        and current.source_content_digest != previous.source_content_digest
    )


def _next_repair_cycle(used: int, limit: int) -> int:
    if used >= limit:
        raise ChapterCandidatePipelineBlocked(
            "候选仍未通过闸门，已达到授权的修复次数上限"
        )
    return used + 1


def _validate_state_shape(
    state_result: ChapterGenerationResult,
) -> tuple[dict[str, Any], str, str, tuple[dict[str, Any], ...]]:
    if (
        state_result.stage is not ChapterGenerationStage.STATE
        or state_result.accepted
    ):
        raise ChapterCandidatePipelineBlocked(
            "状态候选不是未接受的延迟生成结果"
        )
    if not isinstance(state_result.value, Mapping):
        raise ChapterCandidatePipelineBlocked("状态候选不是有效映射")
    state = dict(state_result.value)
    proposal_id = state.get("proposal_id")
    acceptance_token = state.get("acceptance_token")
    if (
        not isinstance(proposal_id, str)
        or not proposal_id
        or not isinstance(acceptance_token, str)
        or not acceptance_token
    ):
        raise ChapterCandidatePipelineBlocked("状态候选缺少接受回执")
    raw_issues = state.get("consistency_issues")
    if not isinstance(raw_issues, list) or any(
        not isinstance(item, Mapping) for item in raw_issues
    ):
        raise ChapterCandidatePipelineBlocked("状态候选冲突证据格式无效")
    issues = tuple(dict(item) for item in raw_issues)
    return state, proposal_id, acceptance_token, issues


class ChapterCandidatePipeline:
    """Hide the ordered candidate gates behind one safe orchestration interface."""

    def __init__(self, deps: ChapterCandidatePipelineDeps) -> None:
        self._deps = deps

    async def _apply_prose_repair(
        self,
        *,
        novel_id: str,
        chapter: dict[str, Any],
        source: ProseCandidateSource,
        request: ProseCandidateRepairRequest,
        recorded_results: list[tuple[str, ChapterGenerationResult]],
    ) -> ProseCandidateSource:
        repair = self._deps.repair_prose_candidate
        if repair is None:
            raise ChapterCandidatePipelineBlocked("正文候选没有授权修复入口")
        generated = await repair(
            novel_id,
            chapter,
            source,
            request,
        )
        repaired_prose, repaired_source = _validate_prose_candidate(generated)
        if not _prose_repair_made_progress(source, repaired_source):
            raise ChapterCandidatePipelineBlocked("正文修复没有产生新候选")
        recorded_results.append(
            (f"prose_repair_{request.cycle}", repaired_prose)
        )
        return repaired_source

    async def run(
        self,
        *,
        novel_id: str,
        chapter: dict[str, Any],
        max_repair_cycles: int = 0,
    ) -> ChapterCandidatePipelineResult:
        repair_limit = _strict_repair_limit(max_repair_cycles)
        repair_cycles_used = 0
        recorded_results: list[tuple[str, ChapterGenerationResult]] = []

        generated = await self._deps.generate_prose_candidate(novel_id, chapter)
        prose, source = _validate_prose_candidate(generated)
        recorded_results.append(("prose", prose))

        review_count = 0
        while True:
            if not _completion_passed(source):
                if self._deps.repair_prose_candidate is None:
                    raise ChapterCandidatePipelineBlocked(
                        "正文候选未通过完成闸门"
                    )
                cycle = _next_repair_cycle(
                    repair_cycles_used,
                    repair_limit,
                )
                source = await self._apply_prose_repair(
                    novel_id=novel_id,
                    chapter=chapter,
                    source=source,
                    request=_completion_repair_request(
                        cycle=cycle,
                        source=source,
                        chapter=chapter,
                    ),
                    recorded_results=recorded_results,
                )
                repair_cycles_used = cycle
                continue

            reviewed = await self._deps.review_prose_candidate(
                novel_id,
                chapter,
                source,
            )
            review_count += 1
            recorded_results.append((
                (
                    "outline_adherence"
                    if review_count == 1
                    else f"outline_adherence_recheck_{review_count}"
                ),
                reviewed,
            ))
            if reviewed.stage is not ChapterGenerationStage.OUTLINE_ADHERENCE:
                raise ChapterCandidatePipelineBlocked(
                    "章纲符合度返回了错误阶段"
                )
            if not isinstance(reviewed.value, Mapping):
                raise ChapterCandidatePipelineBlocked(
                    "章纲符合度不是有效映射"
                )
            adherence = dict(reviewed.value)
            if not _adherence_matches_source(adherence, source):
                raise ChapterCandidatePipelineBlocked(
                    "章纲符合度没有绑定正文候选"
                )
            try:
                _validate_adherence_gate(adherence, chapter)
            except ChapterCandidatePipelineBlocked:
                if self._deps.repair_prose_candidate is None:
                    raise
                cycle = _next_repair_cycle(
                    repair_cycles_used,
                    repair_limit,
                )
                source = await self._apply_prose_repair(
                    novel_id=novel_id,
                    chapter=chapter,
                    source=source,
                    request=_adherence_repair_request(
                        cycle=cycle,
                        adherence=adherence,
                        chapter=chapter,
                    ),
                    recorded_results=recorded_results,
                )
                repair_cycles_used = cycle
                continue
            break

        state_result = await self._deps.generate_state_candidate(
            novel_id,
            chapter,
            source,
        )
        recorded_results.append(("state", state_result))
        while True:
            state, proposal_id, acceptance_token, consistency_issues = (
                _validate_state_shape(state_result)
            )
            dropped = dict(state_result.dropped or {})
            if not consistency_issues and not dropped:
                break
            if self._deps.repair_state_candidate is None:
                raise ChapterCandidatePipelineBlocked(
                    "状态候选仍有一致性冲突或无效引用"
                )
            cycle = _next_repair_cycle(
                repair_cycles_used,
                repair_limit,
            )
            repaired_state = await self._deps.repair_state_candidate(
                novel_id,
                chapter,
                source,
                state,
                _state_repair_request(
                    cycle=cycle,
                    state=state,
                    dropped=dropped,
                ),
            )
            if isinstance(repaired_state.value, Mapping):
                next_proposal_id = repaired_state.value.get("proposal_id")
                if next_proposal_id == proposal_id:
                    raise ChapterCandidatePipelineBlocked(
                        "状态修复没有产生新候选"
                    )
            repair_cycles_used = cycle
            state_result = repaired_state
            recorded_results.append((f"state_repair_{cycle}", state_result))

        finalization = dict(
            await self._deps.finalize(
                novel_id,
                chapter,
                source,
                adherence,
                state,
                repair_cycles_used,
            )
        )
        truncations = tuple(
            value
            for step, result in recorded_results
            for value in (_truncation(step, result),)
            if value is not None
        )
        return ChapterCandidatePipelineResult(
            tokens=sum(
                result.total_tokens for _step, result in recorded_results
            ),
            attempts=tuple(
                dict(attempt)
                for _step, result in recorded_results
                for attempt in result.attempts
            ),
            truncations=truncations,
            outline_adherence=_adherence_metadata(adherence),
            consistency_issues=consistency_issues,
            prose_run_id=source.source_run_id,
            prose_run_revision=source.source_run_revision,
            prose_content_digest=source.source_content_digest,
            state_proposal_id=proposal_id,
            repair_cycles_used=repair_cycles_used,
            finalization=finalization,
        )
