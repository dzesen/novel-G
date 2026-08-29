"""Closed, content-free contract for bounded candidate manual takeover.

Only completion failures with a durable incomplete prose checkpoint may enter
this path.  Outline-adherence, state, stale-source, and unknown failures remain
outside the contract and therefore fail closed in their existing paths.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.services.generation.candidate_repair_contracts import (
    MAX_BSON_INT64,
    MAX_CHAPTER_CANDIDATE_PIPELINE_CHECKPOINTS,
    CandidatePipelineCheckpointV1,
    CandidateSourceIdentityV1,
    ProseCandidateCheckpointV1,
    candidate_pipeline_checkpoint_digest,
    candidate_pipeline_checkpoint_ledger_digest,
    is_safe_candidate_identifier,
    parse_candidate_pipeline_checkpoint,
)
from backend.services.generation.stable_reason_codes import (
    normalize_stable_reason_code,
    project_stable_reason_codes,
)


MAX_CANDIDATE_MANUAL_TAKEOVER_EVENTS = 200

_COMPLETION_REPAIR_REASON_CODES = frozenset({
    "below_minimum_word_ratio",
    "finish_reason_cancelled",
    "finish_reason_content_filter",
    "finish_reason_error",
    "finish_reason_length",
    "finish_reason_tool_call",
    "finish_reason_unreported",
    "remediation_verification_required",
    "repair_no_progress",
    "rewrite_output_invalid",
    "rewrite_provider_generation_failed",
    "scene_word_budget_below_minimum",
    "scene_word_budget_exceeded",
    "scene_word_budget_trimmed_without_sentence_boundary",
    "scenes_incomplete",
})
_DIRECT_COMPLETION_STOP_CODES = frozenset({
    "candidate_completion_failed",
    "candidate_completion_repair_exhausted",
    "repair_budget_exhausted",
    "repair_no_progress",
    "repair_not_converged",
})
_TAKEOVER_REASON_CODES = (
    _COMPLETION_REPAIR_REASON_CODES | _DIRECT_COMPLETION_STOP_CODES
)
_MAX_EXCEPTION_CHAIN = 16


class CandidateManualTakeoverV1(BaseModel):
    """Exact failed candidate and authorization snapshot awaiting an author."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["candidate_manual_takeover.v1"]
    novel_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    job_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    chapter_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    readiness_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_revision: int = Field(ge=1, le=MAX_BSON_INT64)
    expected_narrative_revision: int = Field(
        ge=0,
        le=MAX_BSON_INT64 - 1,
    )
    failure_event_id: str = Field(min_length=1, max_length=240)
    source: CandidateSourceIdentityV1
    checkpoint_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_sequence: int = Field(
        ge=1,
        le=MAX_CHAPTER_CANDIDATE_PIPELINE_CHECKPOINTS,
    )
    checkpoint_ledger_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_count: int = Field(
        ge=1,
        le=MAX_CHAPTER_CANDIDATE_PIPELINE_CHECKPOINTS,
    )
    termination_reason_code: Literal[
        "completion_gate_failed",
        "tool_failure_exhausted",
    ]
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=20)
    next_step: Literal["complete_chapter_manually_then_reauthorize"] = (
        "complete_chapter_manually_then_reauthorize"
    )

    @model_validator(mode="after")
    def validate_closed_evidence(self) -> "CandidateManualTakeoverV1":
        if not is_safe_candidate_identifier(
            self.failure_event_id,
            maximum=240,
        ):
            raise ValueError("candidate manual takeover event identity is invalid")
        if (
            len(set(self.reason_codes)) != len(self.reason_codes)
            or any(code not in _TAKEOVER_REASON_CODES for code in self.reason_codes)
        ):
            raise ValueError("candidate manual takeover reason evidence is invalid")
        if self.checkpoint_sequence > self.checkpoint_count:
            raise ValueError("candidate manual takeover checkpoint is invalid")
        return self


class CandidateManualTakeoverResolutionV1(BaseModel):
    """Manual completion proof consumed with the successor readiness CAS."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["candidate_manual_takeover_resolution.v1"]
    takeover: CandidateManualTakeoverV1
    manual_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    narrative_revision: int = Field(ge=1, le=MAX_BSON_INT64)

    @model_validator(mode="after")
    def validate_revision_advanced(self) -> "CandidateManualTakeoverResolutionV1":
        if self.narrative_revision <= self.takeover.expected_narrative_revision:
            raise ValueError(
                "candidate manual takeover narrative revision did not advance"
            )
        return self


def parse_candidate_manual_takeover(value: Any) -> CandidateManualTakeoverV1:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, Mapping):
        value = dict(value)
        reason_codes = value.get("reason_codes")
        if isinstance(reason_codes, list):
            value["reason_codes"] = tuple(reason_codes)
    return CandidateManualTakeoverV1.model_validate(value)


def parse_candidate_manual_takeover_resolution(
    value: Any,
) -> CandidateManualTakeoverResolutionV1:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, Mapping):
        value = dict(value)
        takeover = value.get("takeover")
        if isinstance(takeover, (Mapping, BaseModel)):
            value["takeover"] = parse_candidate_manual_takeover(takeover)
    return CandidateManualTakeoverResolutionV1.model_validate(value)


def _exception_chain(failure: BaseException) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = failure
    while current is not None and len(chain) < _MAX_EXCEPTION_CHAIN:
        identity = id(current)
        if identity in seen:
            break
        seen.add(identity)
        chain.append(current)
        cause = current.__cause__
        if cause is None and not current.__suppress_context__:
            cause = current.__context__
        current = cause
    return tuple(chain)


def _failure_identity(value: BaseException) -> tuple[str, str]:
    kind = type(value)
    return kind.__module__, kind.__name__


def _manual_takeover_failure_evidence(
    chain: Sequence[BaseException],
) -> tuple[str, tuple[str, ...]] | None:
    for item in chain:
        module, name = _failure_identity(item)
        if (
            module
            == "backend.services.generation.chapter_candidate_repairs"
            and name == "CandidateRepairRunStopped"
        ):
            termination = normalize_stable_reason_code(
                getattr(item, "termination_reason_code", None)
            )
            reason_codes = project_stable_reason_codes(
                getattr(item, "reason_codes", ())
            )
            if (
                getattr(item, "repair_trigger", None) == "completion"
                and termination == "tool_failure_exhausted"
                and reason_codes
                and all(
                    code in _COMPLETION_REPAIR_REASON_CODES
                    for code in reason_codes
                )
            ):
                return termination, reason_codes

    for item in chain:
        module, name = _failure_identity(item)
        if (
            module
            == "backend.services.generation.chapter_candidate_pipeline"
            and name == "ChapterCandidatePipelineBlocked"
            and str(getattr(item, "gate", "") or "") == "completion"
        ):
            code = normalize_stable_reason_code(getattr(item, "code", None))
            if code in _DIRECT_COMPLETION_STOP_CODES:
                return "completion_gate_failed", (code,)
    return None


def _failure_source_identity(
    chain: Sequence[BaseException],
) -> CandidateSourceIdentityV1 | None:
    for item in chain:
        progress = getattr(item, "progress", None)
        run_id = getattr(progress, "prose_run_id", None)
        run_revision = getattr(progress, "prose_run_revision", None)
        content_digest = getattr(progress, "prose_content_digest", None)
        try:
            return CandidateSourceIdentityV1(
                schema_version="candidate_source_identity.v1",
                source_run_id=run_id,
                source_run_revision=run_revision,
                source_content_digest=content_digest,
            )
        except (TypeError, ValueError):
            continue
    return None


def validate_candidate_manual_takeover_binding(
    takeover: CandidateManualTakeoverV1 | Mapping[str, Any],
    checkpoints: Sequence[Any],
) -> tuple[CandidatePipelineCheckpointV1, ...]:
    """Reprove an active takeover against its complete ordered checkpoint tail."""

    binding = parse_candidate_manual_takeover(takeover)
    if (
        isinstance(checkpoints, (str, bytes))
        or not isinstance(checkpoints, Sequence)
        or not 1 <= len(checkpoints) <= MAX_CHAPTER_CANDIDATE_PIPELINE_CHECKPOINTS
    ):
        raise ValueError("candidate manual takeover checkpoint ledger is invalid")
    parsed = tuple(
        parse_candidate_pipeline_checkpoint(checkpoint)
        for checkpoint in checkpoints
    )
    if any(
        checkpoint.chapter_id != binding.chapter_id
        or checkpoint.sequence != sequence
        for sequence, checkpoint in enumerate(parsed, start=1)
    ) or len({checkpoint.checkpoint_id for checkpoint in parsed}) != len(parsed):
        raise ValueError("candidate manual takeover checkpoint ledger is invalid")
    matching = [
        checkpoint
        for checkpoint in parsed
        if isinstance(checkpoint, ProseCandidateCheckpointV1)
        and checkpoint.checkpoint_id == binding.checkpoint_id
    ]
    if len(matching) != 1:
        raise ValueError("candidate manual takeover prose checkpoint is missing")
    prose = matching[0]
    if (
        prose.sequence != binding.checkpoint_sequence
        or prose.source != binding.source
        or prose.completion.status != "incomplete"
        or prose.completion.can_write_formal_prose
        or candidate_pipeline_checkpoint_digest(prose)
        != binding.checkpoint_digest
        or len(parsed) != binding.checkpoint_count
        or candidate_pipeline_checkpoint_ledger_digest(parsed)
        != binding.checkpoint_ledger_digest
    ):
        raise ValueError("candidate manual takeover checkpoint binding is stale")
    return parsed


def project_candidate_manual_takeover(
    failure: BaseException,
    *,
    novel_id: str,
    job_id: str,
    chapter_id: str,
    readiness_digest: str,
    authorization_revision: int,
    expected_narrative_revision: int,
    failure_event_id: str,
    checkpoints: Sequence[Any],
) -> CandidateManualTakeoverV1 | None:
    """Return a takeover only for a proven completion-only terminal failure."""

    chain = _exception_chain(failure)
    evidence = _manual_takeover_failure_evidence(chain)
    source = _failure_source_identity(chain)
    if evidence is None or source is None:
        return None
    try:
        parsed = tuple(
            parse_candidate_pipeline_checkpoint(checkpoint)
            for checkpoint in checkpoints
        )
    except (TypeError, ValueError):
        return None
    matching = [
        checkpoint
        for checkpoint in parsed
        if isinstance(checkpoint, ProseCandidateCheckpointV1)
        and checkpoint.chapter_id == chapter_id
        and checkpoint.source == source
        and checkpoint.completion.status == "incomplete"
        and not checkpoint.completion.can_write_formal_prose
    ]
    if not matching:
        return None
    prose = matching[-1]
    termination_reason_code, reason_codes = evidence
    try:
        takeover = CandidateManualTakeoverV1(
            schema_version="candidate_manual_takeover.v1",
            novel_id=novel_id,
            job_id=job_id,
            chapter_id=chapter_id,
            readiness_digest=readiness_digest,
            authorization_revision=authorization_revision,
            expected_narrative_revision=expected_narrative_revision,
            failure_event_id=failure_event_id,
            source=source,
            checkpoint_id=prose.checkpoint_id,
            checkpoint_digest=candidate_pipeline_checkpoint_digest(prose),
            checkpoint_sequence=prose.sequence,
            checkpoint_ledger_digest=(
                candidate_pipeline_checkpoint_ledger_digest(parsed)
            ),
            checkpoint_count=len(parsed),
            termination_reason_code=termination_reason_code,
            reason_codes=reason_codes,
        )
        validate_candidate_manual_takeover_binding(takeover, parsed)
        return takeover
    except (TypeError, ValueError):
        return None
