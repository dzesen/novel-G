"""Deterministic scene-local repair planning and assembly for V2 prose.

The Provider is allowed to rewrite only the requested scenes.  This module
owns the seam: it proves where every current scene came from, prepares bounded
adjacent context, preserves untargeted scenes byte-for-byte, and rebuilds the
whole-candidate scene proof after replacements are returned.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.scene_contract_versions import (
    SCENE_TRANSITION_CONTRACT_VERSION,
    require_known_scene_contract_version,
)
from backend.services.generation.prose_completion import ProseExecutionPlan
from backend.services.generation.scene_word_budget import (
    SCENE_WORD_BUDGET_TERMINAL_REASONS,
    trim_scene_contribution_to_word_budget,
)
from backend.services.novel.chapter_service import count_chapter_words
from backend.services.novel.state_completion import chapter_content_digest


SCENE_REPAIR_BOUNDARY_CONTEXT_CHARACTERS = 2_000
MAX_SCENE_REPAIR_PROSE_CHARACTERS = 80_000
_SCENE_PROGRESS_STATUSES = frozenset({
    "pending",
    "incomplete",
    "paused",
    "complete",
})
PROSE_CHECKPOINT_BLOCK_REASON_CODES = (
    "checkpoint_no_target_failure_resolved",
    "checkpoint_source_failure_scope_mismatch",
    "checkpoint_failure_scope_not_reduced",
    "checkpoint_new_scene_failure",
    "checkpoint_unsupported_scene_failure_reason",
    "checkpoint_content_unchanged",
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SceneContractValidationEntry(_StrictModel):
    scene_id: str = Field(min_length=1, max_length=100)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    word_count: int = Field(ge=0)
    provider_word_count: int = Field(ge=0)
    discarded_word_count: int = Field(default=0, ge=0)
    normalization_boundary: Literal["sentence", "word"] | None = None
    min: int = Field(ge=1)
    target: int = Field(ge=1)
    max: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_bounds_and_budget(self) -> "SceneContractValidationEntry":
        if self.end < self.start:
            raise ValueError("scene contract validation span is reversed")
        if not self.min <= self.target <= self.max:
            raise ValueError("scene contract validation budget is invalid")
        if self.provider_word_count < self.word_count:
            raise ValueError("provider word count cannot be below retained count")
        if (
            self.discarded_word_count
            != self.provider_word_count - self.word_count
        ):
            raise ValueError("discarded word count does not match retained count")
        if bool(self.discarded_word_count) != bool(
            self.normalization_boundary
        ):
            raise ValueError("normalization boundary does not match trimming")
        return self


class SceneContractValidationProof(_StrictModel):
    contract_version: Literal["scene_transition_contract.v2"]
    source_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    scenes: tuple[SceneContractValidationEntry, ...] = Field(
        min_length=1,
        max_length=20,
    )


@dataclass(frozen=True)
class V2SceneBudget:
    scene_id: str
    scene_index: int
    minimum: int
    target: int
    maximum: int


@dataclass(frozen=True)
class V2SourceSceneNormalization:
    provider_word_count: int
    discarded_word_count: int
    normalization_boundary: Literal["sentence", "word"] | None


@dataclass(frozen=True)
class V2SceneRepairTarget:
    scene_id: str
    scene_index: int
    outline_scene: Mapping[str, Any]
    current_prose: str
    left_context_tail: str
    right_context_head: str

    def to_prompt_dict(self) -> dict[str, Any]:
        word_budget = self.outline_scene.get("word_budget")
        return {
            "scene_id": self.scene_id,
            "scene_index": self.scene_index,
            "outline_scene": dict(self.outline_scene),
            "word_budget": (
                dict(word_budget)
                if isinstance(word_budget, Mapping)
                else {}
            ),
            "current_word_count": count_chapter_words(
                self.current_prose
            ),
            "current_prose": self.current_prose,
            "left_context_tail": self.left_context_tail,
            "right_context_head": self.right_context_head,
        }


@dataclass(frozen=True)
class V2SceneRepairPlan:
    budgets: tuple[V2SceneBudget, ...]
    source_scene_texts: tuple[str, ...]
    source_scene_normalizations: tuple[
        V2SourceSceneNormalization,
        ...,
    ]
    source_failure_reason_codes_by_scene: tuple[tuple[str, ...], ...]
    source_failing_scene_indexes: tuple[int, ...]
    requested_scene_indexes: tuple[int, ...]
    target_scene_indexes: tuple[int, ...]
    targets: tuple[V2SceneRepairTarget, ...]

    @property
    def target_scene_ids(self) -> tuple[str, ...]:
        return tuple(target.scene_id for target in self.targets)


@dataclass(frozen=True)
class V2SceneRepairAssembly:
    prose: str
    proof: SceneContractValidationProof
    reason_codes: tuple[str, ...]
    preserved_scene_indexes: tuple[int, ...]
    replaced_scene_indexes: tuple[int, ...]
    resolved_scene_indexes: tuple[int, ...]
    remaining_scene_indexes: tuple[int, ...]
    can_checkpoint: bool
    checkpoint_block_reason_codes: tuple[str, ...]
    content_changed: bool


@dataclass(frozen=True)
class RepairTargetProgress:
    target_issue_categories: tuple[str, ...]
    target_scene_indexes: tuple[int, ...]
    remaining_issue_categories: tuple[str, ...]
    remaining_unscoped_issue_categories: tuple[str, ...]
    remaining_scene_indexes: tuple[int, ...]
    resolved_issue_categories: tuple[str, ...]
    resolved_scene_indexes: tuple[int, ...]

    @property
    def made_progress(self) -> bool:
        return bool(
            self.resolved_issue_categories
            or (
                self.resolved_scene_indexes
                and not self.remaining_unscoped_issue_categories
            )
        )


def evaluate_repair_target_progress(
    *,
    target_issue_categories: Sequence[str],
    target_scene_indexes: Sequence[int],
    observed_issue_categories: Sequence[str],
    observed_scene_indexes: Sequence[int],
    observed_unscoped_issue_categories: Sequence[str] = (),
) -> RepairTargetProgress:
    """Measure persisted target reduction; rewrite scope is not target proof."""
    target_categories = {str(item) for item in target_issue_categories}
    target_scenes = {int(item) for item in target_scene_indexes}
    remaining_categories = target_categories.intersection(
        str(item) for item in observed_issue_categories
    )
    remaining_scenes = target_scenes.intersection(
        int(item) for item in observed_scene_indexes
    )
    remaining_unscoped_categories = target_categories.intersection(
        str(item) for item in observed_unscoped_issue_categories
    )
    return RepairTargetProgress(
        target_issue_categories=tuple(sorted(target_categories)),
        target_scene_indexes=tuple(sorted(target_scenes)),
        remaining_issue_categories=tuple(sorted(remaining_categories)),
        remaining_unscoped_issue_categories=tuple(
            sorted(remaining_unscoped_categories)
        ),
        remaining_scene_indexes=tuple(sorted(remaining_scenes)),
        resolved_issue_categories=tuple(sorted(
            target_categories - remaining_categories
        )),
        resolved_scene_indexes=tuple(sorted(
            target_scenes - remaining_scenes
        )),
    )


def incomplete_scene_indexes(
    *,
    completion: Mapping[str, Any],
    scene_count: int,
) -> tuple[int, ...]:
    """Return exact one-based non-complete scenes from a closed progress map."""
    if type(scene_count) is not int or not 1 <= scene_count <= 20:
        raise ValueError("scene_count must be between 1 and 20")
    progress = completion.get("scene_progress")
    if not isinstance(progress, list) or len(progress) != scene_count:
        raise ValueError("completion scene_progress must cover every scene")

    statuses: dict[int, str] = {}
    for item in progress:
        if not isinstance(item, Mapping):
            raise ValueError("completion scene_progress entry is invalid")
        scene_index = item.get("scene_index")
        status = str(item.get("status") or "")
        if (
            type(scene_index) is not int
            or not 0 <= scene_index < scene_count
            or scene_index in statuses
            or status not in _SCENE_PROGRESS_STATUSES
        ):
            raise ValueError("completion scene_progress identity is invalid")
        statuses[scene_index] = status
    if set(statuses) != set(range(scene_count)):
        raise ValueError("completion scene_progress is not contiguous")

    targets = tuple(
        scene_index + 1
        for scene_index in range(scene_count)
        if statuses[scene_index] != "complete"
    )
    if not targets:
        raise ValueError("completion failed without an incomplete scene target")
    return targets


def v2_scene_budgets(
    *,
    outline: Mapping[str, Any],
    plan: ProseExecutionPlan,
) -> tuple[V2SceneBudget, ...]:
    if require_known_scene_contract_version(outline) != (
        SCENE_TRANSITION_CONTRACT_VERSION
    ):
        return ()
    scenes = list(outline.get("scenes") or [])
    if (
        len(scenes) != plan.scene_count
        or len(plan.segment_budgets) != plan.scene_count
        or len(plan.segment_minimums) != plan.scene_count
        or len(plan.segment_maximums) != plan.scene_count
    ):
        raise ValueError("V2 scene contract does not match the execution plan")

    budgets: list[V2SceneBudget] = []
    for zero_based_index, scene in enumerate(scenes):
        scene_id = str(scene.get("scene_id") or "")
        word_budget = scene.get("word_budget")
        if not scene_id or not isinstance(word_budget, Mapping):
            raise ValueError("V2 scene contract is missing an auditable budget")
        values = (
            word_budget.get("min"),
            word_budget.get("target"),
            word_budget.get("max"),
        )
        if any(type(value) is not int for value in values):
            raise ValueError("V2 scene contract budget is invalid")
        minimum, target, maximum = values
        if (
            (minimum, target, maximum)
            != (
                plan.segment_minimums[zero_based_index],
                plan.segment_budgets[zero_based_index],
                plan.segment_maximums[zero_based_index],
            )
            or not minimum <= target <= maximum
        ):
            raise ValueError("V2 scene contract budget drifted from the plan")
        budgets.append(V2SceneBudget(
            scene_id=scene_id,
            scene_index=zero_based_index + 1,
            minimum=minimum,
            target=target,
            maximum=maximum,
        ))
    return tuple(budgets)


def validate_v2_scene_contract_proof(
    *,
    text: str,
    outline: Mapping[str, Any],
    plan: ProseExecutionPlan,
    completion: Mapping[str, Any],
    allow_incomplete: bool = False,
) -> SceneContractValidationProof | None:
    budgets = v2_scene_budgets(outline=outline, plan=plan)
    if not budgets:
        return None
    proof = SceneContractValidationProof.model_validate(
        completion.get("scene_contract_validation")
    )
    if proof.source_content_digest != chapter_content_digest(text):
        raise ValueError("V2 scene budget proof does not bind the candidate")
    if len(proof.scenes) != len(budgets):
        raise ValueError("V2 scene budget proof does not cover every scene")

    previous_end = 0
    for zero_based_index, (entry, budget) in enumerate(
        zip(proof.scenes, budgets, strict=True)
    ):
        expected_start = 0 if zero_based_index == 0 else previous_end + 2
        if (
            entry.scene_id != budget.scene_id
            or entry.start != expected_start
            or entry.end > len(text)
            or (
                zero_based_index
                and text[previous_end:entry.start] != "\n\n"
            )
            or (entry.min, entry.target, entry.max)
            != (budget.minimum, budget.target, budget.maximum)
        ):
            raise ValueError("V2 scene budget proof identity or order is invalid")
        scene_text = text[entry.start:entry.end]
        word_count = count_chapter_words(scene_text)
        if (
            entry.content_digest != chapter_content_digest(scene_text)
            or entry.word_count != word_count
        ):
            raise ValueError(
                "V2 scene budget proof failed deterministic validation"
            )
        reasons = _scene_budget_reason_codes(
            word_count=word_count,
            normalization_boundary=entry.normalization_boundary,
            budget=budget,
        )
        if reasons and not allow_incomplete:
            raise ValueError(
                "V2 scene budget proof failed deterministic validation"
            )
        previous_end = entry.end
    if previous_end != len(text):
        raise ValueError("V2 scene budget proof leaves unowned prose")
    return proof


def _scene_budget_reason_codes(
    *,
    word_count: int,
    normalization_boundary: str | None,
    budget: V2SceneBudget,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if word_count < budget.minimum:
        reasons.append("scene_word_budget_below_minimum")
    if normalization_boundary == "word":
        reasons.append(
            "scene_word_budget_trimmed_without_sentence_boundary"
        )
    elif word_count > budget.maximum:
        reasons.append("scene_word_budget_exceeded")
    return tuple(reasons)


def scene_budget_failure_indexes(
    *,
    completion: Mapping[str, Any],
    scene_count: int,
    text: str,
) -> tuple[int, ...]:
    """Project exact budget-failing scenes from a candidate-bound proof."""

    if type(scene_count) is not int or not 1 <= scene_count <= 20:
        raise ValueError("scene_count must be between 1 and 20")
    proof = SceneContractValidationProof.model_validate(
        completion.get("scene_contract_validation")
    )
    if proof.source_content_digest != chapter_content_digest(text):
        raise ValueError("V2 scene budget proof does not bind the candidate")
    if len(proof.scenes) != scene_count:
        raise ValueError("V2 scene budget proof does not cover every scene")
    if len({entry.scene_id for entry in proof.scenes}) != scene_count:
        raise ValueError("V2 scene budget proof scene identity is ambiguous")

    failing: list[int] = []
    previous_end = 0
    for zero_based_index, entry in enumerate(proof.scenes):
        expected_start = 0 if zero_based_index == 0 else previous_end + 2
        if (
            entry.start != expected_start
            or entry.end > len(text)
            or (
                zero_based_index
                and text[previous_end:entry.start] != "\n\n"
            )
        ):
            raise ValueError("V2 scene budget proof order is invalid")
        scene_text = text[entry.start:entry.end]
        word_count = count_chapter_words(scene_text)
        if (
            entry.content_digest != chapter_content_digest(scene_text)
            or entry.word_count != word_count
        ):
            raise ValueError(
                "V2 scene budget proof failed deterministic validation"
            )
        if (
            word_count < entry.min
            or entry.normalization_boundary == "word"
            or word_count > entry.max
        ):
            failing.append(zero_based_index + 1)
        previous_end = entry.end
    if previous_end != len(text):
        raise ValueError("V2 scene budget proof leaves unowned prose")
    return tuple(failing)


def _scene_texts_from_segments(
    *,
    segments: Sequence[Mapping[str, Any]],
    scene_count: int,
) -> tuple[str, ...]:
    grouped: list[list[tuple[tuple[int, int], str]]] = [
        [] for _ in range(scene_count)
    ]
    for segment in segments:
        if not isinstance(segment, Mapping):
            raise ValueError("persisted prose segment is invalid")
        scene_index = segment.get("scene_index")
        if type(scene_index) is not int or not 0 <= scene_index < scene_count:
            raise ValueError("persisted prose segment scene identity is invalid")
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        call_index = segment.get(
            "scene_call_index",
            segment.get("part_index") or 0,
        )
        sequence_index = segment.get("sequence_index") or 0
        if type(call_index) is not int or type(sequence_index) is not int:
            raise ValueError("persisted prose segment order is invalid")
        grouped[scene_index].append(((call_index, sequence_index), text))
    return tuple(
        "\n\n".join(text for _order, text in sorted(items))
        for items in grouped
    )


def _source_scene_normalizations_from_segments(
    *,
    segments: Sequence[Mapping[str, Any]],
    scene_texts: Sequence[str],
) -> tuple[V2SourceSceneNormalization, ...]:
    discarded_by_scene = [0 for _text in scene_texts]
    boundaries_by_scene: list[set[str]] = [
        set() for _text in scene_texts
    ]
    for segment in segments:
        if not isinstance(segment, Mapping):
            raise ValueError("persisted prose segment is invalid")
        scene_index = segment.get("scene_index")
        if (
            type(scene_index) is not int
            or not 0 <= scene_index < len(scene_texts)
        ):
            raise ValueError("persisted prose segment scene identity is invalid")
        trim_fields_present = any(
            field in segment
            for field in (
                "word_budget_trimmed",
                "word_budget_original_words",
                "word_budget_discarded_words",
                "word_budget_trim_boundary",
            )
        )
        if not trim_fields_present:
            continue
        text = str(segment.get("text") or "").strip()
        retained_words = count_chapter_words(text)
        original_words = segment.get("word_budget_original_words")
        discarded_words = segment.get("word_budget_discarded_words")
        boundary = segment.get("word_budget_trim_boundary")
        if (
            segment.get("word_budget_trimmed") is not True
            or type(original_words) is not int
            or type(discarded_words) is not int
            or discarded_words <= 0
            or original_words != retained_words + discarded_words
            or boundary not in {"sentence", "word"}
        ):
            raise ValueError(
                "persisted prose segment trim evidence is invalid"
            )
        discarded_by_scene[scene_index] += discarded_words
        boundaries_by_scene[scene_index].add(str(boundary))

    normalizations: list[V2SourceSceneNormalization] = []
    for scene_index, scene_text in enumerate(scene_texts):
        discarded_words = discarded_by_scene[scene_index]
        boundaries = boundaries_by_scene[scene_index]
        boundary: Literal["sentence", "word"] | None = None
        if discarded_words:
            boundary = "word" if "word" in boundaries else "sentence"
        normalizations.append(V2SourceSceneNormalization(
            provider_word_count=(
                count_chapter_words(scene_text) + discarded_words
            ),
            discarded_word_count=discarded_words,
            normalization_boundary=boundary,
        ))
    return tuple(normalizations)


def _local_budget_pause_reasons_by_scene(
    *,
    completion: Mapping[str, Any],
    scene_count: int,
) -> tuple[tuple[str, ...], ...]:
    progress = completion.get("scene_progress")
    if progress is None:
        return tuple(() for _index in range(scene_count))
    if not isinstance(progress, list) or len(progress) != scene_count:
        raise ValueError("completion scene_progress must cover every scene")

    reasons_by_index: dict[int, tuple[str, ...]] = {}
    for item in progress:
        if not isinstance(item, Mapping):
            raise ValueError("completion scene_progress entry is invalid")
        scene_index = item.get("scene_index")
        status = str(item.get("status") or "")
        if (
            type(scene_index) is not int
            or not 0 <= scene_index < scene_count
            or scene_index in reasons_by_index
            or status not in _SCENE_PROGRESS_STATUSES
        ):
            raise ValueError("completion scene_progress identity is invalid")
        pause_reason = item.get("pause_reason")
        if pause_reason is not None and not isinstance(pause_reason, str):
            raise ValueError("completion scene pause reason is invalid")
        if pause_reason in SCENE_WORD_BUDGET_TERMINAL_REASONS:
            if status not in {"incomplete", "paused"}:
                raise ValueError(
                    "completed scene cannot retain a local budget failure"
                )
            reasons_by_index[scene_index] = (pause_reason,)
        else:
            reasons_by_index[scene_index] = ()
    if set(reasons_by_index) != set(range(scene_count)):
        raise ValueError("completion scene_progress is not contiguous")
    return tuple(reasons_by_index[index] for index in range(scene_count))


def _merge_scene_failure_reasons(
    *,
    budget_reasons: Sequence[tuple[str, ...]],
    pause_reasons: Sequence[tuple[str, ...]],
) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(dict.fromkeys((*budget, *pause)))
        for budget, pause in zip(
            budget_reasons,
            pause_reasons,
            strict=True,
        )
    )


def _source_scene_evidence(
    *,
    run: Mapping[str, Any],
    current_text: str,
    outline: Mapping[str, Any],
    plan: ProseExecutionPlan,
    budgets: tuple[V2SceneBudget, ...],
) -> tuple[
    tuple[str, ...],
    tuple[int, ...],
    tuple[tuple[str, ...], ...],
    tuple[V2SourceSceneNormalization, ...],
]:
    completion = run.get("completion")
    if not isinstance(completion, Mapping):
        raise ValueError("V2 candidate completion evidence is missing")
    pause_reasons = _local_budget_pause_reasons_by_scene(
        completion=completion,
        scene_count=plan.scene_count,
    )
    if completion.get("scene_contract_validation") is not None:
        allow_incomplete = (
            str(run.get("status") or "") == "incomplete"
            and str(completion.get("status") or "") == "incomplete"
            and completion.get("can_write_formal_prose") is False
        )
        proof = validate_v2_scene_contract_proof(
            text=current_text,
            outline=outline,
            plan=plan,
            completion=completion,
            allow_incomplete=allow_incomplete,
        )
        if proof is None:
            raise ValueError("V2 scene contract proof is unavailable")
        scene_texts = tuple(
            current_text[entry.start:entry.end] for entry in proof.scenes
        )
        budget_reasons = tuple(
            _scene_budget_reason_codes(
                word_count=entry.word_count,
                normalization_boundary=entry.normalization_boundary,
                budget=budget,
            )
            for entry, budget in zip(proof.scenes, budgets, strict=True)
        )
        failure_reasons = _merge_scene_failure_reasons(
            budget_reasons=budget_reasons,
            pause_reasons=pause_reasons,
        )
        normalizations = tuple(
            V2SourceSceneNormalization(
                provider_word_count=entry.provider_word_count,
                discarded_word_count=entry.discarded_word_count,
                normalization_boundary=entry.normalization_boundary,
            )
            for entry in proof.scenes
        )
        failing = tuple(
            budget.scene_index
            for budget, reasons in zip(budgets, failure_reasons, strict=True)
            if reasons
        )
        return scene_texts, failing, failure_reasons, normalizations

    segments = run.get("segments")
    if isinstance(segments, list) and segments:
        scene_texts = _scene_texts_from_segments(
            segments=segments,
            scene_count=plan.scene_count,
        )
        reconstructed = "\n\n".join(
            scene_text for scene_text in scene_texts if scene_text
        )
        if reconstructed != current_text:
            raise ValueError("persisted V2 segments do not bind the candidate")
        budget_reasons = tuple(
            _scene_budget_reason_codes(
                word_count=count_chapter_words(scene_text),
                normalization_boundary=None,
                budget=budget,
            )
            for scene_text, budget in zip(
                scene_texts,
                budgets,
                strict=True,
            )
        )
        failure_reasons = _merge_scene_failure_reasons(
            budget_reasons=budget_reasons,
            pause_reasons=pause_reasons,
        )
        normalizations = _source_scene_normalizations_from_segments(
            segments=segments,
            scene_texts=scene_texts,
        )
        failing = tuple(
            budget.scene_index
            for budget, reasons in zip(budgets, failure_reasons, strict=True)
            if reasons
        )
        return scene_texts, failing, failure_reasons, normalizations

    if plan.scene_count == 1 and current_text:
        budget = budgets[0]
        failure_reasons = _merge_scene_failure_reasons(
            budget_reasons=(
                _scene_budget_reason_codes(
                    word_count=count_chapter_words(current_text),
                    normalization_boundary=None,
                    budget=budget,
                ),
            ),
            pause_reasons=pause_reasons,
        )
        failing = ((budget.scene_index,) if failure_reasons[0] else ())
        return (
            (current_text,),
            failing,
            failure_reasons,
            (V2SourceSceneNormalization(
                provider_word_count=count_chapter_words(current_text),
                discarded_word_count=0,
                normalization_boundary=None,
            ),),
        )
    raise ValueError("V2 candidate has no closed per-scene source evidence")


def build_v2_scene_repair_plan(
    *,
    run: Mapping[str, Any],
    current_text: str,
    outline: Mapping[str, Any],
    plan: ProseExecutionPlan,
    target_scene_indexes: Sequence[int],
    max_source_failure_targets: int | None = None,
) -> V2SceneRepairPlan:
    budgets = v2_scene_budgets(outline=outline, plan=plan)
    if not budgets:
        raise ValueError("scene-local repair requires a V2 scene contract")
    requested_targets = tuple(sorted(target_scene_indexes))
    if (
        not requested_targets
        or len(set(requested_targets)) != len(requested_targets)
        or any(
            type(index) is not int or not 1 <= index <= len(budgets)
            for index in requested_targets
        )
    ):
        raise ValueError("scene-local repair targets are invalid")
    if (
        max_source_failure_targets is not None
        and (
            type(max_source_failure_targets) is not int
            or max_source_failure_targets < 1
            or max_source_failure_targets > len(budgets)
        )
    ):
        raise ValueError("scene-local repair target limit is invalid")

    (
        source_scene_texts,
        source_failing_scene_indexes,
        source_failure_reason_codes_by_scene,
        source_scene_normalizations,
    ) = _source_scene_evidence(
        run=run,
        current_text=current_text,
        outline=outline,
        plan=plan,
        budgets=budgets,
    )
    normalized_targets = requested_targets
    if source_failing_scene_indexes:
        missing_source_failures = set(source_failing_scene_indexes) - set(
            requested_targets
        )
        if missing_source_failures:
            raise ValueError(
                "scene-local repair request omits a source budget failure"
            )
        normalized_targets = source_failing_scene_indexes
        if max_source_failure_targets is not None:
            normalized_targets = normalized_targets[
                :max_source_failure_targets
            ]
    outline_scenes = list(outline.get("scenes") or [])
    targets: list[V2SceneRepairTarget] = []
    for scene_index in normalized_targets:
        zero_based_index = scene_index - 1
        targets.append(V2SceneRepairTarget(
            scene_id=budgets[zero_based_index].scene_id,
            scene_index=scene_index,
            outline_scene=dict(outline_scenes[zero_based_index]),
            current_prose=source_scene_texts[zero_based_index],
            left_context_tail=(
                source_scene_texts[zero_based_index - 1][
                    -SCENE_REPAIR_BOUNDARY_CONTEXT_CHARACTERS:
                ]
                if zero_based_index > 0
                else ""
            ),
            right_context_head=(
                source_scene_texts[zero_based_index + 1][
                    :SCENE_REPAIR_BOUNDARY_CONTEXT_CHARACTERS
                ]
                if zero_based_index + 1 < len(source_scene_texts)
                else ""
            ),
        ))
    return V2SceneRepairPlan(
        budgets=budgets,
        source_scene_texts=source_scene_texts,
        source_scene_normalizations=source_scene_normalizations,
        source_failure_reason_codes_by_scene=(
            source_failure_reason_codes_by_scene
        ),
        source_failing_scene_indexes=source_failing_scene_indexes,
        requested_scene_indexes=requested_targets,
        target_scene_indexes=normalized_targets,
        targets=tuple(targets),
    )


def apply_v2_scene_replacements(
    *,
    repair_plan: V2SceneRepairPlan,
    replacements: Sequence[Mapping[str, Any]],
) -> V2SceneRepairAssembly:
    actual_scene_ids = tuple(
        str(replacement.get("scene_id") or "")
        if isinstance(replacement, Mapping)
        else ""
        for replacement in replacements
    )
    if actual_scene_ids != repair_plan.target_scene_ids:
        raise ValueError(
            "V2 rewrite must return only targeted scenes in outline order"
        )
    replacements_by_index = {
        target.scene_index: str(replacement.get("prose") or "")
        for target, replacement in zip(
            repair_plan.targets,
            replacements,
            strict=True,
        )
    }

    parts: list[str] = []
    entries: list[SceneContractValidationEntry] = []
    reason_codes: list[str] = []
    current_reason_codes_by_scene: list[tuple[str, ...]] = []
    cursor = 0
    target_indexes = set(repair_plan.target_scene_indexes)
    for zero_based_index, budget in enumerate(repair_plan.budgets):
        if zero_based_index:
            parts.append("\n\n")
            cursor += 2
        if budget.scene_index in target_indexes:
            normalized = trim_scene_contribution_to_word_budget(
                current_text="",
                contribution=replacements_by_index[budget.scene_index],
                maximum_words=budget.maximum,
                enabled=True,
            )
            scene_text = normalized.text
            provider_word_count = normalized.original_word_count
            discarded_word_count = normalized.discarded_word_count
            normalization_boundary = normalized.boundary
        else:
            scene_text = repair_plan.source_scene_texts[zero_based_index]
            normalization = repair_plan.source_scene_normalizations[
                zero_based_index
            ]
            provider_word_count = normalization.provider_word_count
            discarded_word_count = normalization.discarded_word_count
            normalization_boundary = normalization.normalization_boundary
        if not scene_text and budget.scene_index in target_indexes:
            raise ValueError("V2 repaired scene cannot be empty")

        start = cursor
        parts.append(scene_text)
        cursor += len(scene_text)
        word_count = count_chapter_words(scene_text)
        scene_reasons = _scene_budget_reason_codes(
            word_count=word_count,
            normalization_boundary=normalization_boundary,
            budget=budget,
        )
        current_reason_codes_by_scene.append(scene_reasons)
        reason_codes.extend(scene_reasons)
        entries.append(SceneContractValidationEntry(
            scene_id=budget.scene_id,
            start=start,
            end=cursor,
            content_digest=chapter_content_digest(scene_text),
            word_count=word_count,
            provider_word_count=provider_word_count,
            discarded_word_count=discarded_word_count,
            normalization_boundary=normalization_boundary,
            min=budget.minimum,
            target=budget.target,
            max=budget.maximum,
        ))

    prose = "".join(parts)
    if len(prose) > MAX_SCENE_REPAIR_PROSE_CHARACTERS:
        raise ValueError("V2 rewritten prose exceeds the remediation limit")
    proof = SceneContractValidationProof(
        contract_version=SCENE_TRANSITION_CONTRACT_VERSION,
        source_content_digest=chapter_content_digest(prose),
        scenes=tuple(entries),
    )
    preserved = tuple(
        budget.scene_index
        for budget in repair_plan.budgets
        if budget.scene_index not in target_indexes
    )
    new_failing = tuple(
        budget.scene_index
        for budget, reasons in zip(
            repair_plan.budgets,
            current_reason_codes_by_scene,
            strict=True,
        )
        if reasons
    )
    source_failing = set(repair_plan.source_failing_scene_indexes)
    carried_source_failures = source_failing - target_indexes
    for scene_index in sorted(carried_source_failures):
        reason_codes.extend(
            repair_plan.source_failure_reason_codes_by_scene[
                scene_index - 1
            ]
        )
    new_failing_set = set(new_failing).union(carried_source_failures)
    remaining_source_failures = new_failing_set.intersection(source_failing)
    resolved = tuple(sorted(source_failing - remaining_source_failures))
    remaining = tuple(sorted(remaining_source_failures))
    unique_reasons = tuple(dict.fromkeys(reason_codes))
    target_repair_reason_codes = {
        reason
        for scene_index in target_indexes
        for reason in current_reason_codes_by_scene[scene_index - 1]
    }
    content_changed = (
        chapter_content_digest(prose)
        != chapter_content_digest("\n\n".join(repair_plan.source_scene_texts))
    )
    checkpoint_block_reason_codes: list[str] = []
    if unique_reasons:
        if not resolved:
            checkpoint_block_reason_codes.append(
                "checkpoint_no_target_failure_resolved"
            )
        if not target_indexes.issubset(source_failing):
            checkpoint_block_reason_codes.append(
                "checkpoint_source_failure_scope_mismatch"
            )
        if not remaining_source_failures < source_failing:
            checkpoint_block_reason_codes.append(
                "checkpoint_failure_scope_not_reduced"
            )
        if not new_failing_set.issubset(source_failing):
            checkpoint_block_reason_codes.append(
                "checkpoint_new_scene_failure"
            )
        if target_repair_reason_codes - {
            "scene_word_budget_below_minimum"
        }:
            checkpoint_block_reason_codes.append(
                "checkpoint_unsupported_scene_failure_reason"
            )
        if not content_changed:
            checkpoint_block_reason_codes.append(
                "checkpoint_content_unchanged"
            )
    can_checkpoint = bool(
        unique_reasons and not checkpoint_block_reason_codes
    )
    return V2SceneRepairAssembly(
        prose=prose,
        proof=proof,
        reason_codes=unique_reasons,
        preserved_scene_indexes=preserved,
        replaced_scene_indexes=repair_plan.target_scene_indexes,
        resolved_scene_indexes=resolved,
        remaining_scene_indexes=remaining,
        can_checkpoint=can_checkpoint,
        checkpoint_block_reason_codes=tuple(
            checkpoint_block_reason_codes
        ),
        content_changed=content_changed,
    )
