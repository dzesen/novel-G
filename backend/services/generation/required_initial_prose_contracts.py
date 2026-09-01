"""Frozen initial-prose authority and source identity for ADR-0008."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from backend.services.generation.prose_completion import (
    ProseExecutionPlan,
    prose_completion_module,
)
from backend.services.llm.generation_runtime import (
    GenerationPlan,
    WorkflowStepTarget,
)


REQUIRED_INITIAL_PROSE_STEP_PREFIX = "required-initial-prose:"
INITIAL_PROSE_INPUT_BOUND = 600_000
PROSE_WORKFLOW = "write_chapter_by_ai"
PROSE_STEP = "chapter_content"


def stable_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")).hexdigest()


class _Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class RequiredInitialProseAuthorization(_Closed):
    schema_version: Literal["required_initial_prose_authorization.v1"]
    protocol_revision: Literal["required-initial-prose-r1"]
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_alias: str = Field(min_length=1, max_length=64)
    provider_model: str = Field(min_length=1, max_length=240)
    generation_plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_calls: int = Field(ge=1, le=100)
    input_tokens_per_call: Literal[600000] = INITIAL_PROSE_INPUT_BOUND
    output_tokens_per_call: int = Field(ge=1, le=1_000_000)
    timeout_seconds_per_call: int = Field(ge=1, le=86_400)
    automatic_continuations_per_scene: Literal[0] = 0

    @property
    def token_bound(self) -> int:
        return self.max_calls * (
            self.input_tokens_per_call + self.output_tokens_per_call
        )

    @property
    def serial_seconds(self) -> int:
        return self.max_calls * self.timeout_seconds_per_call


class RequiredInitialProseOrigin(_Closed):
    schema_version: Literal["required_initial_prose_origin.v1"] = (
        "required_initial_prose_origin.v1"
    )
    job_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    readiness_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_revision: int = Field(ge=1, le=2**63 - 1)
    narrative_revision: int = Field(ge=0, le=2**63 - 1)
    chapter_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    outline_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation_plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewedInitialProseSource(_Closed):
    """Exact locked source admitted by a settled independent repair decision."""

    schema_version: Literal["reviewed_initial_prose_source.v1"] = (
        "reviewed_initial_prose_source.v1"
    )
    origin: RequiredInitialProseOrigin
    run_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    run_revision: int = Field(ge=2, le=2**63 - 1)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_checkpoint_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def required_initial_execution_plan(
    generation: GenerationPlan,
    outline: Mapping[str, Any],
) -> ProseExecutionPlan:
    target = generation.target
    if (
        not isinstance(target, WorkflowStepTarget)
        or target.workflow_name != PROSE_WORKFLOW
        or target.step_name != PROSE_STEP
        or generation.reviewer_alias is not None
        or generation.max_output_tokens is None
        or generation.timeout_seconds is None
        or generation.max_semantic_attempts != 1
    ):
        raise ValueError("initial_prose_generation_plan_unsupported")
    target_words = outline.get("target_word_count")
    if isinstance(target_words, bool) or not isinstance(target_words, int):
        raise ValueError("initial_prose_outline_invalid")
    return prose_completion_module.plan(
        outline=dict(outline),
        target_word_count=target_words,
        provider_capability={
            "max_output_tokens": generation.max_output_tokens,
            "model": generation.provider_model,
        },
        request_overrides=None,
    )


def build_required_initial_prose_authorization(
    generation: GenerationPlan,
    outline: Mapping[str, Any],
) -> RequiredInitialProseAuthorization:
    execution = required_initial_execution_plan(generation, outline)
    identity = {
        "schema_version": "required_initial_prose_authorization.v1",
        "protocol_revision": "required-initial-prose-r1",
        "provider_alias": generation.provider_alias,
        "provider_model": generation.provider_model,
        "generation_plan_digest": stable_digest(asdict(generation)),
        "execution_plan_digest": stable_digest(execution.to_dict()),
        "max_calls": execution.scheduled_base_call_count,
        "input_tokens_per_call": INITIAL_PROSE_INPUT_BOUND,
        "output_tokens_per_call": generation.max_output_tokens,
        "timeout_seconds_per_call": generation.timeout_seconds,
        "automatic_continuations_per_scene": 0,
    }
    return RequiredInitialProseAuthorization(
        **identity,
        contract_digest=stable_digest(identity),
    )


def build_required_initial_prose_origin(
    *,
    job_id: str,
    readiness_digest: str,
    authorization_revision: int,
    narrative_revision: int,
    chapter_id: str,
    outline_revision: str,
    authorization: RequiredInitialProseAuthorization,
) -> RequiredInitialProseOrigin:
    request_identity = {
        "job_id": job_id,
        "readiness_digest": readiness_digest,
        "authorization_revision": authorization_revision,
        "narrative_revision": narrative_revision,
        "chapter_id": chapter_id,
        "outline_revision": outline_revision,
        "contract_digest": authorization.contract_digest,
        "generation_plan_digest": authorization.generation_plan_digest,
        "execution_plan_digest": authorization.execution_plan_digest,
    }
    return RequiredInitialProseOrigin(
        **request_identity,
        request_digest=stable_digest(request_identity),
    )
