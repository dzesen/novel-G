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
        if not _adherence_matches_source(adherence, source):
            raise ChapterCandidatePipelineBlocked("章纲符合度没有绑定正文候选")
        _validate_adherence_gate(adherence, chapter)

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
            outline_adherence=_adherence_metadata(adherence),
            consistency_issues=consistency_issues,
            prose_run_id=source.source_run_id,
            prose_run_revision=source.source_run_revision,
            prose_content_digest=source.source_content_digest,
            state_proposal_id=proposal_id,
            repair_cycles_used=0,
            finalization=finalization,
        )
