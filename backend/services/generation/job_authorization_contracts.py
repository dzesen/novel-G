"""Strict authorization contracts for post-outline Job narrowing."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.services.generation.prose_continuation import (
    CURRENT_CONTINUATION_AUTHORIZATION_RULESET_REVISION,
)


MAX_BSON_INT64 = 2**63 - 1
OUTLINE_AUTHORIZATION_SCOPE_FIELDS = (
    "max_base_calls",
    "max_automatic_continuation_calls",
    "max_logical_prose_calls",
    "max_actual_provider_attempts",
    "base_output_token_bound",
    "continuation_output_token_bound",
    "conservative_base_token_bound",
    "conservative_continuation_token_bound",
    "conservative_token_bound",
    "conservative_total_token_bound",
)
_HEX_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ProseContinuationPolicySnapshotV1(_ClosedModel):
    automatic_continuations_per_scene: int = Field(ge=0, le=15)
    continuation_target_words: int = Field(ge=400, le=5_000)


class ProseBudgetCoverageSnapshotV1(_ClosedModel):
    status: Literal["available", "unavailable"]
    estimated_prose_chapter_count: int = Field(ge=0, le=1_000_000)
    chapters_with_automatic_continuations: int | None = Field(
        default=None,
        ge=0,
        le=1_000_000,
    )
    chapters_without_automatic_continuations: int | None = Field(
        default=None,
        ge=0,
        le=1_000_000,
    )
    unavailable_reason: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_status_projection(self) -> "ProseBudgetCoverageSnapshotV1":
        with_calls = self.chapters_with_automatic_continuations
        without_calls = self.chapters_without_automatic_continuations
        if self.status == "available":
            if (
                with_calls is None
                or without_calls is None
                or self.unavailable_reason is not None
                or with_calls > self.estimated_prose_chapter_count
                or without_calls > self.estimated_prose_chapter_count
            ):
                raise ValueError("prose budget coverage is invalid")
        elif (
            with_calls is not None
            or without_calls is not None
            or not self.unavailable_reason
        ):
            raise ValueError("prose budget coverage is invalid")
        return self


class ProseContinuationAuthorizationSnapshotV1(_ClosedModel):
    """Closed persisted authorization produced by readiness."""

    policy: ProseContinuationPolicySnapshotV1
    authorization_revision: int = Field(ge=1, le=MAX_BSON_INT64)
    authorization_ruleset_revision: str = Field(min_length=1, max_length=128)
    content_identity: str = Field(pattern=_HEX_DIGEST_PATTERN)
    provider_plan_revision: str = Field(pattern=_HEX_DIGEST_PATTERN)
    max_base_calls: int = Field(ge=1, le=1_000_000)
    max_automatic_continuation_calls: int = Field(ge=0, le=1_000_000)
    max_logical_prose_calls: int = Field(ge=1, le=1_000_000)
    max_actual_provider_attempts: int = Field(ge=1, le=1_000_000)
    base_output_token_bound: int = Field(ge=0, le=1_000_000_000)
    continuation_output_token_bound: int = Field(ge=0, le=1_000_000_000)
    conservative_base_token_bound: int = Field(ge=1, le=1_000_000_000)
    conservative_continuation_token_bound: int = Field(
        ge=1,
        le=1_000_000_000,
    )
    conservative_token_bound: int = Field(ge=1, le=1_000_000_000)
    conservative_total_token_bound: int = Field(ge=1, le=MAX_BSON_INT64)
    token_bound_known: bool
    budget_coverage: ProseBudgetCoverageSnapshotV1
    token_budget: int | None = Field(default=None, ge=1, le=MAX_BSON_INT64)
    readiness_digest: str = Field(pattern=_HEX_DIGEST_PATTERN)

    @model_validator(mode="after")
    def validate_derived_bounds(self) -> "ProseContinuationAuthorizationSnapshotV1":
        if self.authorization_ruleset_revision != (
            CURRENT_CONTINUATION_AUTHORIZATION_RULESET_REVISION
        ):
            raise ValueError("prose authorization ruleset is invalid")
        if self.max_logical_prose_calls != (
            self.max_base_calls + self.max_automatic_continuation_calls
        ):
            raise ValueError("prose authorization call total is invalid")
        if self.max_actual_provider_attempts != self.max_logical_prose_calls:
            raise ValueError("prose authorization Provider total is invalid")
        if self.conservative_token_bound != max(
            self.conservative_base_token_bound,
            self.conservative_continuation_token_bound,
        ):
            raise ValueError("prose authorization token bound is invalid")
        expected_total = (
            self.max_base_calls * self.conservative_base_token_bound
            + self.max_automatic_continuation_calls
            * self.conservative_continuation_token_bound
        )
        if (
            expected_total > MAX_BSON_INT64
            or self.conservative_total_token_bound != expected_total
        ):
            raise ValueError("prose authorization total token bound is invalid")
        return self


class ProseAuthorizationScopeV1(_ClosedModel):
    max_base_calls: int = Field(ge=0, le=1_000_000)
    max_automatic_continuation_calls: int = Field(ge=0, le=1_000_000)
    max_logical_prose_calls: int = Field(ge=0, le=1_000_000)
    max_actual_provider_attempts: int = Field(ge=0, le=1_000_000)
    base_output_token_bound: int = Field(ge=0, le=1_000_000_000)
    continuation_output_token_bound: int = Field(ge=0, le=1_000_000_000)
    conservative_base_token_bound: int = Field(ge=0, le=1_000_000_000)
    conservative_continuation_token_bound: int = Field(
        ge=0,
        le=1_000_000_000,
    )
    conservative_token_bound: int = Field(ge=0, le=1_000_000_000)
    conservative_total_token_bound: int = Field(ge=0, le=MAX_BSON_INT64)


class OutlineAuthorizationRecalculationCommandV1(_ClosedModel):
    """Caller snapshot bound to the Job state observed before recalculation."""

    schema_version: Literal["outline_authorization_recalculation_command.v1"] = (
        "outline_authorization_recalculation_command.v1"
    )
    chapter_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    authorization_revision: int = Field(ge=1, le=MAX_BSON_INT64)
    expected_narrative_revision: int = Field(ge=0, le=MAX_BSON_INT64)
    readiness_digest: str = Field(min_length=1, max_length=128)
    expected_authorization_digest: str = Field(pattern=_HEX_DIGEST_PATTERN)
    candidate_scope: ProseAuthorizationScopeV1 | None
    new_acknowledgement_codes: tuple[str, ...] = Field(max_length=64)
    blocked_issue_codes: tuple[str, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def validate_issue_codes(self) -> "OutlineAuthorizationRecalculationCommandV1":
        for values in (
            self.new_acknowledgement_codes,
            self.blocked_issue_codes,
        ):
            if len(set(values)) != len(values) or any(
                not value or len(value) > 128 for value in values
            ):
                raise ValueError("outline authorization issue codes are invalid")
        return self


class OutlineAuthorizationRecalculationDecisionV1(_ClosedModel):
    """Metadata-only result safe to persist on a Generation Job."""

    schema_version: Literal["outline_authorization_recalculation.v1"] = (
        "outline_authorization_recalculation.v1"
    )
    chapter_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    authorization_revision: int = Field(ge=1, le=MAX_BSON_INT64)
    authorized_scope: ProseAuthorizationScopeV1
    candidate_scope: ProseAuthorizationScopeV1
    exceeded_fields: tuple[str, ...] = Field(max_length=32)
    new_acknowledgement_codes: tuple[str, ...] = Field(max_length=64)
    blocked_issue_codes: tuple[str, ...] = Field(max_length=64)
    expected_narrative_revision: int = Field(ge=0, le=MAX_BSON_INT64)
    status: Literal["narrowed_or_unchanged", "confirmation_required"]
    requires_confirmation: bool

    @model_validator(mode="after")
    def validate_status(self) -> "OutlineAuthorizationRecalculationDecisionV1":
        expected_confirmation = bool(
            self.exceeded_fields
            or self.new_acknowledgement_codes
            or self.blocked_issue_codes
        )
        if self.requires_confirmation != expected_confirmation or (
            self.status
            != (
                "confirmation_required"
                if expected_confirmation
                else "narrowed_or_unchanged"
            )
        ):
            raise ValueError("outline authorization decision is invalid")
        return self


def parse_prose_authorization(
    value: Any,
) -> ProseContinuationAuthorizationSnapshotV1:
    if not isinstance(value, Mapping):
        raise ValueError("prose continuation authorization is invalid")
    return ProseContinuationAuthorizationSnapshotV1.model_validate(value)


def prose_authorization_digest(
    value: ProseContinuationAuthorizationSnapshotV1 | Mapping[str, Any],
) -> str:
    snapshot = (
        value
        if isinstance(value, ProseContinuationAuthorizationSnapshotV1)
        else parse_prose_authorization(value)
    )
    encoded = json.dumps(
        snapshot.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def prose_authorization_scope(
    value: ProseContinuationAuthorizationSnapshotV1,
) -> ProseAuthorizationScopeV1:
    return ProseAuthorizationScopeV1(**{
        field: getattr(value, field)
        for field in OUTLINE_AUTHORIZATION_SCOPE_FIELDS
    })


def evaluate_outline_authorization_recalculation(
    *,
    command: OutlineAuthorizationRecalculationCommandV1,
    current_authorization: ProseContinuationAuthorizationSnapshotV1,
    job_token_budget: int | None,
) -> OutlineAuthorizationRecalculationDecisionV1:
    """Derive the only persistable result without trusting caller projections."""

    if (
        current_authorization.authorization_revision
        != command.authorization_revision
        or prose_authorization_digest(current_authorization)
        != command.expected_authorization_digest
        or current_authorization.token_budget != job_token_budget
    ):
        raise ValueError("outline authorization snapshot changed")
    authorized_scope = prose_authorization_scope(current_authorization)
    candidate_scope = command.candidate_scope
    exceeded: list[str] = []
    if candidate_scope is None:
        candidate_scope = ProseAuthorizationScopeV1(**{
            field: 0 for field in OUTLINE_AUTHORIZATION_SCOPE_FIELDS
        })
        exceeded.append("candidate_authorization_missing")
    else:
        exceeded.extend(
            field
            for field in OUTLINE_AUTHORIZATION_SCOPE_FIELDS
            if getattr(candidate_scope, field) > getattr(authorized_scope, field)
        )
    new_codes = tuple(sorted(command.new_acknowledgement_codes))
    blocked_codes = tuple(sorted(command.blocked_issue_codes))
    requires_confirmation = bool(exceeded or new_codes or blocked_codes)
    return OutlineAuthorizationRecalculationDecisionV1(
        chapter_id=command.chapter_id,
        authorization_revision=command.authorization_revision,
        authorized_scope=authorized_scope,
        candidate_scope=candidate_scope,
        exceeded_fields=tuple(exceeded),
        new_acknowledgement_codes=new_codes,
        blocked_issue_codes=blocked_codes,
        expected_narrative_revision=command.expected_narrative_revision,
        status=(
            "confirmation_required"
            if requires_confirmation
            else "narrowed_or_unchanged"
        ),
        requires_confirmation=requires_confirmation,
    )
