"""Typed chapter-repair budgets and deterministic issue-set convergence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


MAX_COMPONENT_REPAIR_BUDGET = 512
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_Sha256 = Annotated[str, Field(pattern=_SHA256_PATTERN)]
_NonNegativeInt = Annotated[StrictInt, Field(ge=0)]


class RepairComponent(StrEnum):
    """Closed repair components that may never borrow each other's budget."""

    PROVIDER_TECHNICAL_RETRY = "provider_technical_retry"
    ADHERENCE_JUDGE_RETRY = "adherence_judge_retry"
    STATE_REEXTRACTION = "state_reextraction"
    LOCAL_PROSE_REPAIR = "local_prose_repair"
    SCENE_REGENERATION = "scene_regeneration"
    OUTLINE_ROLLBACK = "outline_rollback"


CONTENT_REPAIR_COMPONENTS = frozenset({
    RepairComponent.LOCAL_PROSE_REPAIR,
    RepairComponent.SCENE_REGENERATION,
})


_NEXT_STEPS: dict[RepairComponent, str] = {
    RepairComponent.PROVIDER_TECHNICAL_RETRY: (
        "retry_provider_manually_or_create_successor_authorization"
    ),
    RepairComponent.ADHERENCE_JUDGE_RETRY: (
        "review_adherence_evidence_or_create_successor_authorization"
    ),
    RepairComponent.STATE_REEXTRACTION: (
        "review_state_evidence_or_create_successor_authorization"
    ),
    RepairComponent.LOCAL_PROSE_REPAIR: (
        "review_prose_or_create_successor_authorization"
    ),
    RepairComponent.SCENE_REGENERATION: (
        "review_scene_contract_or_create_successor_authorization"
    ),
    RepairComponent.OUTLINE_ROLLBACK: (
        "review_outline_manually_or_create_successor_authorization"
    ),
}


def repair_next_step(component: RepairComponent) -> str:
    return _NEXT_STEPS[RepairComponent(component)]


class _RepairPolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RepairBudgetLimitsV1(_RepairPolicyModel):
    """Frozen per-component limits for one chapter candidate execution."""

    schema_version: Literal["chapter_repair_budget_limits.v1"] = (
        "chapter_repair_budget_limits.v1"
    )
    provider_technical_retry: StrictInt = Field(
        ge=0,
        le=MAX_COMPONENT_REPAIR_BUDGET,
    )
    adherence_judge_retry: StrictInt = Field(
        ge=0,
        le=MAX_COMPONENT_REPAIR_BUDGET,
    )
    state_reextraction: StrictInt = Field(
        ge=0,
        le=MAX_COMPONENT_REPAIR_BUDGET,
    )
    local_prose_repair: StrictInt = Field(
        ge=0,
        le=MAX_COMPONENT_REPAIR_BUDGET,
    )
    scene_regeneration: StrictInt = Field(
        ge=0,
        le=MAX_COMPONENT_REPAIR_BUDGET,
    )
    outline_rollback: StrictInt = Field(
        ge=0,
        le=MAX_COMPONENT_REPAIR_BUDGET,
    )

    def limit_for(self, component: RepairComponent) -> int:
        return int(getattr(self, component.value))


class RepairIssueV1(_RepairPolicyModel):
    """One locally normalized hard issue; Provider wording is intentionally absent."""

    schema_version: Literal["chapter_repair_issue.v1"] = (
        "chapter_repair_issue.v1"
    )
    issue_signature: _Sha256
    severity: Literal["blocker", "major"]


class RepairChargeV1(_RepairPolicyModel):
    """One successful component-budget reservation in repair-event order."""

    schema_version: Literal["chapter_repair_charge.v1"] = (
        "chapter_repair_charge.v1"
    )
    event_sequence: StrictInt = Field(ge=1, le=MAX_COMPONENT_REPAIR_BUDGET * 6)
    component: RepairComponent
    component_attempt: StrictInt = Field(
        ge=1,
        le=MAX_COMPONENT_REPAIR_BUDGET,
    )
    authorized_limit: StrictInt = Field(
        ge=1,
        le=MAX_COMPONENT_REPAIR_BUDGET,
    )


class RepairComponentUsageV1(_RepairPolicyModel):
    """Current content-free usage projection for one repair component."""

    schema_version: Literal["chapter_repair_component_usage.v1"] = (
        "chapter_repair_component_usage.v1"
    )
    component: RepairComponent
    used: StrictInt = Field(ge=0, le=MAX_COMPONENT_REPAIR_BUDGET)
    authorized_limit: StrictInt = Field(
        ge=0,
        le=MAX_COMPONENT_REPAIR_BUDGET,
    )


class RepairFailureEvidenceV1(_RepairPolicyModel):
    """Closed, content-free repair context attached to one terminal stop."""

    schema_version: Literal["chapter_repair_failure_evidence.v1"] = (
        "chapter_repair_failure_evidence.v1"
    )
    component: RepairComponent
    component_used: StrictInt = Field(
        ge=0,
        le=MAX_COMPONENT_REPAIR_BUDGET,
    )
    component_limit: StrictInt = Field(
        ge=0,
        le=MAX_COMPONENT_REPAIR_BUDGET,
    )
    next_step: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_closed_route(self) -> "RepairFailureEvidenceV1":
        if self.component_used > self.component_limit:
            raise ValueError("repair failure usage exceeds its authority")
        if self.next_step != repair_next_step(self.component):
            raise ValueError("repair failure next step is not canonical")
        return self


class RepairConvergenceEvidenceV1(_RepairPolicyModel):
    """Content-free proof for one post-repair adherence recheck."""

    schema_version: Literal["chapter_repair_convergence.v1"] = (
        "chapter_repair_convergence.v1"
    )
    charge: RepairChargeV1
    decision: Literal["converged", "progress", "not_converged"]
    target_issue_signatures: tuple[_Sha256, ...] = Field(max_length=80)
    remaining_target_issue_signatures: tuple[_Sha256, ...] = Field(max_length=80)
    resolved_issue_signatures: tuple[_Sha256, ...] = Field(max_length=80)
    introduced_issue_signatures: tuple[_Sha256, ...] = Field(max_length=80)
    regressed_issue_signatures: tuple[_Sha256, ...] = Field(max_length=80)
    introduced_blocker_signatures: tuple[_Sha256, ...] = Field(max_length=80)
    reason_codes: tuple[
        Literal[
            "target_issue_set_not_reduced",
            "introduced_blocker",
            "resolved_issue_regressed",
            "short_cycle_detected",
        ],
        ...,
    ] = Field(max_length=4)
    prose_run_revision_sequence: tuple[_NonNegativeInt, _NonNegativeInt]
    content_digest_sequence: tuple[_Sha256, _Sha256]
    cycle_kind: Literal["issue_set", "content_digest"] | None = None
    cycle_length: StrictInt | None = Field(default=None, ge=2, le=64)

    @property
    def component(self) -> RepairComponent:
        return self.charge.component

    @property
    def component_attempt(self) -> int:
        return self.charge.component_attempt


class RepairBudgetExhausted(ValueError):
    """Stable, actionable stop for one exhausted repair component."""

    code = "repair_budget_exhausted"

    def __init__(
        self,
        *,
        component: RepairComponent,
        used: int,
        limit: int,
    ) -> None:
        super().__init__(f"repair budget exhausted for {component.value}")
        self.component = component
        self.used = used
        self.limit = limit
        self.next_step = _NEXT_STEPS[component]


@dataclass(frozen=True)
class _RepairObservation:
    issues: frozenset[str]
    blockers: frozenset[str]
    revision: int
    content_digest: str


def default_repair_budget_limits(
    max_component_repairs: int,
) -> RepairBudgetLimitsV1:
    """Build the direct-call fallback; production passes frozen exact limits."""

    if (
        isinstance(max_component_repairs, bool)
        or not isinstance(max_component_repairs, int)
        or not 0 <= max_component_repairs <= 8
    ):
        raise ValueError("max component repairs must be an integer from 0 to 8")
    return RepairBudgetLimitsV1(
        provider_technical_retry=0,
        adherence_judge_retry=min(
            MAX_COMPONENT_REPAIR_BUDGET,
            max_component_repairs * 64,
        ),
        state_reextraction=max_component_repairs,
        local_prose_repair=max_component_repairs,
        scene_regeneration=max_component_repairs,
        outline_rollback=0,
    )


class ChapterRepairPolicy:
    """Deep in-process module for repair authorization and convergence replay.

    Callers first record the failed adherence issue set, reserve exactly one
    content component, then submit the post-repair issue set with that charge.
    Other components can be charged independently without affecting content
    repair quotas.
    """

    def __init__(self, limits: RepairBudgetLimitsV1) -> None:
        self._limits = RepairBudgetLimitsV1.model_validate(
            limits.model_dump(mode="python")
            if isinstance(limits, RepairBudgetLimitsV1)
            else limits
        )
        self._used = {component: 0 for component in RepairComponent}
        self._event_sequence = 0
        self._observations: list[_RepairObservation] = []
        self._resolved_history: set[str] = set()
        self._transitions: list[RepairConvergenceEvidenceV1] = []

    @property
    def limits(self) -> RepairBudgetLimitsV1:
        return self._limits

    @property
    def transitions(self) -> tuple[RepairConvergenceEvidenceV1, ...]:
        return tuple(self._transitions)

    @property
    def observation_count(self) -> int:
        return len(self._observations)

    @property
    def component_usage(self) -> tuple[RepairComponentUsageV1, ...]:
        return tuple(
            RepairComponentUsageV1(
                component=component,
                used=self._used[component],
                authorized_limit=self._limits.limit_for(component),
            )
            for component in RepairComponent
        )

    def used(self, component: RepairComponent) -> int:
        normalized = RepairComponent(component)
        return self._used[normalized]

    def authorize(self, component: RepairComponent) -> RepairChargeV1:
        normalized = RepairComponent(component)
        used = self._used[normalized]
        limit = self._limits.limit_for(normalized)
        if used >= limit:
            raise RepairBudgetExhausted(
                component=normalized,
                used=used,
                limit=limit,
            )
        self._event_sequence += 1
        self._used[normalized] = used + 1
        return RepairChargeV1(
            event_sequence=self._event_sequence,
            component=normalized,
            component_attempt=used + 1,
            authorized_limit=limit,
        )

    def observe_adherence(
        self,
        issues: Sequence[RepairIssueV1],
        *,
        prose_run_revision: int,
        content_digest: str,
        charge: RepairChargeV1 | None = None,
    ) -> RepairConvergenceEvidenceV1 | None:
        normalized_issues = self._normalize_issues(issues)
        if (
            isinstance(prose_run_revision, bool)
            or not isinstance(prose_run_revision, int)
            or prose_run_revision < 0
        ):
            raise ValueError("repair prose revision is invalid")
        if not self._is_sha256(content_digest):
            raise ValueError("repair content digest is invalid")
        current = frozenset(item.issue_signature for item in normalized_issues)
        blockers = frozenset(
            item.issue_signature
            for item in normalized_issues
            if item.severity == "blocker"
        )
        if not self._observations:
            if charge is not None:
                raise ValueError("repair convergence has no target issue set")
            self._append_observation(
                current,
                blockers,
                prose_run_revision,
                content_digest,
            )
            return None
        if charge is None:
            raise ValueError("post-repair adherence requires a repair charge")
        validated_charge = RepairChargeV1.model_validate(
            charge.model_dump(mode="python")
            if isinstance(charge, RepairChargeV1)
            else charge
        )
        if validated_charge.component not in CONTENT_REPAIR_COMPONENTS:
            raise ValueError("issue-set convergence requires a content repair")
        if (
            validated_charge.component_attempt
            != self._used[validated_charge.component]
            or validated_charge.authorized_limit
            != self._limits.limit_for(validated_charge.component)
        ):
            raise ValueError("repair charge does not match current authorization")

        previous_observation = self._observations[-1]
        previous = previous_observation.issues
        resolved = previous - current
        introduced = current - previous
        remaining = previous & current
        regressed = current & self._resolved_history
        introduced_blockers = blockers - previous_observation.blockers
        cycle_kind, cycle_length = self._cycle(
            current=current,
            content_digest=content_digest,
        )

        reason_codes: list[str] = []
        if not resolved:
            reason_codes.append("target_issue_set_not_reduced")
        if introduced_blockers:
            reason_codes.append("introduced_blocker")
        if regressed:
            reason_codes.append("resolved_issue_regressed")
        if cycle_kind is not None:
            reason_codes.append("short_cycle_detected")
        decision: Literal["converged", "progress", "not_converged"]
        if reason_codes:
            decision = "not_converged"
        elif not current:
            decision = "converged"
        else:
            decision = "progress"

        evidence = RepairConvergenceEvidenceV1(
            charge=validated_charge,
            decision=decision,
            target_issue_signatures=tuple(sorted(previous)),
            remaining_target_issue_signatures=tuple(sorted(remaining)),
            resolved_issue_signatures=tuple(sorted(resolved)),
            introduced_issue_signatures=tuple(sorted(introduced)),
            regressed_issue_signatures=tuple(sorted(regressed)),
            introduced_blocker_signatures=tuple(sorted(introduced_blockers)),
            reason_codes=tuple(reason_codes),
            prose_run_revision_sequence=(
                previous_observation.revision,
                prose_run_revision,
            ),
            content_digest_sequence=(
                previous_observation.content_digest,
                content_digest,
            ),
            cycle_kind=cycle_kind,
            cycle_length=cycle_length,
        )
        self._resolved_history.update(resolved)
        self._transitions.append(evidence)
        self._append_observation(
            current,
            blockers,
            prose_run_revision,
            content_digest,
        )
        return evidence

    @staticmethod
    def _normalize_issues(
        issues: Sequence[RepairIssueV1],
    ) -> tuple[RepairIssueV1, ...]:
        if isinstance(issues, (str, bytes)) or not isinstance(issues, Sequence):
            raise ValueError("repair issues must be a bounded sequence")
        if len(issues) > 80:
            raise ValueError("repair issue set exceeds its bounded length")
        normalized = tuple(
            RepairIssueV1.model_validate(
                item.model_dump(mode="python")
                if isinstance(item, RepairIssueV1)
                else item
            )
            for item in issues
        )
        signatures = [item.issue_signature for item in normalized]
        if len(signatures) != len(set(signatures)):
            raise ValueError("repair issue signatures must be unique")
        return tuple(sorted(normalized, key=lambda item: item.issue_signature))

    @staticmethod
    def _is_sha256(value: object) -> bool:
        return bool(
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    def _append_observation(
        self,
        issues: frozenset[str],
        blockers: frozenset[str],
        revision: int,
        digest: str,
    ) -> None:
        self._observations.append(_RepairObservation(
            issues=issues,
            blockers=blockers,
            revision=revision,
            content_digest=digest,
        ))

    def _cycle(
        self,
        *,
        current: frozenset[str],
        content_digest: str,
    ) -> tuple[Literal["issue_set", "content_digest"] | None, int | None]:
        issue_cycle = self._shortest_repeat(
            tuple(item.issues for item in self._observations),
            current,
        )
        if issue_cycle is not None:
            return "issue_set", issue_cycle
        digest_cycle = self._shortest_repeat(
            tuple(item.content_digest for item in self._observations),
            content_digest,
        )
        if digest_cycle is not None:
            return "content_digest", digest_cycle
        return None, None

    @staticmethod
    def _shortest_repeat(history: Sequence[object], current: object) -> int | None:
        distances = [
            len(history) - index
            for index, prior in enumerate(history)
            if prior == current and len(history) - index >= 2
        ]
        return min(distances) if distances else None
