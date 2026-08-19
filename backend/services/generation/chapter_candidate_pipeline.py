"""Deferred chapter tail: candidates first, one deterministic formal commit last."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from backend.services.generation.chapter_generation_application import (
    ChapterGenerationResult,
    ChapterGenerationStage,
    ProseCandidateSource,
)
from backend.services.generation.headless_generation import (
    GeneratedProseCandidate,
)
from backend.services.generation.outline_adherence import is_material_deviation


class ChapterCandidatePipelineBlocked(ValueError):
    """A candidate gate failed before the formal chapter commit."""


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
        ],
        Awaitable[Mapping[str, Any]],
    ]


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


class ChapterCandidatePipeline:
    """Hide the ordered candidate gates behind one safe orchestration interface."""

    def __init__(self, deps: ChapterCandidatePipelineDeps) -> None:
        self._deps = deps

    async def run(
        self,
        *,
        novel_id: str,
        chapter: dict[str, Any],
    ) -> ChapterCandidatePipelineResult:
        generated = await self._deps.generate_prose_candidate(novel_id, chapter)
        prose = generated.generation
        source = generated.source
        if prose.stage is not ChapterGenerationStage.PROSE or prose.accepted:
            raise ChapterCandidatePipelineBlocked(
                "正文候选不是未接受的延迟生成结果"
            )
        completion = dict(source.completion or {})
        if (
            completion.get("can_write_formal_prose") is not True
            or str(completion.get("status") or "") != "complete"
        ):
            raise ChapterCandidatePipelineBlocked("正文候选未通过完成闸门")

        reviewed = await self._deps.review_prose_candidate(
            novel_id,
            chapter,
            source,
        )
        if reviewed.stage is not ChapterGenerationStage.OUTLINE_ADHERENCE:
            raise ChapterCandidatePipelineBlocked("章纲符合度返回了错误阶段")
        adherence = dict(reviewed.value or {})
        if (
            str(adherence.get("source_prose_run_id") or "")
            != source.source_run_id
            or adherence.get("source_prose_run_revision")
            != source.source_run_revision
            or str(adherence.get("source_content_digest") or "")
            != source.source_content_digest
        ):
            raise ChapterCandidatePipelineBlocked("章纲符合度没有绑定正文候选")
        if is_material_deviation(adherence):
            raise ChapterCandidatePipelineBlocked("正文候选存在实质章纲偏离")

        state_result = await self._deps.generate_state_candidate(
            novel_id,
            chapter,
            source,
        )
        if (
            state_result.stage is not ChapterGenerationStage.STATE
            or state_result.accepted
        ):
            raise ChapterCandidatePipelineBlocked(
                "状态候选不是未接受的延迟生成结果"
            )
        state = dict(state_result.value or {})
        proposal_id = str(state.get("proposal_id") or "")
        acceptance_token = str(state.get("acceptance_token") or "")
        if not proposal_id or not acceptance_token:
            raise ChapterCandidatePipelineBlocked("状态候选缺少接受回执")
        consistency_issues = tuple(
            dict(item) for item in list(state.get("consistency_issues") or [])
        )
        if consistency_issues:
            raise ChapterCandidatePipelineBlocked("状态候选仍有一致性冲突")

        finalization = dict(
            await self._deps.finalize(
                novel_id,
                chapter,
                source,
                adherence,
                state,
            )
        )
        results = (prose, reviewed, state_result)
        truncations = tuple(
            value
            for value in (
                _truncation("prose", prose),
                _truncation("outline_adherence", reviewed),
                _truncation("state", state_result),
            )
            if value is not None
        )
        return ChapterCandidatePipelineResult(
            tokens=sum(result.total_tokens for result in results),
            attempts=tuple(
                dict(attempt)
                for result in results
                for attempt in result.attempts
            ),
            truncations=truncations,
            outline_adherence=adherence,
            consistency_issues=consistency_issues,
            prose_run_id=source.source_run_id,
            prose_run_revision=source.source_run_revision,
            prose_content_digest=source.source_content_digest,
            state_proposal_id=proposal_id,
            repair_cycles_used=0,
            finalization=finalization,
        )
