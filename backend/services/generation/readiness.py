"""批量生成启动前的只读检查与显式授权。

Module 的 Interface 只有 ``inspect`` 与 ``authorize``。资源查询、活动 Proposal 查询
和 Provider 规划通过 Adapter 注入，测试与生产调用同一套判定规则。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable

from backend.services.generation.job_planner import (
    REUSABLE_STATE_COMPLETION_STATUSES,
)


class ReadinessBlocked(ValueError):
    """当前报告包含阻止项或尚未确认的强警告。"""


class StaleReadiness(ValueError):
    """用户确认的报告已经不是当前启动快照。"""


@dataclass(frozen=True)
class ReadinessDeps:
    load_resource_counts: Callable[[str], Awaitable[dict[str, int]]]
    inspect_active_proposal: Callable[[str], Awaitable[dict[str, Any] | None]]
    plan_work: Callable[[list[dict[str, Any]]], dict[str, Any]]


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _digest(value: Any) -> str:
    encoded = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _has_text(chapter: dict[str, Any], key: str) -> bool:
    if key == "outline":
        return bool(chapter.get("outline"))
    return bool(str(chapter.get(key) or "").strip())


def _work_summary(chapters: list[dict[str, Any]]) -> dict[str, Any]:
    steps = {
        name: {"generate": 0, "reuse": 0}
        for name in ("outline", "prose", "state")
    }
    chapter_snapshots = []
    for chapter in chapters:
        for step, field in (("outline", "outline"), ("prose", "content")):
            steps[step]["reuse" if _has_text(chapter, field) else "generate"] += 1
        state_status = str(
            (chapter.get("state_completion") or {}).get("status") or "missing"
        )
        steps["state"][
            "reuse"
            if state_status in REUSABLE_STATE_COMPLETION_STATUSES
            else "generate"
        ] += 1
        outline = chapter.get("outline") or {}
        chapter_snapshots.append(
            {
                "chapter_id": str(chapter.get("_id") or ""),
                "order_index": int(chapter.get("order_index") or 0),
                "has_outline": bool(outline),
                "has_content": _has_text(chapter, "content"),
                "has_summary": _has_text(chapter, "summary"),
                "state_completion_status": state_status,
                "prose_acceptance_state": str(
                    (chapter.get("prose_acceptance") or {}).get("state")
                    or (chapter.get("state_completion") or {}).get(
                        "prose_acceptance_state"
                    )
                    or "unknown_legacy"
                ),
                "target_word_count": outline.get("target_word_count"),
                "scene_count": len(outline.get("scenes") or []),
                "updated_at": chapter.get("updated_at"),
            }
        )
    return {
        "chapter_count": len(chapters),
        "steps": steps,
        "chapters": chapter_snapshots,
    }


def _issue(
    code: str,
    level: str,
    *,
    details: dict[str, Any] | None = None,
    action_codes: Iterable[str] = (),
) -> dict[str, Any]:
    return {
        "code": code,
        "level": level,
        "details": details or {},
        "action_codes": list(action_codes),
    }


class GenerationReadinessModule:
    def __init__(self, deps: ReadinessDeps) -> None:
        self._deps = deps

    async def inspect(
        self,
        *,
        novel_id: str,
        scope: str,
        volume_id: str | None,
        chapters: list[dict[str, Any]],
    ) -> dict[str, Any]:
        work = _work_summary(chapters)
        resources = await self._deps.load_resource_counts(novel_id)
        proposal = await self._deps.inspect_active_proposal(novel_id)
        issues: list[dict[str, Any]] = []

        has_work = any(
            counts["generate"] > 0 for counts in work["steps"].values()
        )
        if not chapters or not has_work:
            issues.append(
                _issue(
                    "no_generation_work",
                    "blocked",
                    action_codes=["return_to_chapters"],
                )
            )

        if proposal is not None:
            issues.append(
                _issue(
                    "reference_card_proposal_pending",
                    "blocked",
                    details={
                        "proposal_id": str(proposal.get("proposal_id") or ""),
                        "status": str(proposal.get("status") or ""),
                    },
                    action_codes=["review_reference_card_proposal", "reject_reference_card_proposal"],
                )
            )

        partial_prose = [
            snapshot["chapter_id"]
            for snapshot in work["chapters"]
            if snapshot["prose_acceptance_state"] == "partial_manual_required"
            and snapshot["state_completion_status"]
            not in REUSABLE_STATE_COMPLETION_STATUSES
        ]
        if partial_prose:
            issues.append(
                _issue(
                    "partial_prose_requires_manual_completion",
                    "blocked",
                    details={
                        "chapter_count": len(partial_prose),
                        "chapter_ids": partial_prose[:50],
                    },
                    action_codes=["complete_prose_manually"],
                )
            )

        runs_character_sensitive_steps = (
            work["steps"]["outline"]["generate"] > 0
            or work["steps"]["state"]["generate"] > 0
        )
        if runs_character_sensitive_steps and int(resources.get("character") or 0) == 0:
            issues.append(
                _issue(
                    "character_cards_missing",
                    "warning_requires_ack",
                    details={"character_count": 0},
                    action_codes=["curate_reference_cards", "create_character_card"],
                )
            )

        world_count = sum(int(resources.get(kind) or 0) for kind in ("location", "item", "rule"))
        if has_work and world_count == 0:
            issues.append(
                _issue(
                    "world_cards_missing",
                    "warning",
                    details={"world_card_count": 0},
                    action_codes=["curate_reference_cards"],
                )
            )

        try:
            planning = self._deps.plan_work(chapters) if has_work else {
                "attempt_capacity": 0,
                "providers": [],
                "config_revision": "",
                "capability_snapshot": "",
            }
        except Exception as exc:
            planning = {
                "attempt_capacity": 0,
                "providers": [],
                "config_revision": "",
                "capability_snapshot": "",
            }
            issues.append(
                _issue(
                    "provider_plan_invalid",
                    "blocked",
                    details={"message": str(exc)[:500]},
                    action_codes=["open_provider_settings"],
                )
            )
        prose_strategy = planning.get("prose_strategy") or {}
        segmented_chapters = int(
            prose_strategy.get("scene_segment_chapters") or 0
        )
        unknown_outline_chapters = int(
            prose_strategy.get("unknown_outline_chapters") or 0
        )
        if segmented_chapters or unknown_outline_chapters:
            issues.append(
                _issue(
                    "prose_scene_segmentation_planned",
                    "warning",
                    details={
                        "scene_segment_chapters": segmented_chapters,
                        "unknown_outline_chapters": unknown_outline_chapters,
                        "maximum_prose_calls": int(
                            prose_strategy.get("maximum_prose_calls") or 0
                        ),
                    },
                    action_codes=["review_prose_plan"],
                )
            )

        snapshot = {
            "version": 1,
            "novel_id": str(novel_id),
            "scope": scope,
            "volume_id": str(volume_id) if volume_id else None,
            "work": work,
            "resources": {
                **{
                    kind: int(resources.get(kind) or 0)
                    for kind in ("character", "location", "item", "rule")
                },
                "narrative_revision": int(
                    resources.get("narrative_revision") or 0
                ),
            },
            "active_proposal": (
                {
                    "proposal_id": str(proposal.get("proposal_id") or ""),
                    "status": str(proposal.get("status") or ""),
                }
                if proposal is not None
                else None
            ),
            "planning": planning,
            "issues": issues,
        }
        levels = {item["level"] for item in issues}
        status = (
            "blocked"
            if "blocked" in levels
            else "warning_requires_ack"
            if "warning_requires_ack" in levels
            else "warning"
            if "warning" in levels
            else "ready"
        )
        return {
            **snapshot,
            "status": status,
            "digest": _digest(snapshot),
        }

    def authorize(
        self,
        report: dict[str, Any],
        *,
        supplied_digest: str | None,
        acknowledged_warning_codes: Iterable[str],
    ) -> dict[str, Any]:
        current_digest = str(report.get("digest") or "")
        if supplied_digest is not None and supplied_digest != current_digest:
            raise StaleReadiness("生成前检查已过期，请重新检查后再启动")

        blocked = [
            item["code"]
            for item in report.get("issues", [])
            if item.get("level") == "blocked"
        ]
        if blocked:
            raise ReadinessBlocked(f"生成被前置检查阻止: {', '.join(blocked)}")

        acknowledged = sorted(set(str(code) for code in acknowledged_warning_codes))
        required = [
            item["code"]
            for item in report.get("issues", [])
            if item.get("level") == "warning_requires_ack"
            and item.get("code") not in acknowledged
        ]
        if required:
            raise ReadinessBlocked(f"以下警告需要明确确认: {', '.join(required)}")

        return {
            "digest": current_digest,
            "acknowledged_warning_codes": acknowledged,
            "issues": list(report.get("issues") or []),
            "work": report.get("work") or {},
            "resources": report.get("resources") or {},
            "planning": report.get("planning") or {},
        }


async def _load_resource_counts(novel_id: str) -> dict[str, int]:
    from backend.db.narrative_revision import narrative_revision_store
    from backend.db.repositories.character_repository import character_repo
    from backend.db.repositories.worldbook_repository import worldbook_repo

    result = {
        "character": len(await character_repo.list_cards(novel_id, "character")),
        "narrative_revision": await narrative_revision_store.current(novel_id),
    }
    for card_type in ("location", "item", "rule"):
        result[card_type] = len(await worldbook_repo.list_cards(novel_id, card_type))
    return result


async def _inspect_active_proposal(novel_id: str) -> dict[str, Any] | None:
    from backend.services.novel.reference_card_curation import (
        reference_card_curation_service,
    )

    return await reference_card_curation_service.inspect(novel_id)


def _plan_work(chapters: list[dict[str, Any]]) -> dict[str, Any]:
    from backend.services.generation.headless_generation import (
        CHAPTER_OUTLINE_STEP,
        CHAPTER_OUTLINE_WORKFLOW,
        PROSE_STEP,
        PROSE_WORKFLOW,
        STATE_STEP,
        STATE_WORKFLOW,
        estimate_worklist_attempt_capacity,
    )
    from backend.services.llm.generation_runtime import (
        WorkflowStepTarget,
        create_generation_runtime,
    )
    from backend.services.generation.prose_completion import (
        prose_completion_module,
    )

    runtime = create_generation_runtime()
    plans = []
    if any(not chapter.get("outline") for chapter in chapters):
        plans.append(runtime.plan_structured(
            WorkflowStepTarget(CHAPTER_OUTLINE_WORKFLOW, CHAPTER_OUTLINE_STEP)
        ))
    if any(not _has_text(chapter, "content") for chapter in chapters):
        plans.append(runtime.plan_text(WorkflowStepTarget(PROSE_WORKFLOW, PROSE_STEP)))
    if any(
        str((chapter.get("state_completion") or {}).get("status") or "missing")
        not in REUSABLE_STATE_COMPLETION_STATUSES
        for chapter in chapters
    ):
        plans.append(runtime.plan_structured(WorkflowStepTarget(STATE_WORKFLOW, STATE_STEP)))
    revisions = sorted({plan.config_revision for plan in plans})
    capabilities = sorted({plan.capability_snapshot for plan in plans})
    prose_text_plan = next(
        (
            plan
            for plan in plans
            if isinstance(plan.target, WorkflowStepTarget)
            and plan.target.workflow_name == PROSE_WORKFLOW
            and plan.target.step_name == PROSE_STEP
        ),
        None,
    )
    single_chapters = 0
    segmented_chapters = 0
    unknown_outline_chapters = 0
    maximum_prose_calls = 0
    prose_capability: dict[str, Any] = {}
    if prose_text_plan is not None:
        capability_plan = prose_completion_module.plan(
            outline={"scenes": [{}]},
            target_word_count=3_000,
            provider_capability={
                "max_output_tokens": prose_text_plan.max_output_tokens,
                "model": prose_text_plan.provider_model,
            },
            request_overrides={},
        )
        prose_capability = {
            "provider_alias": prose_text_plan.provider_alias,
            "provider_model": prose_text_plan.provider_model,
            "max_output_tokens": prose_text_plan.max_output_tokens,
            "safe_output_words": capability_plan.safe_output_budget,
            "output_limit_known": capability_plan.provider_output_limit
            is not None,
        }
        for chapter in chapters:
            if _has_text(chapter, "content"):
                continue
            outline = chapter.get("outline") or {}
            if not outline:
                unknown_outline_chapters += 1
                maximum_prose_calls += 32
                continue
            prose_plan = prose_completion_module.plan(
                outline=outline,
                target_word_count=int(
                    outline.get("target_word_count")
                    or chapter.get("words_per_chapter")
                    or 3_000
                ),
                provider_capability={
                    "max_output_tokens": prose_text_plan.max_output_tokens,
                    "model": prose_text_plan.provider_model,
                },
                request_overrides={},
            )
            maximum_prose_calls += prose_plan.call_count
            if prose_plan.mode == "scene_segments":
                segmented_chapters += 1
            else:
                single_chapters += 1
    return {
        "attempt_capacity": estimate_worklist_attempt_capacity(chapters),
        "providers": sorted({plan.provider_alias for plan in plans}),
        "config_revision": "|".join(revisions),
        "capability_snapshot": "|".join(capabilities),
        "prose_strategy": {
            "single_call_chapters": single_chapters,
            "scene_segment_chapters": segmented_chapters,
            "unknown_outline_chapters": unknown_outline_chapters,
            "maximum_prose_calls": maximum_prose_calls,
            **prose_capability,
        },
    }


generation_readiness_module = GenerationReadinessModule(
    ReadinessDeps(
        load_resource_counts=_load_resource_counts,
        inspect_active_proposal=_inspect_active_proposal,
        plan_work=_plan_work,
    )
)
