"""Versioned, candidate-only rewrite contracts for ADR-0008.

These snapshots are not a production readiness builder or an authorization to
spend. The Job must already contain the exact opt-in snapshot before use.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.services.agent_runtime.contracts import AgentRuntimeLimits, RuntimeToolReference
from backend.services.generation.independent_outline_review import IndependentReviewPlan
from backend.services.generation.outline_adherence import OutlineIssueCategoryValue
from backend.services.generation.required_adherence_capacity import RequiredAdherenceCapacity
from backend.services.llm.generation_runtime import GenerationPlan


REQUIRED_REWRITE_SCOPE = "chapter_prose_required_rewrite"
REQUIRED_REWRITE_TOOL = RuntimeToolReference(name="rewrite_prose_scene_candidate", version=2)
REQUIRED_REWRITE_FINISH = "candidate_awaiting_adherence"
REQUIRED_REWRITE_RESULT = "prose_candidate_awaiting_adherence"
REQUIRED_REWRITE_STEP_PREFIX = "required-prose-rewrite:"
PLANNER_INPUT_BOUND = 120_000
REWRITE_INPUT_BOUND = 600_000


def contract_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False, default=str,
    ).encode("utf-8")).hexdigest()


class ClosedRewriteModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class RequiredProseRewriteRequest(ClosedRewriteModel):
    source_run_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    source_revision: int = Field(ge=1, le=2**63 - 2)
    source_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    issue_categories: tuple[OutlineIssueCategoryValue, ...] = Field(min_length=1, max_length=20)
    scene_indexes: tuple[int, ...] = Field(min_length=1, max_length=20)

    def tool_arguments(self) -> dict:
        from backend.services.generation.prose_remediation_runtime import RewriteProseCandidateInput

        return RewriteProseCandidateInput(
            expected_revision=self.source_revision,
            expected_content_digest=self.source_content_digest,
            issue_categories=self.issue_categories, scene_indexes=self.scene_indexes,
        ).model_dump(mode="json")


class RequiredRewriteCallBound(ClosedRewriteModel):
    provider_alias: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=240)
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_attempts: int = Field(ge=1, le=2)
    input_tokens: int = Field(ge=1, le=600_000)
    output_tokens: int = Field(ge=1, le=1_000_000)
    timeout_seconds: int = Field(ge=1, le=86_400)

    @property
    def tokens(self) -> int:
        return self.max_attempts * (self.input_tokens + self.output_tokens)

    @property
    def seconds(self) -> int:
        return self.max_attempts * self.timeout_seconds


class RequiredRewriteAuthorization(ClosedRewriteModel):
    schema_version: Literal["required_prose_rewrite_authorization.v1", "required_prose_rewrite_authorization.v2"]
    protocol_revision: Literal["required-prose-rewrite-r1", "required-prose-rewrite-r2"]
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    planner: RequiredRewriteCallBound | None
    rewrite: RequiredRewriteCallBound
    review_capacity: RequiredAdherenceCapacity
    max_rewrites: Literal[2] = 2
    max_planner_calls: Literal[2] = 2
    max_tool_calls: Literal[1] = 1
    max_tool_retries: Literal[0] = 0
    max_planner_repairs: Literal[0] = 0

    @model_validator(mode="after")
    def validate_dispatch_version(self) -> "RequiredRewriteAuthorization":
        expected = (
            ("required_prose_rewrite_authorization.v1", "required-prose-rewrite-r1")
            if self.planner is not None else
            ("required_prose_rewrite_authorization.v2", "required-prose-rewrite-r2")
        )
        if (self.schema_version, self.protocol_revision) != expected:
            raise ValueError("rewrite_dispatch_version_mismatch")
        return self

    @property
    def attempts(self) -> int:
        return (2 * self.planner.max_attempts if self.planner is not None else 0) + self.rewrite.max_attempts

    @property
    def tokens(self) -> int:
        return (2 * self.planner.tokens if self.planner is not None else 0) + self.rewrite.tokens

    @property
    def seconds(self) -> int:
        return (2 * self.planner.seconds if self.planner is not None else 0) + self.rewrite.seconds

    def runtime_limits(self) -> AgentRuntimeLimits:
        return AgentRuntimeLimits(
            max_steps=2, max_planner_calls=2, max_tool_calls=1,
            max_paid_attempts=self.attempts, token_budget=self.tokens,
            deadline_seconds=self.seconds, max_predispatch_retries=0,
            max_planner_repairs=0, max_tool_retries=0,
        )


@dataclass(frozen=True)
class RequiredProseRewritePlan:
    planner: GenerationPlan | None
    rewrite: GenerationPlan
    review: IndependentReviewPlan

    def authorization(self) -> dict:
        # No hidden format reviewer/fallback route is part of this contract.
        calls = []
        for plan, input_bound in ((self.planner, PLANNER_INPUT_BOUND), (self.rewrite, REWRITE_INPUT_BOUND)):
            if plan is None:
                calls.append(None)
                continue
            if (
                plan.mode not in {"prompt_json", "json_object"}
                or plan.reviewer_alias is not None
                or type(plan.max_semantic_attempts) is not int
                or not 1 <= plan.max_semantic_attempts <= 2
            ):
                raise ValueError("rewrite_plan_unsupported")
            calls.append(RequiredRewriteCallBound(
                provider_alias=plan.provider_alias, model=plan.provider_model,
                revision=contract_digest(asdict(plan)), max_attempts=plan.max_semantic_attempts,
                input_tokens=input_bound, output_tokens=plan.max_output_tokens,
                timeout_seconds=plan.timeout_seconds,
            ))
        if self.review.writer_model != self.rewrite.provider_model:
            raise ValueError("rewrite_writer_model_mismatch")
        values = {
            "schema_version": "required_prose_rewrite_authorization.v1" if self.planner is not None else "required_prose_rewrite_authorization.v2",
            "protocol_revision": "required-prose-rewrite-r1" if self.planner is not None else "required-prose-rewrite-r2",
            "planner": calls[0], "rewrite": calls[1],
            "review_capacity": RequiredAdherenceCapacity.from_plan(self.review),
        }
        identity = {key: value.model_dump(mode="json") if isinstance(value, BaseModel) else value for key, value in values.items()}
        return RequiredRewriteAuthorization(
            **values, contract_digest=contract_digest(identity),
        ).model_dump(mode="json")


class RequiredRewriteOrigin(ClosedRewriteModel):
    schema_version: Literal["required_prose_rewrite_origin.v1"] = "required_prose_rewrite_origin.v1"
    job_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    readiness_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_revision: int = Field(ge=1)
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    rewrite_ordinal: int = Field(ge=1, le=2)
    request: RequiredProseRewriteRequest


class RequiredRewriteCandidateOrigin(RequiredRewriteOrigin):
    agent_run_id: str = Field(pattern=r"^[0-9a-f]{24}$")
