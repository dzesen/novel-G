"""批量生成启动前的只读检查与显式授权。

Module 的 Interface 只有 ``inspect`` 与 ``authorize``。资源查询、活动 Proposal 查询
和 Provider 规划通过 Adapter 注入，测试与生产调用同一套判定规则。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping

from backend.services.generation.job_planner import (
    REUSABLE_STATE_COMPLETION_STATUSES,
)
from backend.services.generation.prose_continuation import (
    SCENE_DIVERGENCE_STOP_FACTOR,
    ProseContinuationPolicy,
    prose_authorization_module,
)
from backend.services.generation.prose_token_bounds import (
    positive_token_limit,
    v3_output_token_bound,
)
from backend.services.llm.context_builder import ContextBudgetError


class ReadinessBlocked(ValueError):
    """当前报告包含阻止项或尚未确认的强警告。"""


class StaleReadiness(ValueError):
    """用户确认的报告已经不是当前启动快照。"""


READINESS_PROSE_PROMPT_INPUT_BOUNDS_KEY = (
    "_internal_readiness_prose_prompt_input_bounds"
)


@dataclass(frozen=True)
class ReadinessDeps:
    load_resource_counts: Callable[[str], Awaitable[dict[str, int]]]
    inspect_active_proposal: Callable[[str], Awaitable[dict[str, Any] | None]]
    plan_work: Callable[
        [list[dict[str, Any]], ProseContinuationPolicy, Mapping[str, Any] | None],
        dict[str, Any],
    ]
    prepare_generation_params: Callable[
        [str, list[dict[str, Any]], ProseContinuationPolicy, Mapping[str, Any] | None],
        Awaitable[Mapping[str, Any] | None],
    ] | None = None


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


def _batch_prose_authorization(
    *,
    scope: str,
    volume_id: str | None,
    work: Mapping[str, Any],
    planning: Mapping[str, Any],
    policy: ProseContinuationPolicy,
    token_budget: int | None,
    authorization_revision: int = 1,
) -> dict[str, Any]:
    """Bind mutable continuation authority to the current batch work snapshot."""
    strategy = dict(planning.get("prose_strategy") or {})
    maximum_base_calls = int(
        strategy.get("maximum_base_prose_calls")
        or strategy.get("maximum_prose_calls")
        or 0
    )
    estimated_scene_count = int(strategy.get("estimated_scene_count") or 0)
    work_steps = dict(work.get("steps") or {})
    prose_steps = dict(work_steps.get("prose") or {})
    estimated_chapter_count = int(
        strategy.get("estimated_prose_chapter_count")
        or prose_steps.get("generate")
        or 0
    )
    base_output_bound = int(strategy.get("base_output_token_bound") or 0)
    continuation_output_bound = int(
        strategy.get("continuation_output_token_bound") or 0
    )
    legacy_conservative_bound = int(
        strategy.get("conservative_token_bound") or 0
    )
    base_conservative_bound = int(
        strategy.get("conservative_base_token_bound")
        or legacy_conservative_bound
    )
    continuation_conservative_bound = int(
        strategy.get("conservative_continuation_token_bound")
        or legacy_conservative_bound
    )
    conservative_bound = max(
        legacy_conservative_bound,
        base_conservative_bound,
        continuation_conservative_bound,
    )
    token_bound_known = bool(strategy.get("token_bound_known"))
    if maximum_base_calls <= 0:
        coverage = prose_authorization_module.estimate_budget_coverage(
            token_budget=token_budget,
            token_bound_known=token_bound_known,
            estimated_chapter_count=estimated_chapter_count,
            conservative_base_token_bound=0,
            conservative_total_token_bound=0,
        )
        return {
            "policy": policy.to_dict(),
            "authorization_revision": max(1, int(authorization_revision)),
            "max_base_calls": 0,
            "max_automatic_continuation_calls": 0,
            "max_logical_prose_calls": 0,
            "max_actual_provider_attempts": 0,
            "base_output_token_bound": base_output_bound,
            "continuation_output_token_bound": continuation_output_bound,
            "conservative_base_token_bound": base_conservative_bound,
            "conservative_continuation_token_bound": continuation_conservative_bound,
            "conservative_token_bound": conservative_bound,
            "conservative_total_token_bound": 0,
            "token_bound_known": token_bound_known,
            "budget_coverage": coverage.to_dict(),
            "token_budget": token_budget,
            "readiness_digest": "",
        }
    content_identity = _digest(
        {
            "scope": scope,
            "volume_id": volume_id,
            "chapters": list(work.get("chapters") or []),
        }
    )
    provider_plan_revision = _digest(
        {
            "config_revision": planning.get("config_revision"),
            "capability_snapshot": planning.get("capability_snapshot"),
            "prose_strategy": strategy,
        }
    )
    authorization = prose_authorization_module.authorize(
        policy=policy,
        authorization_revision=authorization_revision,
        content_identity=content_identity,
        provider_plan_revision=provider_plan_revision,
        scheduled_base_calls=maximum_base_calls,
        scene_count=estimated_scene_count,
        conservative_token_bound=conservative_bound,
        base_output_token_bound=base_output_bound,
        continuation_output_token_bound=continuation_output_bound,
        conservative_base_token_bound=base_conservative_bound,
        conservative_continuation_token_bound=continuation_conservative_bound,
        token_bound_known=token_bound_known,
        estimated_chapter_count=estimated_chapter_count,
        token_budget=token_budget,
    ).to_dict()
    return {
        **authorization,
        # Budget-tracked runtimes disable opaque SDK retries, so one logical
        # prose call maps to exactly one maximum Provider attempt here.
        "max_actual_provider_attempts": authorization["max_logical_prose_calls"],
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
        prose_continuation_policy: ProseContinuationPolicy | None = None,
        token_budget: int | None = None,
        generation_params: Mapping[str, Any] | None = None,
        authorization_revision: int = 1,
    ) -> dict[str, Any]:
        continuation_policy = (
            prose_continuation_policy or ProseContinuationPolicy()
        )
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

        world_count = sum(
            int(resources.get(kind) or 0)
            for kind in ("location", "item", "rule", "lore")
        )
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
            planning_generation_params = generation_params
            needs_prose = work["steps"]["prose"]["generate"] > 0
            if needs_prose and self._deps.prepare_generation_params is not None:
                planning_generation_params = await self._deps.prepare_generation_params(
                    novel_id,
                    chapters,
                    continuation_policy,
                    generation_params,
                )
            planning = self._deps.plan_work(
                chapters,
                continuation_policy,
                planning_generation_params,
            ) if has_work else {
                "attempt_capacity": 0,
                "providers": [],
                "config_revision": "",
                "capability_snapshot": "",
            }
        except ContextBudgetError as exc:
            planning = {
                "attempt_capacity": 0,
                "providers": [],
                "config_revision": "",
                "capability_snapshot": "",
            }
            issues.append(
                _issue(
                    "generation_context_too_large",
                    "blocked",
                    details={"message": str(exc)[:500]},
                    action_codes=["review_generation_context"],
                )
            )
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
        high_risk_chapters = int(
            prose_strategy.get("high_risk_chapter_count") or 0
        )
        if high_risk_chapters:
            issues.append(
                _issue(
                    "prose_output_risk_requires_ack",
                    "warning_requires_ack",
                    details={
                        "chapter_count": high_risk_chapters,
                        "chapter_ids": list(
                            prose_strategy.get("high_risk_chapter_ids") or []
                        )[:50],
                        "maximum_target_words": int(
                            prose_strategy.get("maximum_target_words") or 0
                        ),
                        "safe_output_words": int(
                            prose_strategy.get("safe_output_words") or 0
                        ),
                        "maximum_prose_calls": int(
                            prose_strategy.get("maximum_prose_calls") or 0
                        ),
                        "output_limit_known": bool(
                            prose_strategy.get("output_limit_known")
                        ),
                    },
                    action_codes=["review_prose_plan"],
                )
            )


        prose_authorization = _batch_prose_authorization(
            scope=scope,
            volume_id=volume_id,
            work=work,
            planning=planning,
            policy=continuation_policy,
            token_budget=token_budget,
            authorization_revision=authorization_revision,
        )
        planning = {
            **planning,
            "prose_continuation_authorization": prose_authorization,
        }
        automatic_requested = bool(
            continuation_policy.permits_automatic_continuation
            and prose_authorization.get("max_base_calls")
        )
        if automatic_requested:
            issues.append(
                _issue(
                    "prose_scene_divergence_protection",
                    "warning",
                    details={
                        "counting_basis": "effective_word_count",
                        "stop_factor": SCENE_DIVERGENCE_STOP_FACTOR,
                        "manual_continuation_allowed": True,
                    },
                    action_codes=["review_prose_plan"],
                )
            )
            issues.append(
                _issue(
                    "automatic_continuations_require_confirmation",
                    "warning_requires_ack",
                    details={
                        "per_scene": continuation_policy.automatic_continuations_per_scene,
                        "continuation_target_words": continuation_policy.continuation_target_words,
                        "maximum_automatic_calls": prose_authorization.get(
                            "max_automatic_continuation_calls"
                        ),
                    },
                    action_codes=["review_prose_plan"],
                )
            )
            if not prose_authorization.get("token_bound_known"):
                issues.append(_issue("prose_token_bound_unproven", "blocked"))
            if token_budget is None:
                issues.append(
                    _issue(
                        "automatic_continuations_require_token_budget",
                        "blocked",
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
                    for kind in ("character", "location", "item", "rule", "lore")
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
        automatic_confirmation_required = any(
            item.get("code") == "automatic_continuations_require_confirmation"
            for item in report.get("issues", [])
        )
        if automatic_confirmation_required and supplied_digest is None:
            raise StaleReadiness(
                "自动续写必须使用当前 readiness 摘要确认后才能启动"
            )
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
    for card_type in ("location", "item", "rule", "lore"):
        result[card_type] = len(await worldbook_repo.list_cards(novel_id, card_type))
    return result


async def _inspect_active_proposal(novel_id: str) -> dict[str, Any] | None:
    from backend.services.novel.reference_card_curation import (
        reference_card_curation_service,
    )

    return await reference_card_curation_service.inspect(novel_id)


async def _prepare_generation_params(
    novel_id: str,
    chapters: list[dict[str, Any]],
    policy: ProseContinuationPolicy,
    generation_params: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    """Attach trusted scalar prompt bounds for the read-only planning pass."""
    from backend.services.generation.headless_generation import (
        build_batch_prose_prompt_input_bounds,
    )

    return {
        **dict(generation_params or {}),
        READINESS_PROSE_PROMPT_INPUT_BOUNDS_KEY: (
            await build_batch_prose_prompt_input_bounds(
                novel_id=novel_id,
                chapters=chapters,
                policy=policy,
                generation_params=generation_params,
            )
        ),
    }


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
    high_risk_chapter_ids: list[str] = []
    maximum_target_words = 0
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
            chapter_id = str(chapter.get("_id") or "")
            outline = chapter.get("outline") or {}
            if not outline:
                target_words = int(chapter.get("words_per_chapter") or 3_000)
                unknown_outline_chapters += 1
                maximum_prose_calls += 32
                maximum_target_words = max(maximum_target_words, target_words)
                high_risk_chapter_ids.append(chapter_id)
                continue
            target_words = int(
                outline.get("target_word_count")
                or chapter.get("words_per_chapter")
                or 3_000
            )
            prose_plan = prose_completion_module.plan(
                outline=outline,
                target_word_count=target_words,
                provider_capability={
                    "max_output_tokens": prose_text_plan.max_output_tokens,
                    "model": prose_text_plan.provider_model,
                },
                request_overrides={},
            )
            maximum_prose_calls += prose_plan.call_count
            maximum_target_words = max(maximum_target_words, target_words)
            if (
                prose_plan.mode == "scene_segments"
                or capability_plan.provider_output_limit is None
            ):
                high_risk_chapter_ids.append(chapter_id)
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
            "high_risk_chapter_count": len(high_risk_chapter_ids),
            "high_risk_chapter_ids": high_risk_chapter_ids,
            "maximum_target_words": maximum_target_words,
            **prose_capability,
        },
    }



def _plan_work_with_prose_continuation(
    chapters: list[dict[str, Any]],
    policy: ProseContinuationPolicy,
    generation_params: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Deepen the existing batch plan with bounded per-scene prose authority."""
    from backend.llm.schemas.novel_pydantic import MAX_CHAPTER_OUTLINE_SCENES
    from backend.services.generation.headless_generation import (
        PROSE_STEP,
        PROSE_WORKFLOW,
        estimate_worklist_attempt_capacity,
    )
    from backend.services.generation.prose_completion import prose_completion_module
    from backend.services.llm.generation_runtime import (
        WorkflowStepTarget,
        create_generation_runtime,
    )

    base = _plan_work(chapters)
    values = dict(generation_params or {})
    chapters_needing_prose = [
        chapter for chapter in chapters if not _has_text(chapter, "content")
    ]
    strategy = dict(base.get("prose_strategy") or {})
    if not chapters_needing_prose:
        return {
            **base,
            "generation_params_digest": _digest(values),
            "prose_strategy": {
                **strategy,
                "maximum_base_prose_calls": 0,
                "estimated_prose_chapter_count": 0,
                "estimated_scene_count": 0,
                "maximum_automatic_continuation_calls": 0,
                "maximum_logical_prose_calls": 0,
                "max_actual_provider_attempts": 0,
                "maximum_base_call_target_words": 0,
                "continuation_call_target_words": 0,
                "base_output_token_bound": 0,
                "continuation_output_token_bound": 0,
                "prompt_input_bound_basis": "not_applicable",
                "base_prompt_input_token_bound": 0,
                "continuation_prompt_input_token_bound": 0,
                "conservative_base_token_bound": 0,
                "conservative_continuation_token_bound": 0,
                "conservative_token_bound": 0,
                "conservative_total_token_bound": 0,
                "token_bound_known": False,
            },
        }
    overrides = {
        key: values[key]
        for key in (
            "temperature", "top_p", "max_tokens", "presence_penalty",
            "frequency_penalty", "system_prompt",
        )
        if values.get(key) is not None
    }
    runtime_kwargs = (
        {} if values.get("allow_failure_retry", True)
        else {"max_provider_retries": 0}
    )
    runtime = create_generation_runtime(**runtime_kwargs)
    prose_plan = runtime.plan_text(
        WorkflowStepTarget(PROSE_WORKFLOW, PROSE_STEP)
    )
    # Mirror GenerationRuntime._conservative_token_bound: a per-job max_tokens
    # override is the effective provider output cap and must be what readiness
    # displays, even when the provider configuration has no default cap.
    effective_prose_output_token_limit = (
        positive_token_limit(overrides.get("max_tokens"))
        or positive_token_limit(getattr(prose_plan, "max_output_tokens", None))
    )
    capability_plan = prose_completion_module.plan(
        outline={"scenes": [{}]},
        target_word_count=3_000,
        provider_capability={
            "max_output_tokens": effective_prose_output_token_limit,
            "model": prose_plan.provider_model,
        },
        request_overrides=overrides,
    )
    maximum_base_calls = 0
    estimated_scene_count = 0
    unknown_outline_chapters = 0
    maximum_base_call_target_words = capability_plan.safe_output_budget
    for chapter in chapters_needing_prose:
        outline = chapter.get("outline") or {}
        if not outline:
            unknown_outline_chapters += 1
            maximum_base_calls += 32
            estimated_scene_count += MAX_CHAPTER_OUTLINE_SCENES
            continue
        target_words = int(
            outline.get("target_word_count")
            or chapter.get("words_per_chapter")
            or 3_000
        )
        chapter_plan = prose_completion_module.plan(
            outline=outline,
            target_word_count=target_words,
            provider_capability={
                "max_output_tokens": effective_prose_output_token_limit,
                "model": prose_plan.provider_model,
            },
            request_overrides=overrides,
        )
        maximum_base_calls += chapter_plan.scheduled_base_call_count
        estimated_scene_count += chapter_plan.scene_count
        maximum_base_call_target_words = max(
            maximum_base_call_target_words,
            *chapter_plan.segment_budgets,
        )

    inherited_max_tokens = values.get("max_tokens")
    base_output_token_bound = v3_output_token_bound(
        target_words=maximum_base_call_target_words,
        inherited_max_tokens=inherited_max_tokens,
    )
    continuation_output_token_bound = v3_output_token_bound(
        target_words=policy.continuation_target_words,
        inherited_max_tokens=inherited_max_tokens,
    )
    max_context = positive_token_limit(
        getattr(prose_plan, "max_context_tokens", None)
    )
    raw_prompt_input_bounds = values.get(
        READINESS_PROSE_PROMPT_INPUT_BOUNDS_KEY
    )
    prompt_input_bounds = (
        dict(raw_prompt_input_bounds)
        if isinstance(raw_prompt_input_bounds, Mapping)
        else {}
    )
    try:
        base_prompt_input_bound = max(
            0,
            int(prompt_input_bounds.get("base_input_token_bound") or 0),
        )
    except (TypeError, ValueError):
        base_prompt_input_bound = 0
    try:
        continuation_prompt_input_bound = max(
            0,
            int(prompt_input_bounds.get("continuation_input_token_bound") or 0),
        )
    except (TypeError, ValueError):
        continuation_prompt_input_bound = 0
    conservative_base_token_bound = (
        base_prompt_input_bound + base_output_token_bound
        if base_prompt_input_bound > 0
        else 0
    )
    conservative_continuation_token_bound = (
        continuation_prompt_input_bound + continuation_output_token_bound
        if continuation_prompt_input_bound > 0
        else 0
    )
    conservative_token_bound = max(
        conservative_base_token_bound,
        conservative_continuation_token_bound,
    )
    maximum_automatic_calls = (
        estimated_scene_count * policy.automatic_continuations_per_scene
    )
    maximum_logical_calls = maximum_base_calls + maximum_automatic_calls
    conservative_total_token_bound = (
        maximum_base_calls * conservative_base_token_bound
        + maximum_automatic_calls * conservative_continuation_token_bound
    )
    maximum_call_target_words = max(
        maximum_base_call_target_words,
        policy.continuation_target_words,
    )
    return {
        **base,
        "attempt_capacity": estimate_worklist_attempt_capacity(chapters, values),
        "generation_params_digest": _digest(values),
        "prose_strategy": {
            **strategy,
            "provider_alias": prose_plan.provider_alias,
            "provider_model": prose_plan.provider_model,
            "max_output_tokens": effective_prose_output_token_limit,
            "max_context_tokens": max_context,
            "prompt_input_bound_basis": str(
                prompt_input_bounds.get("basis") or "unavailable"
            ),
            "base_prompt_input_token_bound": base_prompt_input_bound,
            "continuation_prompt_input_token_bound": (
                continuation_prompt_input_bound
            ),
            "unknown_outline_chapters": unknown_outline_chapters,
            "unknown_scene_upper_bound": MAX_CHAPTER_OUTLINE_SCENES,
            "maximum_prose_calls": maximum_base_calls,
            "maximum_base_prose_calls": maximum_base_calls,
            "estimated_prose_chapter_count": len(chapters_needing_prose),
            "estimated_scene_count": estimated_scene_count,
            "maximum_automatic_continuation_calls": maximum_automatic_calls,
            "maximum_logical_prose_calls": maximum_logical_calls,
            "max_actual_provider_attempts": maximum_logical_calls,
            "maximum_base_call_target_words": maximum_base_call_target_words,
            "continuation_call_target_words": policy.continuation_target_words,
            "maximum_call_target_words": maximum_call_target_words,
            "base_output_token_bound": base_output_token_bound,
            "continuation_output_token_bound": continuation_output_token_bound,
            "conservative_base_token_bound": conservative_base_token_bound,
            "conservative_continuation_token_bound": conservative_continuation_token_bound,
            "conservative_token_bound": conservative_token_bound,
            "conservative_total_token_bound": conservative_total_token_bound,
            "token_bound_known": conservative_token_bound > 0,
        },
    }


generation_readiness_module = GenerationReadinessModule(
    ReadinessDeps(
        load_resource_counts=_load_resource_counts,
        inspect_active_proposal=_inspect_active_proposal,
        plan_work=_plan_work_with_prose_continuation,
        prepare_generation_params=_prepare_generation_params,
    )
)
