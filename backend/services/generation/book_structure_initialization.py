"""Bounded auto-book bootstrap for novels without volume/chapter structure.

This is deliberately a prerequisite operation, not a hidden chapter Job.  It
performs one explicitly authorized volume-outline workflow, commits the result
through ``VolumeService.accept_volume_outline`` and then hands the user to the
world-baseline gate before prose authorization can be created.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from typing import Any
from uuid import uuid4
from backend.novel_scale import MIN_CHAPTERS, MAX_CHAPTERS

from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db.repositories.volume_repository import volume_repo
from backend.db.mutation import MutationConflictError
from backend.llm.models import TokenUsage
from backend.llm.prompts.prompt_selector import (
    VOLUME_OUTLINE_PROMPT_NAME,
    load_prompt_config,
)
from backend.services.generation.provider_budget import structured_call_budget
from backend.services.generation.protected_generation_params import (
    validate_protected_generation_params,
)
from backend.services.generation.volume_outline_generation import (
    VOLUME_OUTLINE_STEP,
    VOLUME_OUTLINE_STEPS,
    VOLUME_OUTLINE_WORKFLOW,
    volume_outline_params,
)
from backend.services.llm.generation_runtime import (
    AttemptUsage,
    GenerationPlan,
    WorkflowStepTarget,
    create_generation_runtime,
    create_workflow_runtime,
)
from backend.services.llm.workflow_runner import (
    WorkflowDeps,
    run_workflow,
    run_workflow_to_result,
)
from backend.services.novel.volume_service import VolumeService


BOOK_STRUCTURE_INITIALIZATION_SCHEMA = "book_structure_initialization.v1"
BOOK_STRUCTURE_RESULT_SCHEMA = "book_structure_initialization_result.v1"
_GENERATION_OVERRIDE_KEYS = frozenset({
    "temperature",
    "top_p",
    "max_tokens",
    "presence_penalty",
    "frequency_penalty",
})


class BookStructureInitializationStale(ValueError):
    """The novel or structure changed after the user reviewed readiness."""


class BookStructureInitializationFailed(RuntimeError):
    """The bounded volume-outline workflow did not produce an applicable result."""


class BookStructureBudgetBoundary(ValueError):
    """A Provider request was rejected before dispatch by the fixed budget."""

    provider_request_not_dispatched = True


class _FixedBudgetAttemptScope:
    """In-memory atomic ceiling for this single explicitly confirmed workflow."""

    def __init__(self, *, maximum_attempts: int, token_budget: int) -> None:
        self.maximum_attempts = int(maximum_attempts)
        self.token_budget = int(token_budget)
        self._lock = asyncio.Lock()
        self._claims: dict[str, tuple[str, str]] = {}
        self._reservations: dict[str, int] = {}
        self._attempts: dict[str, AttemptUsage] = {}
        self._uncertain: set[str] = set()
        self._consumed_tokens = 0

    @property
    def attempts(self) -> tuple[AttemptUsage, ...]:
        return tuple(self._attempts.values())

    @property
    def claimed_attempt_ids(self) -> tuple[str, ...]:
        return tuple(self._claims)

    @property
    def uncertain_attempt_ids(self) -> tuple[str, ...]:
        return tuple(self._uncertain)

    async def claim(self, provider_alias: str, phase: str) -> str:
        return await self.claim_with_budget(provider_alias, phase, None)

    async def claim_with_budget(
        self,
        provider_alias: str,
        phase: str,
        conservative_tokens: int | None,
    ) -> str:
        if (
            conservative_tokens is None
            or isinstance(conservative_tokens, bool)
            or int(conservative_tokens) <= 0
        ):
            raise BookStructureBudgetBoundary(
                "卷章结构生成缺少可证明的 token 上界"
            )
        bound = int(conservative_tokens)
        async with self._lock:
            if len(self._claims) >= self.maximum_attempts:
                raise BookStructureBudgetBoundary(
                    "卷章结构生成调用次数已达到授权上限"
                )
            reserved = sum(self._reservations.values())
            if self._consumed_tokens + reserved + bound > self.token_budget:
                raise BookStructureBudgetBoundary(
                    "卷章结构生成会超过已确认的 token 硬预算"
                )
            attempt_id = uuid4().hex
            self._claims[attempt_id] = (str(provider_alias), str(phase))
            self._reservations[attempt_id] = bound
            return attempt_id

    async def account(self, attempt_id: str, usage: TokenUsage) -> None:
        async with self._lock:
            if attempt_id in self._attempts:
                return
            provider_alias, phase = self._claims[attempt_id]
            reserved = self._reservations.pop(attempt_id, 0)
            observed = max(
                int(usage.total_tokens or 0),
                int(usage.input_tokens or 0) + int(usage.output_tokens or 0),
            )
            accounted = observed if observed > 0 else reserved
            self._consumed_tokens += accounted
            self._attempts[attempt_id] = AttemptUsage(
                attempt_id=attempt_id,
                provider_alias=provider_alias,
                phase=phase,
                usage=TokenUsage(
                    input_tokens=max(0, int(usage.input_tokens or 0)),
                    output_tokens=max(0, int(usage.output_tokens or 0)),
                    total_tokens=accounted,
                ),
            )

    async def mark_uncertain(self, attempt_id: str, reason: str) -> None:
        del reason
        async with self._lock:
            if attempt_id in self._uncertain:
                return
            provider_alias, phase = self._claims[attempt_id]
            reserved = self._reservations.pop(attempt_id, 0)
            self._consumed_tokens += reserved
            self._uncertain.add(attempt_id)
            self._attempts[attempt_id] = AttemptUsage(
                attempt_id=attempt_id,
                provider_alias=provider_alias,
                phase=phase,
                usage=TokenUsage(total_tokens=reserved),
                state="uncertain",
            )

    async def release_pre_dispatch(self, attempt_id: str, reason: str) -> None:
        del reason
        async with self._lock:
            self._reservations.pop(attempt_id, None)
            self._claims.pop(attempt_id, None)


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _generation_overrides(
    generation_params: Mapping[str, Any] | None,
) -> dict[str, Any]:
    protected = validate_protected_generation_params(generation_params)
    return {
        key: value
        for key, value in protected.items()
        if key in _GENERATION_OVERRIDE_KEYS and value is not None
    }


def _plan_projection(plan: GenerationPlan) -> dict[str, Any]:
    target = plan.target
    if (
        not isinstance(target, WorkflowStepTarget)
        or target.workflow_name != VOLUME_OUTLINE_WORKFLOW
        or target.step_name != VOLUME_OUTLINE_STEP
    ):
        raise ValueError("卷章结构 Provider 计划目标已变化")
    return {
        "workflow": target.workflow_name,
        "step": target.step_name,
        "provider_alias": plan.provider_alias,
        "provider_model": plan.provider_model,
        "mode": plan.mode.value,
        "reviewer_alias": plan.reviewer_alias,
        "timeout_seconds": plan.timeout_seconds,
        "config_revision": plan.config_revision,
        "capability_snapshot": plan.capability_snapshot,
        "max_semantic_attempts": plan.max_semantic_attempts,
        "max_output_tokens": plan.max_output_tokens,
        "max_context_tokens": plan.max_context_tokens,
        "thinking_mode": plan.thinking_mode,
    }


async def _structure_state(novel_id: str) -> dict[str, Any]:
    active_volumes = await volume_repo.get_volumes_by_novel(novel_id)
    active_chapters = await chapter_repo.get_chapters_by_novel(
        novel_id,
        include_content=False,
    )
    deleted_volumes = await volume_repo.get_deleted_volumes_by_novel(novel_id)
    deleted_chapters = [
        chapter
        for chapter in await chapter_repo.get_chapters_by_novel(
            novel_id,
            include_content=False,
            include_deleted=True,
        )
        if chapter.get("is_deleted") is True
    ]
    return {
        "active_volume_ids": [str(item["_id"]) for item in active_volumes],
        "active_chapter_ids": [str(item["_id"]) for item in active_chapters],
        "deleted_volume_ids": [str(item["_id"]) for item in deleted_volumes],
        "deleted_chapter_ids": [str(item["_id"]) for item in deleted_chapters],
    }


def _base_snapshot(
    *,
    state: str,
    structure: Mapping[str, Any],
    target_chapter_count: int,
) -> dict[str, Any]:
    return {
        "schema_version": BOOK_STRUCTURE_INITIALIZATION_SCHEMA,
        "state": state,
        "target_chapter_count": target_chapter_count,
        "current_volume_count": len(structure["active_volume_ids"]),
        "current_chapter_count": len(structure["active_chapter_ids"]),
        "deleted_volume_count": len(structure["deleted_volume_ids"]),
        "deleted_chapter_count": len(structure["deleted_chapter_ids"]),
        "work": {
            "generate": 0,
            "reuse": 1 if state == "present" else 0,
            "target_chapter_count": target_chapter_count,
        },
        "planning": {
            "attempt_capacity": 0,
            "providers": [],
            "config_revision": "",
            "capability_snapshot": "",
            "base_generation_budget": {
                "schema_version": "base_generation_budget.v1",
                "maximum_provider_attempts_total": 0,
                "maximum_tokens_total": 0,
                "token_bound_known": False,
                "provider_bounds": [],
            },
        },
    }


async def inspect_book_structure_initialization(
    novel_id: str,
    *,
    generation_params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Inspect whether auto-book may safely create the initial structure."""

    novel = await novel_repo.get_novel_by_id(novel_id)
    structure = await _structure_state(novel_id)
    active_volumes = len(structure["active_volume_ids"])
    active_chapters = len(structure["active_chapter_ids"])
    deleted = bool(
        structure["deleted_volume_ids"] or structure["deleted_chapter_ids"]
    )
    raw_target = novel.get("number_of_chapters")
    target = (
        int(raw_target)
        if type(raw_target) is int
        else 0
    )

    if active_volumes and active_chapters:
        return _base_snapshot(
            state="present",
            structure=structure,
            target_chapter_count=target,
        )
    if active_volumes or active_chapters:
        return _base_snapshot(
            state="partial",
            structure=structure,
            target_chapter_count=target,
        )
    if deleted:
        return _base_snapshot(
            state="deleted_conflict",
            structure=structure,
            target_chapter_count=target,
        )
    if not MIN_CHAPTERS <= target <= MAX_CHAPTERS:
        return _base_snapshot(
            state="invalid_target",
            structure=structure,
            target_chapter_count=target,
        )

    overrides = _generation_overrides(generation_params)
    source = {
        "novel_id": str(novel_id),
        "params": volume_outline_params(novel),
        "generation_params": overrides,
        "prompt_revision": "",
        "narrative_revision": int(novel.get("narrative_revision") or 0),
        "structure": structure,
    }
    try:
        prompts = dict(
            load_prompt_config().get(VOLUME_OUTLINE_PROMPT_NAME, {})
        )
        source["prompt_revision"] = _digest(prompts)
        runtime = create_generation_runtime(max_provider_retries=0)
        plan = runtime.plan_structured(
            WorkflowStepTarget(
                VOLUME_OUTLINE_WORKFLOW,
                VOLUME_OUTLINE_STEP,
            )
        )
        output_bound = overrides.get("max_tokens") or plan.max_output_tokens
        budget = structured_call_budget(
            plan,
            output_token_bound=output_bound,
        )
        projection = _plan_projection(plan)
    except Exception as exc:
        snapshot = _base_snapshot(
            state="provider_invalid",
            structure=structure,
            target_chapter_count=target,
        )
        del exc
        snapshot["source_digest"] = _digest(source)
        return snapshot

    snapshot = _base_snapshot(
        state="missing",
        structure=structure,
        target_chapter_count=target,
    )
    snapshot["source_digest"] = _digest(source)
    snapshot["prompt_revision"] = source["prompt_revision"]
    snapshot["expected_narrative_revision"] = source[
        "narrative_revision"
    ]
    snapshot["provider_plan"] = projection
    snapshot["work"] = {
        "generate": 1,
        "reuse": 0,
        "target_chapter_count": target,
    }
    snapshot["planning"] = {
        "attempt_capacity": budget.max_paid_attempts,
        "providers": [item.provider_alias for item in budget.provider_bounds],
        "config_revision": str(plan.config_revision or ""),
        "capability_snapshot": str(plan.capability_snapshot or ""),
        "base_generation_budget": {
            "schema_version": "base_generation_budget.v1",
            "maximum_provider_attempts_total": budget.max_paid_attempts,
            "maximum_tokens_total": budget.max_tokens_per_call,
            "token_bound_known": True,
            "provider_bounds": [
                {
                    "provider_alias": item.provider_alias,
                    "maximum_paid_attempts_total": item.paid_attempts,
                    "maximum_tokens_total": item.tokens,
                }
                for item in budget.provider_bounds
            ],
        },
    }
    return snapshot


async def initialize_book_structure(
    novel_id: str,
    *,
    authorization: Mapping[str, Any],
    token_budget: int,
    generation_params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute exactly the structure plan represented by signed readiness."""

    live = await inspect_book_structure_initialization(
        novel_id,
        generation_params=generation_params,
    )
    if dict(authorization) != live or live.get("state") != "missing":
        raise BookStructureInitializationStale(
            "卷章结构或 Provider 计划已变化，请重新预检"
        )
    planning = live.get("planning")
    if not isinstance(planning, Mapping):
        raise BookStructureInitializationStale("卷章结构授权计划无效")
    attempt_capacity = planning.get("attempt_capacity")
    if type(attempt_capacity) is not int or attempt_capacity < 1:
        raise BookStructureInitializationStale("卷章结构授权调用上限无效")

    expected_narrative_revision = live.get("expected_narrative_revision")
    if (
        isinstance(expected_narrative_revision, bool)
        or not isinstance(expected_narrative_revision, int)
        or expected_narrative_revision < 0
    ):
        raise BookStructureInitializationStale(
            "卷章结构授权的叙事版本无效，请重新预检"
        )
    try:
        novel = await novel_repo.get_novel_by_id(novel_id)
        prompts = dict(
            load_prompt_config().get(VOLUME_OUTLINE_PROMPT_NAME, {})
        )
        overrides = _generation_overrides(generation_params)
        planning_runtime = create_generation_runtime(max_provider_retries=0)
        plan = planning_runtime.plan_structured(
            WorkflowStepTarget(VOLUME_OUTLINE_WORKFLOW, VOLUME_OUTLINE_STEP)
        )
    except Exception as exc:
        raise BookStructureInitializationStale(
            "卷章结构 Provider 或提示词计划已变化，请重新预检"
        ) from exc
    if (
        _plan_projection(plan) != live.get("provider_plan")
        or _digest(prompts) != live.get("prompt_revision")
    ):
        raise BookStructureInitializationStale(
            "卷章结构 Provider 或提示词计划已变化，请重新预检"
        )

    scope = _FixedBudgetAttemptScope(
        maximum_attempts=attempt_capacity,
        token_budget=token_budget,
    )
    runtime = create_workflow_runtime(
        attempt_scope=scope,
        max_provider_retries=0,
    )
    try:
        generated, total_tokens = await run_workflow_to_result(
            VOLUME_OUTLINE_STEP,
            run_workflow(
                workflow_name=VOLUME_OUTLINE_WORKFLOW,
                steps=VOLUME_OUTLINE_STEPS,
                prompts=prompts,
                params=volume_outline_params(novel),
                gen_kwargs=overrides,
                cached={},
                deps=WorkflowDeps(
                    runtime=runtime,
                    structured_plans={VOLUME_OUTLINE_STEP: plan},
                ),
                request_id=uuid4().hex[:8],
                is_disconnected=None,
                log_partial_on_disconnect=False,
            ),
        )
    except BookStructureBudgetBoundary:
        raise
    except Exception as exc:
        raise BookStructureInitializationFailed(
            "卷章结构生成失败，未写入任何卷或章节"
        ) from exc

    current = await inspect_book_structure_initialization(
        novel_id,
        generation_params=generation_params,
    )
    if (
        current.get("state") != "missing"
        or current.get("source_digest") != live.get("source_digest")
    ):
        raise BookStructureInitializationStale(
            "生成期间小说或卷章结构发生变化，结果未写入"
        )
    volumes = generated.get("volumes") if isinstance(generated, Mapping) else None
    if not isinstance(volumes, list) or not volumes:
        raise BookStructureInitializationFailed(
            "卷章结构结果无效，未写入任何卷或章节"
        )
    try:
        accepted = await VolumeService.accept_volume_outline(
            novel_id,
            volumes,
            expected_narrative_revision=expected_narrative_revision,
        )
    except MutationConflictError as exc:
        raise BookStructureInitializationStale(
            "写入前小说或卷章结构已变化，结果未写入"
        ) from exc
    except Exception as exc:
        raise BookStructureInitializationFailed(
            "卷章结构未能完整写入，请重新检查当前结构后再继续"
        ) from exc
    return {
        "schema_version": BOOK_STRUCTURE_RESULT_SCHEMA,
        "volume_count": int(accepted["volume_count"]),
        "chapter_count": int(accepted["chapter_count"]),
        "volume_ids": list(accepted.get("volume_ids") or []),
        "next_route": accepted.get("next_route"),
        "usage": {
            "attempt_count": len(scope.attempts),
            "total_tokens": max(
                int(total_tokens),
                sum(
                    int(item.usage.total_tokens or 0)
                    for item in scope.attempts
                ),
            ),
        },
    }
