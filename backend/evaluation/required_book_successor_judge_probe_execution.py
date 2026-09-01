"""Authorized execution seam for the successor independent-Judge probe.

The readiness builder remains zero-call.  This Module is the only layer that
may consume its two explicit acknowledgements, run the exact frozen synthetic
review through ``GenerationRuntime``, and mint a redacted receipt.  The caller
owns the durable AttemptScope used by the runtime; an in-memory scope is only
appropriate for tests.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING
import json
from typing import Any, Callable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.services.generation.independent_outline_review import (
    IndependentOutlineReviewer,
    IndependentReviewPlan,
)
from backend.services.llm.generation_runtime import (
    AttemptUsage,
    GenerationRuntime,
)
from backend.evaluation.required_book_successor_judge_probe import (
    JUDGE_PROBE_RECEIPT_VALIDITY_SECONDS,
    REQUIRED_JUDGE_PROBE_AUTHORIZATION_CODES,
    RequiredJudgeCapabilityProbeAuthorization,
    RequiredJudgeCapabilityProbeReceipt,
    parse_required_judge_capability_probe_receipt,
    required_judge_generation_plan_digest,
    required_judge_probe_digest,
    required_judge_probe_sample_digest,
    required_judge_probe_snapshot,
    validate_required_judge_capability_probe_readiness,
)


_SHA256 = r"^[0-9a-f]{64}$"
_MAX = 2**63 - 1
_PRICE_QUANTUM = Decimal("0.000001")
REQUIRED_JUDGE_PROBE_RECEIPT_VALIDITY = timedelta(
    seconds=JUDGE_PROBE_RECEIPT_VALIDITY_SECONDS
)

RequiredJudgeProbeFailureCode = Literal[
    "review_uncertain",
    "review_plan_stale",
    "review_not_natural_end",
    "review_evidence_invalid",
    "review_response_limit",
    "review_budget_exhausted",
    "review_accounting_invalid",
    "review_generation_failed",
    "review_dispatch_rejected",
    "probe_deadline_exceeded",
    "probe_evidence_not_pass",
    "probe_accounting_invalid",
]


class RequiredJudgeCapabilityProbeExecutionConflict(ValueError):
    """Execution authority, time window, or runtime state is not usable."""


class _Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class RequiredJudgeProbeAttemptEvidence(_Closed):
    schema_version: Literal["required_judge_probe_attempt.v1"] = (
        "required_judge_probe_attempt.v1"
    )
    attempt_id_digest: str = Field(pattern=_SHA256)
    provider_alias: str = Field(min_length=1, max_length=160)
    phase: Literal["primary", "repair"]
    input_tokens: int = Field(ge=1, le=_MAX)
    output_tokens: int = Field(ge=1, le=_MAX)
    total_tokens: int = Field(ge=2, le=_MAX)

    @model_validator(mode="after")
    def validate_usage(self) -> "RequiredJudgeProbeAttemptEvidence":
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("required_judge_probe_attempt_usage_invalid")
        return self


class RequiredJudgeCapabilityProbeReport(_Closed):
    schema_version: Literal[
        "required_judge_capability_probe_report.v1"
    ] = "required_judge_capability_probe_report.v1"
    report_digest: str = Field(pattern=_SHA256)
    probe_readiness_digest: str = Field(pattern=_SHA256)
    authorization_contract_digest: str = Field(pattern=_SHA256)
    generation_plan_digest: str = Field(pattern=_SHA256)
    review_contract_digest: str = Field(pattern=_SHA256)
    synthetic_sample_digest: str = Field(pattern=_SHA256)
    provider_alias: str = Field(min_length=1, max_length=160)
    provider_model: str = Field(min_length=1, max_length=240)
    started_at: datetime
    completed_at: datetime
    status: Literal["passed", "failed"]
    failure_code: RequiredJudgeProbeFailureCode | None = None
    attempts: tuple[RequiredJudgeProbeAttemptEvidence, ...] = Field(
        max_length=2,
    )
    accounted_attempt_count: int = Field(ge=0, le=2)
    uncertain_attempt_count: int = Field(ge=0, le=2)
    input_tokens: int = Field(ge=0, le=_MAX)
    output_tokens: int = Field(ge=0, le=_MAX)
    total_tokens: int = Field(ge=0, le=_MAX)
    currency: str = Field(min_length=1, max_length=16)
    cost_upper_bound: Decimal = Field(ge=0)
    evidence_digest: str | None = Field(default=None, pattern=_SHA256)
    evidence_decision: Literal["pass"] | None = None
    finish_reason: Literal["stop"] | None = None
    usage_complete: bool
    synthetic_material_only: Literal[True] = True
    contains_user_material: Literal[False] = False

    @model_validator(mode="after")
    def validate_report(self) -> "RequiredJudgeCapabilityProbeReport":
        if (
            self.started_at.tzinfo is None
            or self.completed_at.tzinfo is None
            or self.completed_at < self.started_at
            or self.accounted_attempt_count != len(self.attempts)
            or self.input_tokens
            != sum(item.input_tokens for item in self.attempts)
            or self.output_tokens
            != sum(item.output_tokens for item in self.attempts)
            or self.total_tokens != self.input_tokens + self.output_tokens
            or (
                self.status == "passed"
                and (
                    self.failure_code is not None
                    or not self.attempts
                    or self.uncertain_attempt_count != 0
                    or self.evidence_digest is None
                    or self.evidence_decision != "pass"
                    or self.finish_reason != "stop"
                    or self.usage_complete is not True
                )
            )
            or (
                self.status == "failed"
                and (
                    self.failure_code is None
                    or self.evidence_digest is not None
                    or self.evidence_decision is not None
                    or self.finish_reason is not None
                )
            )
        ):
            raise ValueError("required_judge_probe_report_invalid")
        identity = self.model_dump(mode="python", exclude={"report_digest"})
        if required_judge_probe_digest(identity) != self.report_digest:
            raise ValueError("required_judge_probe_report_changed")
        return self


@dataclass(frozen=True)
class RequiredJudgeCapabilityProbeExecution:
    report: RequiredJudgeCapabilityProbeReport
    receipt: RequiredJudgeCapabilityProbeReceipt | None


def _cost_upper_bound(
    *,
    authorization: RequiredJudgeCapabilityProbeAuthorization,
    input_tokens: int,
    output_tokens: int,
    uncertain_attempt_count: int,
) -> Decimal:
    if uncertain_attempt_count:
        return authorization.cost_upper_bound
    amount = (
        Decimal(input_tokens)
        * authorization.pricing.input_cache_miss_per_million
        + Decimal(output_tokens) * authorization.pricing.output_per_million
    ) / Decimal(1_000_000)
    return amount.quantize(_PRICE_QUANTUM, rounding=ROUND_CEILING)


def _attempt_evidence(
    attempts: tuple[AttemptUsage, ...],
) -> tuple[RequiredJudgeProbeAttemptEvidence, ...]:
    evidence: list[RequiredJudgeProbeAttemptEvidence] = []
    seen: set[str] = set()
    for attempt in attempts:
        usage = attempt.usage
        if (
            not attempt.attempt_id
            or attempt.attempt_id in seen
            or attempt.state != "accounted"
            or attempt.phase not in {"primary", "repair"}
            or usage.input_tokens <= 0
            or usage.output_tokens <= 0
            or usage.total_tokens
            != usage.input_tokens + usage.output_tokens
        ):
            raise ValueError("required_judge_probe_attempt_evidence_invalid")
        seen.add(attempt.attempt_id)
        evidence.append(
            RequiredJudgeProbeAttemptEvidence(
                attempt_id_digest=required_judge_probe_digest(
                    {"attempt_id": attempt.attempt_id}
                ),
                provider_alias=attempt.provider_alias,
                phase=attempt.phase,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
            )
        )
    return tuple(evidence)


def _attempt_set_digest(
    attempts: tuple[RequiredJudgeProbeAttemptEvidence, ...],
) -> str:
    return required_judge_probe_digest(
        [item.model_dump(mode="json") for item in attempts]
    )


def _build_report(
    *,
    authorization: RequiredJudgeCapabilityProbeAuthorization,
    readiness_digest: str,
    started_at: datetime,
    completed_at: datetime,
    status: Literal["passed", "failed"],
    failure_code: RequiredJudgeProbeFailureCode | None,
    attempts: tuple[RequiredJudgeProbeAttemptEvidence, ...],
    uncertain_attempt_count: int,
    evidence: Mapping[str, Any] | None,
) -> RequiredJudgeCapabilityProbeReport:
    input_tokens = sum(item.input_tokens for item in attempts)
    output_tokens = sum(item.output_tokens for item in attempts)
    identity = {
        "schema_version": "required_judge_capability_probe_report.v1",
        "probe_readiness_digest": readiness_digest,
        "authorization_contract_digest": authorization.contract_digest,
        "generation_plan_digest": required_judge_generation_plan_digest(
            authorization.generation_plan
        ),
        "review_contract_digest": authorization.review_contract_digest,
        "synthetic_sample_digest": authorization.synthetic_sample_digest,
        "provider_alias": authorization.generation_plan.provider_alias,
        "provider_model": authorization.generation_plan.provider_model,
        "started_at": started_at,
        "completed_at": completed_at,
        "status": status,
        "failure_code": failure_code,
        "attempts": attempts,
        "accounted_attempt_count": len(attempts),
        "uncertain_attempt_count": uncertain_attempt_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "currency": authorization.pricing.currency,
        "cost_upper_bound": _cost_upper_bound(
            authorization=authorization,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            uncertain_attempt_count=uncertain_attempt_count,
        ),
        "evidence_digest": (
            required_judge_probe_digest(evidence)
            if status == "passed" and evidence is not None
            else None
        ),
        "evidence_decision": "pass" if status == "passed" else None,
        "finish_reason": "stop" if status == "passed" else None,
        "usage_complete": status == "passed",
        "synthetic_material_only": True,
        "contains_user_material": False,
    }
    return RequiredJudgeCapabilityProbeReport(
        **identity,
        report_digest=required_judge_probe_digest(identity),
    )


def parse_required_judge_capability_probe_report(
    value: Any,
) -> RequiredJudgeCapabilityProbeReport:
    try:
        return RequiredJudgeCapabilityProbeReport.model_validate_json(
            json.dumps(
                value.model_dump(mode="json")
                if isinstance(value, BaseModel)
                else value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("required_judge_probe_report_invalid") from exc


def validate_required_judge_capability_probe_report(
    value: Any,
    *,
    readiness: Mapping[str, Any],
) -> RequiredJudgeCapabilityProbeReport:
    authorization = validate_required_judge_capability_probe_readiness(
        readiness
    )
    report = parse_required_judge_capability_probe_report(value)
    if (
        report.probe_readiness_digest != readiness.get("digest")
        or report.authorization_contract_digest
        != authorization.contract_digest
        or report.generation_plan_digest
        != required_judge_generation_plan_digest(
            authorization.generation_plan
        )
        or report.review_contract_digest
        != authorization.review_contract_digest
        or report.synthetic_sample_digest
        != required_judge_probe_sample_digest()
        or report.provider_alias
        != authorization.generation_plan.provider_alias
        or report.provider_model
        != authorization.generation_plan.provider_model
        or report.started_at < authorization.created_at
        or report.accounted_attempt_count
        > authorization.maximum_provider_attempts
        or report.uncertain_attempt_count
        > authorization.maximum_provider_attempts
        - report.accounted_attempt_count
        or report.input_tokens > authorization.maximum_input_tokens
        or report.output_tokens > authorization.maximum_output_tokens
        or report.total_tokens > authorization.maximum_total_tokens
        or report.currency != authorization.pricing.currency
        or report.cost_upper_bound
        != _cost_upper_bound(
            authorization=authorization,
            input_tokens=report.input_tokens,
            output_tokens=report.output_tokens,
            uncertain_attempt_count=report.uncertain_attempt_count,
        )
        or (
            report.status == "passed"
            and report.completed_at > authorization.deadline_at
        )
    ):
        raise ValueError("required_judge_probe_report_binding_changed")
    return report


def validate_required_judge_capability_probe_execution(
    value: RequiredJudgeCapabilityProbeExecution,
    *,
    readiness: Mapping[str, Any],
) -> RequiredJudgeCapabilityProbeExecution:
    if not isinstance(value, RequiredJudgeCapabilityProbeExecution):
        raise ValueError("required_judge_probe_execution_invalid")
    authorization = validate_required_judge_capability_probe_readiness(
        readiness
    )
    report = validate_required_judge_capability_probe_report(
        value.report,
        readiness=readiness,
    )
    receipt = (
        parse_required_judge_capability_probe_receipt(value.receipt)
        if value.receipt is not None
        else None
    )
    if (
        (report.status == "passed") != (receipt is not None)
        or (
            receipt is not None
            and (
                receipt.probe_readiness_digest != readiness.get("digest")
                or receipt.probe_report_sha256 != report.report_digest
                or receipt.synthetic_sample_digest
                != authorization.synthetic_sample_digest
                or receipt.provider_alias
                != authorization.generation_plan.provider_alias
                or receipt.provider_model
                != authorization.generation_plan.provider_model
                or receipt.generation_plan_digest
                != report.generation_plan_digest
                or receipt.review_contract_digest
                != authorization.review_contract_digest
                or receipt.completed_at != report.completed_at
                or receipt.valid_until
                != report.completed_at
                + timedelta(seconds=authorization.receipt_validity_seconds)
                or receipt.accounted_attempt_count
                != report.accounted_attempt_count
                or receipt.uncertain_attempt_count != 0
                or receipt.input_tokens != report.input_tokens
                or receipt.output_tokens != report.output_tokens
                or receipt.total_tokens != report.total_tokens
            )
        )
    ):
        raise ValueError("required_judge_probe_execution_binding_changed")
    return RequiredJudgeCapabilityProbeExecution(report, receipt)


class RequiredJudgeCapabilityProbeRunner:
    """Run one already-authorized synthetic review against the exact plan."""

    def __init__(
        self,
        runtime: GenerationRuntime,
        *,
        now_supplier: Callable[[], datetime],
        attempt_scope: Any | None = None,
    ) -> None:
        if (
            attempt_scope is not None
            and getattr(runtime, "_attempt_scope", None) is not attempt_scope
        ):
            raise ValueError("required judge probe runtime/scope mismatch")
        self._runtime = runtime
        self._now_supplier = now_supplier
        self._attempt_scope = attempt_scope

    async def run(
        self,
        readiness: Mapping[str, Any],
        *,
        acknowledged_authorization_codes: tuple[str, ...],
    ) -> RequiredJudgeCapabilityProbeExecution:
        authorization = validate_required_judge_capability_probe_readiness(
            readiness
        )
        if (
            acknowledged_authorization_codes
            != REQUIRED_JUDGE_PROBE_AUTHORIZATION_CODES
        ):
            raise RequiredJudgeCapabilityProbeExecutionConflict(
                "required_judge_probe_authorization_missing"
            )
        terminal = (
            getattr(self._attempt_scope, "terminal_execution", None)
            if self._attempt_scope is not None
            else None
        )
        if terminal is not None:
            return validate_required_judge_capability_probe_execution(
                terminal,
                readiness=readiness,
            )
        started_at = self._now_supplier()
        if (
            started_at.tzinfo is None
            or started_at < authorization.created_at
            or started_at > authorization.deadline_at
        ):
            raise RequiredJudgeCapabilityProbeExecutionConflict(
                "required_judge_probe_authorization_expired"
            )
        if (
            self._runtime.claimed_attempt_count
            or self._runtime.uncertain_attempt_count
            or self._runtime.attempts
            or self._runtime.attempt_evidence_errors
        ):
            raise RequiredJudgeCapabilityProbeExecutionConflict(
                "required_judge_probe_runtime_not_fresh"
            )
        plan = IndependentReviewPlan(
            generation=authorization.generation_plan.thaw(),
            writer_model=authorization.writer_model,
            input_token_bound=authorization.review_input_token_bound,
            max_response_bytes=authorization.review_max_response_bytes,
        )
        if plan.contract_digest != authorization.review_contract_digest:
            raise RequiredJudgeCapabilityProbeExecutionConflict(
                "required_judge_probe_review_contract_changed"
            )

        result = await IndependentOutlineReviewer(self._runtime).review(
            required_judge_probe_snapshot(),
            plan,
        )
        completed_at = self._now_supplier()
        uncertain = self._runtime.uncertain_attempt_count
        try:
            attempts = _attempt_evidence(result.attempts)
        except ValueError:
            attempts = ()
            failure_code: RequiredJudgeProbeFailureCode = (
                "probe_accounting_invalid"
            )
        else:
            if uncertain:
                failure_code = "review_uncertain"
            elif self._runtime.attempt_evidence_errors:
                failure_code = "probe_accounting_invalid"
            elif (
                self._attempt_scope is not None
                and getattr(self._attempt_scope, "usage_complete", True)
                is not True
            ):
                failure_code = "probe_accounting_invalid"
            elif self._runtime.claimed_attempt_count != len(attempts):
                failure_code = "probe_accounting_invalid"
            elif completed_at > authorization.deadline_at:
                failure_code = "probe_deadline_exceeded"
            elif result.failure_code is not None:
                failure_code = result.failure_code
            elif (
                result.evidence is None
                or result.evidence.get("decision") != "pass"
            ):
                failure_code = "probe_evidence_not_pass"
            else:
                failure_code = None

        passed = failure_code is None
        report = _build_report(
            authorization=authorization,
            readiness_digest=str(readiness["digest"]),
            started_at=started_at,
            completed_at=completed_at,
            status="passed" if passed else "failed",
            failure_code=failure_code,
            attempts=attempts,
            uncertain_attempt_count=uncertain,
            evidence=result.evidence if passed else None,
        )
        validate_required_judge_capability_probe_report(
            report,
            readiness=readiness,
        )
        receipt = (
            RequiredJudgeCapabilityProbeReceipt.create(
                probe_readiness_digest=str(readiness["digest"]),
                probe_report_sha256=report.report_digest,
                generation_plan=authorization.generation_plan,
                review_contract_digest=authorization.review_contract_digest,
                completed_at=completed_at,
                valid_until=(
                    completed_at
                    + timedelta(
                        seconds=authorization.receipt_validity_seconds
                    )
                ),
                accounted_attempt_count=len(attempts),
                accounted_attempt_ids_digest=_attempt_set_digest(attempts),
                input_tokens=report.input_tokens,
                output_tokens=report.output_tokens,
            )
            if passed
            else None
        )
        execution = validate_required_judge_capability_probe_execution(
            RequiredJudgeCapabilityProbeExecution(report, receipt),
            readiness=readiness,
        )
        publish = getattr(self._attempt_scope, "publish_execution", None)
        if callable(publish):
            await publish(execution)
        return execution


__all__ = [
    "REQUIRED_JUDGE_PROBE_RECEIPT_VALIDITY",
    "RequiredJudgeCapabilityProbeExecution",
    "RequiredJudgeCapabilityProbeExecutionConflict",
    "RequiredJudgeCapabilityProbeReport",
    "RequiredJudgeCapabilityProbeRunner",
    "RequiredJudgeProbeAttemptEvidence",
    "parse_required_judge_capability_probe_report",
    "validate_required_judge_capability_probe_report",
    "validate_required_judge_capability_probe_execution",
]
