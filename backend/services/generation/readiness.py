"""批量生成启动前的只读检查与显式授权。

Module 的 Interface 只有 ``inspect`` 与 ``authorize``。资源查询、活动 Proposal 查询
和 Provider 规划通过 Adapter 注入，测试与生产调用同一套判定规则。
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping

from backend.services.generation.job_planner import (
    REUSABLE_STATE_COMPLETION_STATUSES,
)
from backend.services.generation.chapter_finalization import (
    build_chapter_finalization_authorization,
)
from backend.services.generation.chapter_candidate_authorization import (
    CANDIDATE_PIPELINE_REVISION,
    CandidateJobGenerationPlans,
    build_candidate_job_execution_authorization,
    build_chapter_candidate_repair_authorization,
    parse_candidate_repair_authorization,
)
from backend.services.generation.narrative_quality_authorization import (
    build_narrative_quality_signal_authorization,
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
from backend.services.generation.prose_protocol import (
    maximum_v2_chapter_base_calls,
    v2_scene_base_call_safe_output_budget,
)
from backend.services.generation.prose_generation import (
    planned_base_call_output_capacity_words,
)
from backend.services.generation.reference_card_auto_creation import (
    ReferenceCardAutoCreationPolicy,
    build_reference_card_creation_authorization,
)
from backend.services.generation.reference_card_dependency_repair import (
    build_reference_card_repair_plan_authorization,
    parse_reference_card_repair_plan_authorization,
)
from backend.services.generation.provider_budget import (
    ProviderBudgetBound,
    merge_provider_bounds,
    scale_provider_bounds,
    structured_call_budget,
)
from backend.scene_contract_versions import (
    SCENE_TRANSITION_CONTRACT_VERSION,
    require_known_scene_contract_version,
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
    load_resource_counts: Callable[[str], Awaitable[dict[str, Any]]]
    inspect_active_proposal: Callable[[str], Awaitable[dict[str, Any] | None]]
    plan_work: Callable[
        [list[dict[str, Any]], ProseContinuationPolicy, Mapping[str, Any] | None],
        dict[str, Any],
    ]
    prepare_generation_params: Callable[
        [str, list[dict[str, Any]], ProseContinuationPolicy, Mapping[str, Any] | None],
        Awaitable[Mapping[str, Any] | None],
    ] | None = None
    plan_candidate_repairs: Callable[
        [list[dict[str, Any]], int, int, Mapping[str, Any] | None],
        Mapping[str, Any],
    ] | None = None
    plan_reference_card_repairs: Callable[
        [list[dict[str, Any]], int, Mapping[str, Any] | None],
        Mapping[str, Any],
    ] | None = None


@dataclass(frozen=True)
class _BaseGenerationBudget:
    maximum_provider_attempts_total: int
    maximum_tokens_total: int
    token_bound_known: bool
    provider_bounds: tuple[ProviderBudgetBound, ...]
    maximum_input_tokens_total: int | None = None
    maximum_output_tokens_total: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "base_generation_budget.v1",
            "maximum_provider_attempts_total": (
                self.maximum_provider_attempts_total
            ),
            "maximum_tokens_total": self.maximum_tokens_total,
            "token_bound_known": self.token_bound_known,
            "provider_bounds": [
                {
                    "provider_alias": bound.provider_alias,
                    "maximum_paid_attempts_total": bound.paid_attempts,
                    "maximum_tokens_total": bound.tokens,
                }
                for bound in self.provider_bounds
            ],
        }

    def token_upper_bound(self) -> dict[str, int] | None:
        if (
            self.maximum_input_tokens_total is None
            or self.maximum_output_tokens_total is None
        ):
            return None
        input_tokens = _strict_non_negative_budget_int(
            self.maximum_input_tokens_total,
            field="base generation input-token bound",
        )
        output_tokens = _strict_non_negative_budget_int(
            self.maximum_output_tokens_total,
            field="base generation output-token bound",
        )
        if input_tokens + output_tokens != self.maximum_tokens_total:
            raise ValueError("base generation token components changed")
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": self.maximum_tokens_total,
        }


def _strict_non_negative_budget_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} is invalid")
    return value


def _parse_base_generation_budget(value: Any) -> _BaseGenerationBudget:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "maximum_provider_attempts_total",
        "maximum_tokens_total",
        "token_bound_known",
        "provider_bounds",
    }:
        raise ValueError("base generation budget is invalid")
    if value.get("schema_version") != "base_generation_budget.v1":
        raise ValueError("base generation budget version is invalid")
    token_bound_known = value.get("token_bound_known")
    if not isinstance(token_bound_known, bool):
        raise ValueError("base generation token-bound status is invalid")
    raw_bounds = value.get("provider_bounds")
    if not isinstance(raw_bounds, list):
        raise ValueError("base generation Provider bounds are invalid")
    bounds: list[ProviderBudgetBound] = []
    for raw in raw_bounds:
        if not isinstance(raw, Mapping) or set(raw) != {
            "provider_alias",
            "maximum_paid_attempts_total",
            "maximum_tokens_total",
        }:
            raise ValueError("base generation Provider bound is invalid")
        bounds.append(
            ProviderBudgetBound(
                provider_alias=str(raw.get("provider_alias") or ""),
                paid_attempts=_strict_non_negative_budget_int(
                    raw.get("maximum_paid_attempts_total"),
                    field="base Provider paid-attempt bound",
                ),
                tokens=_strict_non_negative_budget_int(
                    raw.get("maximum_tokens_total"),
                    field="base Provider token bound",
                ),
            )
        )
    normalized = merge_provider_bounds(bounds)
    if len(normalized) != len(bounds):
        raise ValueError("base generation Provider aliases are duplicated")
    attempts = _strict_non_negative_budget_int(
        value.get("maximum_provider_attempts_total"),
        field="base generation paid-attempt bound",
    )
    tokens = _strict_non_negative_budget_int(
        value.get("maximum_tokens_total"),
        field="base generation token bound",
    )
    if attempts != sum(item.paid_attempts for item in normalized):
        raise ValueError("base generation paid-attempt total changed")
    if tokens != sum(item.tokens for item in normalized):
        raise ValueError("base generation token total changed")
    return _BaseGenerationBudget(
        maximum_provider_attempts_total=attempts,
        maximum_tokens_total=tokens,
        token_bound_known=token_bound_known,
        provider_bounds=normalized,
    )


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
                "volume_id": str(chapter.get("volume_id") or ""),
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
        book_structure_initialization: Mapping[str, Any] | None = None,
        prose_continuation_policy: ProseContinuationPolicy | None = None,
        token_budget: int | None = None,
        generation_params: Mapping[str, Any] | None = None,
        authorization_revision: int = 1,
        outline_deviation_policy: str = "pause_for_rewrite",
        reference_card_auto_creation_policy: (
            ReferenceCardAutoCreationPolicy | Mapping[str, Any] | None
        ) = None,
    ) -> dict[str, Any]:
        if outline_deviation_policy not in {
            "pause_for_rewrite",
            "accept_and_continue",
        }:
            raise ValueError("outline deviation policy is invalid")
        continuation_policy = (
            prose_continuation_policy or ProseContinuationPolicy()
        )
        auto_creation_policy = (
            reference_card_auto_creation_policy
            if isinstance(
                reference_card_auto_creation_policy,
                ReferenceCardAutoCreationPolicy,
            )
            else ReferenceCardAutoCreationPolicy.from_mapping(
                reference_card_auto_creation_policy
            )
        )
        work = _work_summary(chapters)
        structure_snapshot = (
            dict(book_structure_initialization)
            if scope == "book"
            and isinstance(book_structure_initialization, Mapping)
            else None
        )
        structure_state = str(
            (structure_snapshot or {}).get("state") or "not_applicable"
        )
        structure_work = (
            dict(structure_snapshot.get("work") or {})
            if structure_snapshot is not None
            else {}
        )
        if structure_snapshot is not None:
            work["structure"] = structure_work
        resources = await self._deps.load_resource_counts(novel_id)
        proposal = await self._deps.inspect_active_proposal(novel_id)
        issues: list[dict[str, Any]] = []

        has_chapter_work = any(
            counts["generate"] > 0 for counts in work["steps"].values()
        )
        has_structure_work = bool(
            structure_state == "missing"
            and int(structure_work.get("generate") or 0) > 0
        )
        has_work = has_chapter_work or has_structure_work

        world_baseline_state = str(
            resources.get("world_baseline_state") or "not_required_legacy"
        )
        world_material_count = sum(
            int(resources.get(kind) or 0)
            for kind in (
                "character",
                "location",
                "item",
                "rule",
                "lore",
                "factions",
                "relationships",
            )
        )
        empty_world_auto_supplement = bool(
            not has_structure_work
            and has_chapter_work
            and world_baseline_state == "required"
            and world_material_count == 0
            and auto_creation_policy.enabled
        )
        if (
            not has_structure_work
            and world_baseline_state in {
                "required",
                "stale",
                "blocked_pending_decisions",
            }
        ):
            if empty_world_auto_supplement:
                issues.append(
                    _issue(
                        "empty_world_auto_supplement_requires_confirmation",
                        "warning_requires_ack",
                        details={
                            "allowed_card_types": list(
                                auto_creation_policy.allowed_card_types
                            )
                        },
                        action_codes=["review_reference_card_auto_creation"],
                    )
                )
            else:
                issues.append(
                    _issue(
                        "world_baseline_confirmation_required",
                        "blocked",
                        details={"state": world_baseline_state},
                        action_codes=["open_world_baseline"],
                    )
                )

        if structure_state == "missing":
            issues.append(
                _issue(
                    "book_structure_initialization_required",
                    "warning_requires_ack",
                    details={
                        "target_chapter_count": int(
                            structure_work.get("target_chapter_count") or 0
                        )
                    },
                    action_codes=["review_book_structure_initialization"],
                )
            )
        elif structure_state == "partial":
            issues.append(
                _issue(
                    "book_structure_partial",
                    "blocked",
                    action_codes=["review_book_structure"],
                )
            )
        elif structure_state == "deleted_conflict":
            issues.append(
                _issue(
                    "book_structure_in_trash",
                    "blocked",
                    details={
                        "deleted_volume_count": int(
                            (structure_snapshot or {}).get(
                                "deleted_volume_count"
                            )
                            or 0
                        ),
                        "deleted_chapter_count": int(
                            (structure_snapshot or {}).get(
                                "deleted_chapter_count"
                            )
                            or 0
                        ),
                    },
                    action_codes=["review_book_structure_trash"],
                )
            )
        elif structure_state == "invalid_target":
            issues.append(
                _issue(
                    "book_structure_target_invalid",
                    "blocked",
                    action_codes=["review_novel_blueprint"],
                )
            )
        elif structure_state == "provider_invalid":
            issues.append(
                _issue(
                    "provider_plan_invalid",
                    "blocked",
                    details={"step": "volume_outline"},
                    action_codes=["open_provider_settings"],
                )
            )

        structure_has_specific_blocker = structure_state in {
            "partial",
            "deleted_conflict",
            "invalid_target",
            "provider_invalid",
        }
        if not has_work and not structure_has_specific_blocker:
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

        prose_without_outline = [
            snapshot["chapter_id"]
            for snapshot in work["chapters"]
            if snapshot["has_content"] is True
            and snapshot["has_outline"] is False
            and snapshot["state_completion_status"]
            not in REUSABLE_STATE_COMPLETION_STATUSES
        ]
        if prose_without_outline:
            issues.append(
                _issue(
                    "existing_prose_without_outline_requires_manual_review",
                    "blocked",
                    details={
                        "chapter_count": len(prose_without_outline),
                        "chapter_ids": prose_without_outline[:50],
                    },
                    action_codes=["review_chapter_outline"],
                )
            )

        legacy_outline_prose = []
        unknown_outline_contracts = []
        for chapter in chapters:
            if str(chapter.get("content") or "").strip():
                continue
            outline = chapter.get("outline")
            if not isinstance(outline, Mapping) or not outline:
                continue
            chapter_id = str(chapter.get("_id") or "")
            try:
                contract_version = require_known_scene_contract_version(outline)
            except ValueError:
                unknown_outline_contracts.append(chapter_id)
                continue
            if contract_version != SCENE_TRANSITION_CONTRACT_VERSION:
                legacy_outline_prose.append(chapter_id)
        if legacy_outline_prose:
            issues.append(
                _issue(
                    "legacy_outline_requires_v2_regeneration",
                    "blocked",
                    details={
                        "chapter_count": len(legacy_outline_prose),
                        "chapter_ids": legacy_outline_prose[:50],
                    },
                    action_codes=["regenerate_chapter_outline"],
                )
            )
        if unknown_outline_contracts:
            issues.append(
                _issue(
                    "unknown_outline_contract_requires_manual_review",
                    "blocked",
                    details={
                        "chapter_count": len(unknown_outline_contracts),
                        "chapter_ids": unknown_outline_contracts[:50],
                    },
                    action_codes=["review_chapter_outline"],
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
        if has_chapter_work and world_count == 0:
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
            if has_chapter_work:
                planning = self._deps.plan_work(
                    chapters,
                    continuation_policy,
                    planning_generation_params,
                )
            elif has_structure_work:
                planning = {
                    **dict((structure_snapshot or {}).get("planning") or {}),
                    "book_structure_initialization": structure_snapshot,
                }
            else:
                planning = {
                    "attempt_capacity": 0,
                    "providers": [],
                    "config_revision": "",
                    "capability_snapshot": "",
                }
            if has_chapter_work:
                planning = {
                    **planning,
                    "narrative_quality_signal_authorization": (
                        build_narrative_quality_signal_authorization(chapters)
                    ),
                }
            finalization_authorization = (
                build_chapter_finalization_authorization(
                    authorization_revision=authorization_revision,
                )
            )

            if has_chapter_work and self._deps.plan_candidate_repairs is not None:
                candidate_repair_authorization = (
                    parse_candidate_repair_authorization(
                        self._deps.plan_candidate_repairs(
                            chapters,
                            authorization_revision,
                            int(
                                finalization_authorization[
                                    "max_repair_cycles"
                                ]
                            ),
                            generation_params,
                        )
                    )
                )
                repair_attempts = (
                    candidate_repair_authorization.maximum_provider_attempts_total
                )
                base_budget = _parse_base_generation_budget(
                    planning.get("base_generation_budget")
                )
                base_attempt_capacity = _strict_non_negative_budget_int(
                    planning.get("attempt_capacity"),
                    field="base generation attempt capacity",
                )
                if (
                    base_attempt_capacity
                    != base_budget.maximum_provider_attempts_total
                ):
                    raise ValueError("base generation attempt capacity changed")
                candidate_provider_bounds = tuple(
                    ProviderBudgetBound(
                        provider_alias=item.provider_alias,
                        paid_attempts=item.maximum_paid_attempts_total,
                        tokens=item.maximum_tokens_total,
                    )
                    for item in candidate_repair_authorization.provider_bounds
                )
                reference_repair_authorization = None
                reference_provider_bounds: tuple[ProviderBudgetBound, ...] = ()
                reference_repair_attempts = 0
                reference_repair_tokens = 0
                if auto_creation_policy.max_candidate_repair_cycles_per_chapter:
                    if self._deps.plan_reference_card_repairs is None:
                        raise ValueError(
                            "reference-card repair planner is unavailable"
                        )
                    reference_repair_authorization = (
                        parse_reference_card_repair_plan_authorization(
                            self._deps.plan_reference_card_repairs(
                                chapters,
                                auto_creation_policy.
                                max_candidate_repair_cycles_per_chapter,
                                generation_params,
                            )
                        )
                    )
                    reference_provider_bounds = tuple(
                        ProviderBudgetBound(
                            provider_alias=item.provider_alias,
                            paid_attempts=item.maximum_paid_attempts_total,
                            tokens=item.maximum_tokens_total,
                        )
                        for item in reference_repair_authorization.provider_bounds
                    )
                    reference_repair_attempts = (
                        reference_repair_authorization.
                        maximum_provider_attempts_total
                    )
                    reference_repair_tokens = (
                        reference_repair_authorization.maximum_tokens_total
                    )
                full_provider_bounds = merge_provider_bounds(
                    base_budget.provider_bounds,
                    candidate_provider_bounds,
                    reference_provider_bounds,
                )
                full_token_bound = (
                    base_budget.maximum_tokens_total
                    + candidate_repair_authorization.maximum_tokens_total
                    + reference_repair_tokens
                )
                full_attempt_bound = (
                    base_attempt_capacity
                    + repair_attempts
                    + reference_repair_attempts
                )
                planning = {
                    **planning,
                    "attempt_capacity": full_attempt_bound,
                    "providers": [
                        item.provider_alias for item in full_provider_bounds
                    ],
                    "chapter_candidate_pipeline_revision": (
                        CANDIDATE_PIPELINE_REVISION
                    ),
                    "chapter_candidate_repair_authorization": (
                        candidate_repair_authorization.model_dump(mode="json")
                    ),
                    **(
                        {
                            "reference_card_repair_plan_authorization": (
                                reference_repair_authorization.model_dump(
                                    mode="json"
                                )
                            )
                        }
                        if reference_repair_authorization is not None
                        else {}
                    ),
                    "batch_generation_budget_coverage": {
                        "schema_version": (
                            "batch_generation_budget_coverage.v1"
                        ),
                        "base_generation_maximum_tokens": (
                            base_budget.maximum_tokens_total
                        ),
                        "candidate_repair_maximum_tokens": (
                            candidate_repair_authorization.maximum_tokens_total
                        ),
                        "reference_card_repair_maximum_tokens": (
                            reference_repair_tokens
                        ),
                        "maximum_tokens_total": full_token_bound,
                        "maximum_provider_attempts_total": full_attempt_bound,
                        "provider_bounds": [
                            {
                                "provider_alias": item.provider_alias,
                                "maximum_paid_attempts_total": (
                                    item.paid_attempts
                                ),
                                "maximum_tokens_total": item.tokens,
                            }
                            for item in full_provider_bounds
                        ],
                        "token_bound_known": base_budget.token_bound_known,
                        "token_budget": token_budget,
                        "covers_full_job_authority": bool(
                            base_budget.token_bound_known
                            and token_budget is not None
                            and token_budget >= full_token_bound
                        ),
                    },
                }
                if not base_budget.token_bound_known:
                    issues.append(
                        _issue(
                            "batch_generation_token_bound_unproven",
                            "blocked",
                            action_codes=["review_provider_settings"],
                        )
                    )
                if full_token_bound > 0 and token_budget is None:
                    issues.append(
                        _issue(
                            "batch_generation_requires_token_budget",
                            "blocked",
                            details={
                                "maximum_tokens_total": full_token_bound
                            },
                            action_codes=["set_token_budget"],
                        )
                    )
                elif (
                    full_token_bound > 0
                    and token_budget is not None
                    and token_budget < full_token_bound
                ):
                    issues.append(
                        _issue(
                            "batch_generation_budget_may_pause",
                            "warning",
                            details={
                                "maximum_tokens_total": full_token_bound,
                                "token_budget": token_budget,
                            },
                            action_codes=["review_token_budget"],
                        )
                    )
                    if reference_repair_authorization is not None:
                        issues.append(
                            _issue(
                                "reference_card_repair_budget_not_covered",
                                "blocked",
                                details={
                                    "maximum_tokens_total": full_token_bound,
                                    "token_budget": token_budget,
                                },
                                action_codes=["set_token_budget"],
                            )
                        )
            elif has_structure_work:
                base_budget = _parse_base_generation_budget(
                    planning.get("base_generation_budget")
                )
                base_attempt_capacity = _strict_non_negative_budget_int(
                    planning.get("attempt_capacity"),
                    field="book structure attempt capacity",
                )
                if (
                    base_attempt_capacity
                    != base_budget.maximum_provider_attempts_total
                ):
                    raise ValueError("book structure attempt capacity changed")
                maximum_tokens_total = base_budget.maximum_tokens_total
                planning["batch_generation_budget_coverage"] = {
                    "schema_version": "batch_generation_budget_coverage.v1",
                    "base_generation_maximum_tokens": maximum_tokens_total,
                    "candidate_repair_maximum_tokens": 0,
                    "reference_card_repair_maximum_tokens": 0,
                    "maximum_tokens_total": maximum_tokens_total,
                    "maximum_provider_attempts_total": base_attempt_capacity,
                    "provider_bounds": [
                        {
                            "provider_alias": item.provider_alias,
                            "maximum_paid_attempts_total": item.paid_attempts,
                            "maximum_tokens_total": item.tokens,
                        }
                        for item in base_budget.provider_bounds
                    ],
                    "token_bound_known": base_budget.token_bound_known,
                    "token_budget": token_budget,
                    "covers_full_job_authority": bool(
                        base_budget.token_bound_known
                        and token_budget is not None
                        and token_budget >= maximum_tokens_total
                    ),
                }
                if not base_budget.token_bound_known:
                    issues.append(
                        _issue(
                            "batch_generation_token_bound_unproven",
                            "blocked",
                            action_codes=["review_provider_settings"],
                        )
                    )
                if maximum_tokens_total > 0 and token_budget is None:
                    issues.append(
                        _issue(
                            "batch_generation_requires_token_budget",
                            "blocked",
                            details={
                                "maximum_tokens_total": maximum_tokens_total
                            },
                            action_codes=["set_token_budget"],
                        )
                    )
                elif (
                    maximum_tokens_total > 0
                    and token_budget is not None
                    and token_budget < maximum_tokens_total
                ):
                    issues.append(
                        _issue(
                            "book_structure_budget_not_covered",
                            "blocked",
                            details={
                                "maximum_tokens_total": maximum_tokens_total,
                                "token_budget": token_budget,
                            },
                            action_codes=["review_token_budget"],
                        )
                    )
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
        raw_candidate_repair_authorization = planning.get(
            "chapter_candidate_repair_authorization"
        )
        finalization_repair_events = None
        if isinstance(raw_candidate_repair_authorization, Mapping):
            finalization_repair_events = parse_candidate_repair_authorization(
                raw_candidate_repair_authorization
            ).maximum_repair_events_per_chapter
        finalization_authorization = build_chapter_finalization_authorization(
            authorization_revision=authorization_revision,
            **(
                {"max_repair_cycles": finalization_repair_events}
                if finalization_repair_events is not None
                else {}
            ),
        )
        planning = {
            **planning,
            "prose_continuation_authorization": prose_authorization,
            "chapter_finalization_authorization": finalization_authorization,
            "empty_world_auto_supplement": empty_world_auto_supplement,
            "reference_card_auto_creation_policy": (
                auto_creation_policy.model_dump(mode="json")
            ),
        }
        if auto_creation_policy.enabled and has_chapter_work:
            raw_reference_repair = planning.get(
                "reference_card_repair_plan_authorization"
            )
            reference_repair_plan = (
                parse_reference_card_repair_plan_authorization(
                    raw_reference_repair
                )
                if auto_creation_policy.
                max_candidate_repair_cycles_per_chapter
                else None
            )
            auto_creation_authorization = (
                build_reference_card_creation_authorization(
                    owner_id=str(resources.get("owner_id") or ""),
                    novel_id=str(novel_id),
                    scope=scope,
                    volume_id=volume_id,
                    chapter_ids=(
                        str(item.get("chapter_id") or "")
                        for item in work.get("chapters") or []
                    ),
                    worklist_digest=_digest(
                        {
                            "scope": scope,
                            "volume_id": volume_id,
                            "chapters": list(work.get("chapters") or []),
                        }
                    ),
                    baseline_narrative_revision=int(
                        resources.get("narrative_revision") or 0
                    ),
                    authorization_revision=authorization_revision,
                    allowed_card_types=auto_creation_policy.allowed_card_types,
                    max_auto_creates_per_chapter=(
                        auto_creation_policy.max_auto_creates_per_chapter
                    ),
                    max_auto_creates_per_book=(
                        auto_creation_policy.max_auto_creates_per_book
                    ),
                    max_candidate_repair_cycles_per_chapter=(
                        auto_creation_policy.
                        max_candidate_repair_cycles_per_chapter
                    ),
                    repair_provider_bounds=(
                        [
                            item.model_dump(mode="json")
                            for item in reference_repair_plan.provider_bounds
                        ]
                        if reference_repair_plan is not None
                        else ()
                    ),
                    maximum_repair_provider_attempts_total=(
                        reference_repair_plan.maximum_provider_attempts_total
                        if reference_repair_plan is not None
                        else 0
                    ),
                    maximum_repair_tokens_total=(
                        reference_repair_plan.maximum_tokens_total
                        if reference_repair_plan is not None
                        else 0
                    ),
                )
            )
            planning["reference_card_creation_authorization"] = (
                auto_creation_authorization
            )
            issues.append(
                _issue(
                    "automatic_reference_card_creation_requires_confirmation",
                    "warning_requires_ack",
                    details={
                        "allowed_card_types": list(
                            auto_creation_policy.allowed_card_types
                        ),
                        "max_auto_creates_per_chapter": (
                            auto_creation_policy.max_auto_creates_per_chapter
                        ),
                        "max_auto_creates_per_book": (
                            auto_creation_policy.max_auto_creates_per_book
                        ),
                        "max_candidate_repair_cycles_per_chapter": (
                            auto_creation_policy.
                            max_candidate_repair_cycles_per_chapter
                        ),
                        "maximum_repair_provider_attempts_total": (
                            auto_creation_authorization[
                                "maximum_repair_provider_attempts_total"
                            ]
                        ),
                        "maximum_repair_tokens_total": (
                            auto_creation_authorization[
                                "maximum_repair_tokens_total"
                            ]
                        ),
                    },
                    action_codes=["review_reference_card_auto_creation"],
                )
            )
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
            "version": 2,
            "novel_id": str(novel_id),
            "scope": scope,
            "volume_id": str(volume_id) if volume_id else None,
            "outline_deviation_policy": outline_deviation_policy,
            "work": work,
            "resources": {
                "owner_id": str(resources.get("owner_id") or ""),
                **{
                    kind: int(resources.get(kind) or 0)
                    for kind in ("character", "location", "item", "rule", "lore")
                },
                "narrative_revision": int(
                    resources.get("narrative_revision") or 0
                ),
                "world_baseline_state": world_baseline_state,
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
        readiness_version = report.get("version")
        if (
            isinstance(readiness_version, bool)
            or not isinstance(readiness_version, int)
            or readiness_version != 2
        ):
            raise StaleReadiness("生成前检查版本无效，请重新检查后再启动")
        novel_id = report.get("novel_id")
        scope = report.get("scope")
        volume_id = report.get("volume_id")
        outline_deviation_policy = report.get("outline_deviation_policy")
        planning = report.get("planning")
        legacy_candidate_readiness = (
            isinstance(planning, Mapping)
            and "chapter_candidate_pipeline_revision" in planning
        )
        successor_readiness = False
        if isinstance(planning, Mapping):
            from backend.services.generation.required_book_successor import (
                required_book_successor_planning_present,
                validate_required_book_successor_readiness,
            )
            from backend.services.generation.required_chapter_finalization_job import (
                required_chapter_finalization_planning_present,
                validate_required_chapter_finalization_readiness,
            )
            from backend.services.generation.required_chapter_review_job import (
                required_chapter_review_planning_present,
                validate_required_chapter_review_readiness,
            )
            from backend.services.generation.required_chapter_state_job import (
                required_chapter_state_planning_present,
                validate_required_chapter_state_readiness,
            )

            successor_validator = None
            if required_book_successor_planning_present(planning):
                successor_validator = validate_required_book_successor_readiness
            elif required_chapter_finalization_planning_present(planning):
                successor_validator = (
                    validate_required_chapter_finalization_readiness
                )
            elif required_chapter_state_planning_present(planning):
                successor_validator = validate_required_chapter_state_readiness
            elif required_chapter_review_planning_present(planning):
                successor_validator = validate_required_chapter_review_readiness
            successor_readiness = successor_validator is not None
            if successor_readiness:
                try:
                    successor_validator(report)
                except ValueError as exc:
                    raise StaleReadiness(
                        "必需章节审查 readiness 已失效，请重新检查"
                    ) from exc
        candidate_readiness = (
            legacy_candidate_readiness or successor_readiness
        )
        resources = report.get("resources")
        owner_id = resources.get("owner_id") if isinstance(resources, Mapping) else None
        narrative_revision = (
            resources.get("narrative_revision")
            if isinstance(resources, Mapping)
            else None
        )
        work = report.get("work")
        raw_chapters = work.get("chapters") if isinstance(work, Mapping) else None
        if candidate_readiness:
            if not isinstance(novel_id, str) or not novel_id:
                raise StaleReadiness("生成前检查小说范围无效，请重新检查")
            if scope not in {"volume", "book"}:
                raise StaleReadiness("生成前检查作业范围无效，请重新检查")
            if outline_deviation_policy not in {
                "pause_for_rewrite",
                "accept_and_continue",
            }:
                raise StaleReadiness("生成前检查偏纲处置权限无效，请重新检查")
            if (
                (
                    scope == "volume"
                    and (not isinstance(volume_id, str) or not volume_id)
                )
                or (scope == "book" and volume_id is not None)
            ):
                raise StaleReadiness("生成前检查卷范围无效，请重新检查")
            if (
                not isinstance(owner_id, str)
                or not owner_id
                or type(narrative_revision) is not int
                or narrative_revision < 0
            ):
                raise StaleReadiness("生成前检查资源范围无效，请重新检查")
            if not isinstance(raw_chapters, list) or any(
                not isinstance(item, Mapping)
                or not isinstance(item.get("chapter_id"), str)
                or not item.get("chapter_id")
                or not isinstance(item.get("volume_id"), str)
                or not item.get("volume_id")
                for item in raw_chapters
            ):
                raise StaleReadiness("生成前检查章节范围无效，请重新检查")
        current_digest = str(report.get("digest") or "")
        automatic_confirmation_required = any(
            item.get("code") == "automatic_continuations_require_confirmation"
            for item in report.get("issues", [])
        )
        auto_creation_confirmation_required = any(
            item.get("code")
            == "automatic_reference_card_creation_requires_confirmation"
            for item in report.get("issues", [])
        )
        if auto_creation_confirmation_required and supplied_digest is None:
            raise StaleReadiness(
                "自动建卡必须使用当前 readiness 摘要确认后才能启动"
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

        authorized = {
            "version": 2,
            "novel_id": novel_id,
            "scope": scope,
            "volume_id": volume_id,
            "outline_deviation_policy": outline_deviation_policy,
            "digest": current_digest,
            "acknowledged_warning_codes": acknowledged,
            "issues": list(report.get("issues") or []),
            "work": report.get("work") or {},
            "resources": report.get("resources") or {},
            "planning": report.get("planning") or {},
        }
        if "source_binding" in report:
            authorized["source_binding"] = deepcopy(
                report.get("source_binding")
            )
        return authorized


async def _load_resource_counts(novel_id: str) -> dict[str, Any]:
    from backend.db.narrative_revision import narrative_revision_store
    from backend.db.repositories.novel_repository import novel_repo
    from backend.services.novel.world_baseline import WorldBaselineService

    novel = await novel_repo.get_novel_by_id(novel_id)
    world_baseline = await WorldBaselineService.inspect(novel_id)
    material_counts = dict(world_baseline.get("counts") or {})
    result = {
        "owner_id": str(novel.get("owner_id") or ""),
        **{
            key: int(material_counts.get(key) or 0)
            for key in (
                "character",
                "location",
                "item",
                "rule",
                "lore",
                "factions",
                "relationships",
            )
        },
        "narrative_revision": await narrative_revision_store.current(novel_id),
        "world_baseline_state": world_baseline["state"],
    }
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


def _base_structured_generation_budget(
    chapters: list[dict[str, Any]],
    generation_params: Mapping[str, Any] | None,
    *,
    runtime: Any | None = None,
) -> _BaseGenerationBudget:
    from backend.services.generation.headless_generation import (
        CHAPTER_OUTLINE_STEP,
        CHAPTER_OUTLINE_WORKFLOW,
        OUTLINE_ADHERENCE_STEP,
        PROSE_REMEDIATION_WORKFLOW,
        STATE_STEP,
        STATE_WORKFLOW,
    )
    from backend.services.llm.generation_runtime import (
        WorkflowStepTarget,
        create_generation_runtime,
    )

    values = dict(generation_params or {})
    runtime_kwargs = (
        {} if values.get("allow_failure_retry", True)
        else {"max_provider_retries": 0}
    )
    if runtime is None:
        runtime = create_generation_runtime(**runtime_kwargs)
    outline_count = sum(not chapter.get("outline") for chapter in chapters)
    state_count = sum(
        str((chapter.get("state_completion") or {}).get("status") or "missing")
        not in REUSABLE_STATE_COMPLETION_STATUSES
        for chapter in chapters
    )
    raw_output_bound = values.get("max_tokens")
    calls: list[tuple[Any, int]] = []
    if outline_count:
        calls.append((
            runtime.plan_structured(
                WorkflowStepTarget(
                    CHAPTER_OUTLINE_WORKFLOW,
                    CHAPTER_OUTLINE_STEP,
                )
            ),
            outline_count,
        ))
    if state_count:
        calls.extend((
            (
                runtime.plan_structured(
                    WorkflowStepTarget(
                        PROSE_REMEDIATION_WORKFLOW,
                        OUTLINE_ADHERENCE_STEP,
                    )
                ),
                state_count * 2,
            ),
            (
                runtime.plan_structured(
                    WorkflowStepTarget(STATE_WORKFLOW, STATE_STEP)
                ),
                state_count,
            ),
        ))
    provider_bounds: tuple[ProviderBudgetBound, ...] = ()
    maximum_attempts = 0
    maximum_input_tokens = 0
    maximum_output_tokens = 0
    maximum_tokens = 0
    for plan, multiplier in calls:
        call_budget = structured_call_budget(
            plan,
            output_token_bound=raw_output_bound,
        )
        provider_bounds = merge_provider_bounds(
            provider_bounds,
            scale_provider_bounds(call_budget.provider_bounds, multiplier),
        )
        maximum_attempts += call_budget.max_paid_attempts * multiplier
        maximum_input_tokens += (
            call_budget.max_input_tokens_per_attempt
            * call_budget.max_paid_attempts
            * multiplier
        )
        maximum_output_tokens += (
            call_budget.max_output_tokens_per_attempt
            * call_budget.max_paid_attempts
            * multiplier
        )
        maximum_tokens += call_budget.max_tokens_per_call * multiplier
    return _BaseGenerationBudget(
        maximum_provider_attempts_total=maximum_attempts,
        maximum_tokens_total=maximum_tokens,
        token_bound_known=True,
        provider_bounds=provider_bounds,
        maximum_input_tokens_total=maximum_input_tokens,
        maximum_output_tokens_total=maximum_output_tokens,
    )


def _plan_work(
    chapters: list[dict[str, Any]],
    generation_params: Mapping[str, Any] | None = None,
    *,
    runtime: Any | None = None,
) -> dict[str, Any]:
    from backend.services.generation.headless_generation import (
        CHAPTER_OUTLINE_STEP,
        CHAPTER_OUTLINE_WORKFLOW,
        OUTLINE_ADHERENCE_STEP,
        PROSE_REMEDIATION_WORKFLOW,
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

    reuse_runtime = runtime is not None
    if runtime is None:
        runtime = create_generation_runtime()
    plans = []
    candidate_chapters = [
        chapter for chapter in chapters if not _has_text(chapter, "content")
    ]
    outline_plan = None
    if any(not chapter.get("outline") for chapter in chapters):
        outline_plan = runtime.plan_structured(
            WorkflowStepTarget(CHAPTER_OUTLINE_WORKFLOW, CHAPTER_OUTLINE_STEP)
        )
        plans.append(outline_plan)
    prose_text_plan = None
    if any(not _has_text(chapter, "content") for chapter in chapters):
        prose_text_plan = runtime.plan_text(
            WorkflowStepTarget(PROSE_WORKFLOW, PROSE_STEP)
        )
        plans.append(prose_text_plan)
    state_plan = None
    if any(
        str((chapter.get("state_completion") or {}).get("status") or "missing")
        not in REUSABLE_STATE_COMPLETION_STATUSES
        for chapter in chapters
    ):
        state_plan = runtime.plan_structured(
            WorkflowStepTarget(STATE_WORKFLOW, STATE_STEP)
        )
        plans.append(state_plan)
    adherence_plan = (
        runtime.plan_structured(
            WorkflowStepTarget(
                PROSE_REMEDIATION_WORKFLOW,
                OUTLINE_ADHERENCE_STEP,
            )
        )
        if candidate_chapters
        else None
    )
    if adherence_plan is not None:
        plans.append(adherence_plan)
    if candidate_chapters and state_plan is None:
        state_plan = runtime.plan_structured(
            WorkflowStepTarget(STATE_WORKFLOW, STATE_STEP)
        )
        plans.append(state_plan)
    revisions = sorted({plan.config_revision for plan in plans})
    capabilities = sorted({plan.capability_snapshot for plan in plans})
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
                maximum_prose_calls += maximum_v2_chapter_base_calls(
                    safe_output_budget=capability_plan.safe_output_budget,
                )
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
        "attempt_capacity": estimate_worklist_attempt_capacity(
            chapters,
            generation_params,
            **({"runtime": runtime} if reuse_runtime else {}),
        ),
        "providers": sorted({plan.provider_alias for plan in plans}),
        "config_revision": "|".join(revisions),
        "capability_snapshot": "|".join(capabilities),
        "chapter_candidate_job_execution_authorization": (
            build_candidate_job_execution_authorization(
                chapters,
                generation_params,
                plans=CandidateJobGenerationPlans(
                    outline=(
                        outline_plan
                        if any(
                            not chapter.get("outline")
                            for chapter in candidate_chapters
                        )
                        else None
                    ),
                    prose=prose_text_plan,
                    adherence=adherence_plan,
                    state=state_plan if candidate_chapters else None,
                ),
            )
        ),
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
    *,
    runtime: Any | None = None,
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

    reuse_runtime = runtime is not None
    base = _plan_work(
        chapters,
        generation_params,
        **({"runtime": runtime} if reuse_runtime else {}),
    )
    values = dict(generation_params or {})
    structured_budget = _base_structured_generation_budget(
        chapters,
        values,
        **({"runtime": runtime} if reuse_runtime else {}),
    )
    chapters_needing_prose = [
        chapter for chapter in chapters if not _has_text(chapter, "content")
    ]
    strategy = dict(base.get("prose_strategy") or {})
    if not chapters_needing_prose:
        attempt_capacity = estimate_worklist_attempt_capacity(
            chapters,
            values,
            **({"runtime": runtime} if reuse_runtime else {}),
        )
        return {
            **base,
            "attempt_capacity": attempt_capacity,
            "providers": [
                item.provider_alias
                for item in structured_budget.provider_bounds
            ],
            "base_generation_budget": structured_budget.to_dict(),
            "base_generation_token_upper_bound": (
                structured_budget.token_upper_bound()
            ),
            "generation_params_digest": _digest(values),
            "prose_strategy": {
                **strategy,
                "maximum_base_prose_calls": 0,
                "estimated_prose_chapter_count": 0,
                "estimated_scene_count": 0,
                "maximum_automatic_continuation_calls": 0,
                "maximum_logical_prose_calls": 0,
                "max_actual_provider_attempts": 0,
                "maximum_base_call_output_capacity_words": 0,
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
    if runtime is None:
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
    maximum_base_call_output_capacity_words = 1
    for chapter in chapters_needing_prose:
        outline = chapter.get("outline") or {}
        if not outline:
            unknown_outline_chapters += 1
            maximum_base_call_output_capacity_words = max(
                maximum_base_call_output_capacity_words,
                v2_scene_base_call_safe_output_budget(
                    capability_plan.safe_output_budget
                ),
            )
            maximum_base_calls += maximum_v2_chapter_base_calls(
                safe_output_budget=capability_plan.safe_output_budget,
            )
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
        maximum_base_calls += chapter_plan.maximum_base_call_count
        estimated_scene_count += chapter_plan.scene_count
        maximum_base_call_output_capacity_words = max(
            maximum_base_call_output_capacity_words,
            *planned_base_call_output_capacity_words(chapter_plan),
        )

    inherited_max_tokens = values.get("max_tokens")
    base_output_token_bound = v3_output_token_bound(
        target_words=maximum_base_call_output_capacity_words,
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
    prose_token_bound_known = conservative_token_bound > 0
    prose_provider_bound = ProviderBudgetBound(
        provider_alias=str(prose_plan.provider_alias or ""),
        paid_attempts=maximum_logical_calls,
        tokens=(
            conservative_total_token_bound
            if prose_token_bound_known
            else 0
        ),
    )
    combined_provider_bounds = merge_provider_bounds(
        structured_budget.provider_bounds,
        (prose_provider_bound,),
    )
    base_generation_budget = _BaseGenerationBudget(
        maximum_provider_attempts_total=(
            structured_budget.maximum_provider_attempts_total
            + maximum_logical_calls
        ),
        maximum_tokens_total=(
            structured_budget.maximum_tokens_total
            + conservative_total_token_bound
        ),
        token_bound_known=(
            structured_budget.token_bound_known and prose_token_bound_known
        ),
        provider_bounds=combined_provider_bounds,
        maximum_input_tokens_total=(
            (
                structured_budget.maximum_input_tokens_total
                + maximum_base_calls * base_prompt_input_bound
                + maximum_automatic_calls * continuation_prompt_input_bound
            )
            if structured_budget.maximum_input_tokens_total is not None
            and prose_token_bound_known
            else None
        ),
        maximum_output_tokens_total=(
            (
                structured_budget.maximum_output_tokens_total
                + maximum_base_calls * base_output_token_bound
                + maximum_automatic_calls * continuation_output_token_bound
            )
            if structured_budget.maximum_output_tokens_total is not None
            and prose_token_bound_known
            else None
        ),
    )
    attempt_capacity = estimate_worklist_attempt_capacity(
        chapters,
        values,
        **({"runtime": runtime} if reuse_runtime else {}),
    )
    maximum_call_output_capacity_words = max(
        maximum_base_call_output_capacity_words,
        policy.continuation_target_words,
    )
    return {
        **base,
        "attempt_capacity": attempt_capacity,
        "providers": [
            item.provider_alias for item in combined_provider_bounds
        ],
        "base_generation_budget": base_generation_budget.to_dict(),
        "base_generation_token_upper_bound": (
            base_generation_budget.token_upper_bound()
        ),
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
            "maximum_base_call_output_capacity_words": (
                maximum_base_call_output_capacity_words
            ),
            "continuation_call_target_words": policy.continuation_target_words,
            "maximum_call_output_capacity_words": (
                maximum_call_output_capacity_words
            ),
            "base_output_token_bound": base_output_token_bound,
            "continuation_output_token_bound": continuation_output_token_bound,
            "conservative_base_token_bound": conservative_base_token_bound,
            "conservative_continuation_token_bound": conservative_continuation_token_bound,
            "conservative_token_bound": conservative_token_bound,
            "conservative_total_token_bound": conservative_total_token_bound,
            "token_bound_known": prose_token_bound_known,
        },
    }


generation_readiness_module = GenerationReadinessModule(
    ReadinessDeps(
        load_resource_counts=_load_resource_counts,
        inspect_active_proposal=_inspect_active_proposal,
        plan_work=_plan_work_with_prose_continuation,
        prepare_generation_params=_prepare_generation_params,
        plan_candidate_repairs=build_chapter_candidate_repair_authorization,
        plan_reference_card_repairs=(
            build_reference_card_repair_plan_authorization
        ),
    )
)
