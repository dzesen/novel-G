"""Pure stop-loss and evidence ledger for successor issue #20 acceptance.

The Module owns the ordering, two-run limit, exact attempt identities, budget
reconciliation, recovery requirement and cleanup-dependent outcome.  It does
not choose where journals are stored and does not call a Provider or database.
Production and in-memory adapters therefore share the same small Interface:
``claim`` / ``advance`` / ``inspect``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
import json
from typing import Annotated, Any, Literal, Mapping, Sequence, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.evaluation.required_book_successor_acceptance import (
    REQUIRED_AUTHORIZATION_CODES,
    RequiredBookSuccessorAcceptanceAuthorization,
    validate_current_required_book_successor_acceptance_authorization,
    validate_required_book_successor_acceptance_outline,
    validate_required_book_successor_acceptance_readiness,
    validate_required_book_successor_acceptance_root_readiness,
)
from backend.services.generation.prose_runs import prose_revision
from backend.services.generation.required_book_successor import (
    required_book_successor_digest,
)


_SHA256 = r"^[0-9a-f]{64}$"
_OBJECT_ID = r"^[0-9a-f]{24}$"
_MAX = 2**63 - 1
_PRICE_QUANTUM = Decimal("0.000001")


class SuccessorAcceptanceLedgerConflict(ValueError):
    """A persisted run, event sequence or budget no longer matches authority."""


class _Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class SuccessorAcceptanceAttemptEvidence(_Closed):
    schema_version: Literal["successor_acceptance_attempt.v1"] = (
        "successor_acceptance_attempt.v1"
    )
    attempt_digest: str = Field(pattern=_SHA256)
    provider_alias: str = Field(min_length=1, max_length=160)
    provider_model: str = Field(min_length=1, max_length=240)
    state: Literal["accounted", "uncertain", "released_pre_dispatch"]
    usage_basis: Literal[
        "provider_receipt",
        "conservative_upper_bound",
        "not_dispatched",
    ]
    input_tokens: int = Field(ge=0, le=_MAX)
    output_tokens: int = Field(ge=0, le=_MAX)
    total_tokens: int = Field(ge=0, le=_MAX)

    @model_validator(mode="after")
    def validate_usage(self) -> "SuccessorAcceptanceAttemptEvidence":
        if (
            self.total_tokens != self.input_tokens + self.output_tokens
            or (
                self.state == "released_pre_dispatch"
                and (
                    self.usage_basis != "not_dispatched"
                    or self.total_tokens != 0
                )
            )
            or (
                self.state != "released_pre_dispatch"
                and self.usage_basis == "not_dispatched"
            )
            or (
                self.state == "uncertain"
                and self.usage_basis != "conservative_upper_bound"
            )
        ):
            raise ValueError("successor_acceptance_attempt_usage_invalid")
        return self


class SuccessorAcceptanceRunClaim(_Closed):
    schema_version: Literal["successor_acceptance_run_claim.v1"] = (
        "successor_acceptance_run_claim.v1"
    )
    claim_digest: str = Field(pattern=_SHA256)
    sample_id: Literal["successor-representative-3000-v1"]
    ordinal: int = Field(ge=1, le=2)
    owner_id: str = Field(pattern=_OBJECT_ID)
    readiness_digest: str = Field(pattern=_SHA256)
    authorization_contract_digest: str = Field(pattern=_SHA256)
    acknowledged_authorization_codes: tuple[str, str, str]
    claimed_at: datetime
    deadline_at: datetime

    @model_validator(mode="after")
    def validate_claim(self) -> "SuccessorAcceptanceRunClaim":
        identity = self.model_dump(mode="python", exclude={"claim_digest"})
        if (
            self.claimed_at.tzinfo is None
            or self.deadline_at.tzinfo is None
            or self.claimed_at >= self.deadline_at
            or self.acknowledged_authorization_codes
            != REQUIRED_AUTHORIZATION_CODES
            or required_book_successor_digest(identity) != self.claim_digest
        ):
            raise ValueError("successor_acceptance_run_claim_invalid")
        return self


class _FixtureReadyEvent(_Closed):
    kind: Literal["fixture_ready"] = "fixture_ready"
    fixture_binding_digest: str = Field(pattern=_SHA256)
    isolated_fixture: Literal[True] = True


class _OutlineAcceptedEvent(_Closed):
    kind: Literal["outline_accepted"] = "outline_accepted"
    chapter_order: int = Field(ge=1, le=3)
    provider_response_digest: str = Field(pattern=_SHA256)
    formal_outline_revision: str = Field(pattern=_SHA256)
    attempts: tuple[SuccessorAcceptanceAttemptEvidence, ...] = Field(
        min_length=1,
        max_length=2,
    )
    attempt_set_digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_attempts(self) -> "_OutlineAcceptedEvent":
        if self.attempt_set_digest != _attempt_set_digest(self.attempts):
            raise ValueError("successor_acceptance_outline_attempts_changed")
        return self


class _RootBoundEvent(_Closed):
    kind: Literal["root_bound"] = "root_bound"
    root_readiness_digest: str = Field(pattern=_SHA256)
    root_projection_digest: str = Field(pattern=_SHA256)


class _RootStartedEvent(_Closed):
    kind: Literal["root_started"] = "root_started"
    root_job_binding_digest: str = Field(pattern=_SHA256)
    root_readiness_digest: str = Field(pattern=_SHA256)
    execution_epoch: int = Field(ge=1, le=_MAX)


class _RecoveryProvenEvent(_Closed):
    kind: Literal["recovery_proven"] = "recovery_proven"
    root_job_binding_digest: str = Field(pattern=_SHA256)
    checkpoint_digest: str = Field(pattern=_SHA256)
    before_execution_epoch: int = Field(ge=1, le=_MAX)
    after_execution_epoch: int = Field(ge=2, le=_MAX)
    duplicate_paid_attempt_count: Literal[0] = 0
    source_and_action_chain_unchanged: Literal[True] = True

    @model_validator(mode="after")
    def validate_epochs(self) -> "_RecoveryProvenEvent":
        if self.after_execution_epoch <= self.before_execution_epoch:
            raise ValueError("successor_acceptance_recovery_epoch_invalid")
        return self


class _RootSettledEvent(_Closed):
    kind: Literal["root_settled"] = "root_settled"
    root_job_binding_digest: str = Field(pattern=_SHA256)
    status: Literal["completed", "failed"]
    result_digest: str = Field(pattern=_SHA256)
    final_audit_complete: bool
    formal_prose_write_count: int = Field(ge=0, le=3)
    formal_state_accept_count: int = Field(ge=0, le=3)
    narrative_revision_increment: int = Field(ge=0, le=3)
    failure_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        pattern=r"^[a-z0-9_]+$",
    )
    attempts: tuple[SuccessorAcceptanceAttemptEvidence, ...] = Field(
        max_length=1000,
    )
    authoritative_attempt_count: int = Field(ge=0, le=1000)
    attempt_set_digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_terminal_shape(self) -> "_RootSettledEvent":
        if (
            self.authoritative_attempt_count != len(self.attempts)
            or self.attempt_set_digest != _attempt_set_digest(self.attempts)
            or (self.status == "completed" and self.failure_code is not None)
            or (self.status == "failed" and self.failure_code is None)
        ):
            raise ValueError("successor_acceptance_root_terminal_invalid")
        return self


class _FailureObservedEvent(_Closed):
    kind: Literal["failure_observed"] = "failure_observed"
    stage: Literal[
        "fixture",
        "outlines",
        "root_binding",
        "root_start",
        "recovery",
    ]
    failure_code: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[a-z0-9_]+$",
    )
    attempts: tuple[SuccessorAcceptanceAttemptEvidence, ...] = Field(
        max_length=1000,
    )
    authoritative_attempt_count: int = Field(ge=0, le=1000)
    attempt_set_digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_failure_attempts(self) -> "_FailureObservedEvent":
        if (
            self.authoritative_attempt_count != len(self.attempts)
            or self.attempt_set_digest != _attempt_set_digest(self.attempts)
        ):
            raise ValueError("successor_acceptance_failure_attempts_invalid")
        return self


class _CleanupProvenEvent(_Closed):
    kind: Literal["cleanup_proven"] = "cleanup_proven"
    hard_delete_complete: Literal[True] = True
    scanned_collection_count: int = Field(ge=1, le=10_000)
    residual_record_count: Literal[0] = 0
    independent_rescan_complete: Literal[True] = True
    residual_counts_digest: str = Field(pattern=_SHA256)


_StoredEvent = Annotated[
    Union[
        _FixtureReadyEvent,
        _OutlineAcceptedEvent,
        _RootBoundEvent,
        _RootStartedEvent,
        _RecoveryProvenEvent,
        _RootSettledEvent,
        _FailureObservedEvent,
        _CleanupProvenEvent,
    ],
    Field(discriminator="kind"),
]


class SuccessorAcceptanceRunJournal(_Closed):
    schema_version: Literal["successor_acceptance_run_journal.v1"] = (
        "successor_acceptance_run_journal.v1"
    )
    claim: SuccessorAcceptanceRunClaim
    events: tuple[_StoredEvent, ...] = Field(max_length=16)
    journal_digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_digest(self) -> "SuccessorAcceptanceRunJournal":
        identity = self.model_dump(mode="python", exclude={"journal_digest"})
        if required_book_successor_digest(identity) != self.journal_digest:
            raise ValueError("successor_acceptance_run_journal_changed")
        return self


class SuccessorActualProviderUsage(_Closed):
    provider_alias: str = Field(min_length=1, max_length=160)
    provider_model: str = Field(min_length=1, max_length=240)
    accounted_attempts: int = Field(ge=0, le=_MAX)
    uncertain_attempts: int = Field(ge=0, le=_MAX)
    released_pre_dispatch_attempts: int = Field(ge=0, le=_MAX)
    input_tokens: int = Field(ge=0, le=_MAX)
    output_tokens: int = Field(ge=0, le=_MAX)
    total_tokens: int = Field(ge=0, le=_MAX)
    currency: str = Field(min_length=1, max_length=16)
    cost_upper_bound: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def validate_total(self) -> "SuccessorActualProviderUsage":
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("successor_acceptance_provider_usage_invalid")
        return self


class SuccessorAcceptanceRunProgress(_Closed):
    schema_version: Literal["successor_acceptance_run_progress.v1"] = (
        "successor_acceptance_run_progress.v1"
    )
    sample_id: Literal["successor-representative-3000-v1"]
    ordinal: int = Field(ge=1, le=2)
    status: Literal["running", "awaiting_cleanup", "passed", "failed"]
    phase: Literal[
        "claimed",
        "fixture_ready",
        "outlines",
        "root_bound",
        "root_started",
        "recovery_proven",
        "root_settled",
        "failure_observed",
        "cleanup_proven",
    ]
    accepted_outline_count: int = Field(ge=0, le=3)
    recovery_proven: bool
    final_audit_complete: bool
    cleanup_proven: bool
    failure_code: str | None
    provider_usage: tuple[SuccessorActualProviderUsage, ...]
    uncertain_attempt_count: int = Field(ge=0, le=_MAX)
    can_close_issue_20: bool


@dataclass(frozen=True)
class FixtureReadyObservation:
    fixture_binding_digest: str


@dataclass(frozen=True)
class OutlineAcceptedObservation:
    chapter_order: int
    provider_outline: Mapping[str, Any]
    formal_outline: Mapping[str, Any]
    attempts: tuple[SuccessorAcceptanceAttemptEvidence, ...]


@dataclass(frozen=True)
class RootBoundObservation:
    root_readiness: Mapping[str, Any]


@dataclass(frozen=True)
class RootStartedObservation:
    root_job_binding_digest: str
    execution_epoch: int


@dataclass(frozen=True)
class RecoveryProvenObservation:
    root_job_binding_digest: str
    checkpoint_digest: str
    before_execution_epoch: int
    after_execution_epoch: int


@dataclass(frozen=True)
class RootSettledObservation:
    root_job_binding_digest: str
    status: Literal["completed", "failed"]
    result_digest: str
    final_audit_complete: bool
    formal_prose_write_count: int
    formal_state_accept_count: int
    narrative_revision_increment: int
    failure_code: str | None
    attempts: tuple[SuccessorAcceptanceAttemptEvidence, ...]
    authoritative_attempt_count: int


@dataclass(frozen=True)
class FailureObservedObservation:
    stage: Literal[
        "fixture",
        "outlines",
        "root_binding",
        "root_start",
        "recovery",
    ]
    failure_code: str
    attempts: tuple[SuccessorAcceptanceAttemptEvidence, ...] = ()
    authoritative_attempt_count: int = 0


@dataclass(frozen=True)
class CleanupProvenObservation:
    scanned_collection_count: int
    residual_counts_digest: str


SuccessorAcceptanceObservation = Union[
    FixtureReadyObservation,
    OutlineAcceptedObservation,
    RootBoundObservation,
    RootStartedObservation,
    RecoveryProvenObservation,
    RootSettledObservation,
    FailureObservedObservation,
    CleanupProvenObservation,
]


def _attempt_set_digest(
    attempts: Sequence[SuccessorAcceptanceAttemptEvidence],
) -> str:
    return required_book_successor_digest(
        sorted(item.attempt_digest for item in attempts)
    )


def _journal(
    claim: SuccessorAcceptanceRunClaim,
    events: Sequence[_StoredEvent],
) -> SuccessorAcceptanceRunJournal:
    identity = {
        "schema_version": "successor_acceptance_run_journal.v1",
        "claim": claim,
        "events": tuple(events),
    }
    return SuccessorAcceptanceRunJournal(
        **identity,
        journal_digest=required_book_successor_digest(identity),
    )


def parse_successor_acceptance_run_journal(
    value: Any,
) -> SuccessorAcceptanceRunJournal:
    if isinstance(value, SuccessorAcceptanceRunJournal):
        return value
    try:
        return SuccessorAcceptanceRunJournal.model_validate_json(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        )
    except (TypeError, ValueError) as exc:
        raise SuccessorAcceptanceLedgerConflict(
            "successor acceptance run journal is invalid"
        ) from exc


def _attempts_for_event(event: _StoredEvent) -> tuple[
    SuccessorAcceptanceAttemptEvidence,
    ...,
]:
    if isinstance(
        event,
        (_OutlineAcceptedEvent, _RootSettledEvent, _FailureObservedEvent),
    ):
        return event.attempts
    return ()


def _usage(
    authorization: RequiredBookSuccessorAcceptanceAuthorization,
    attempts: Sequence[SuccessorAcceptanceAttemptEvidence],
) -> tuple[SuccessorActualProviderUsage, ...]:
    prices = {
        item.provider_alias: item for item in authorization.provider_usage_bounds
    }
    totals: dict[str, list[int]] = {
        alias: [0, 0, 0, 0, 0, 0]
        for alias in prices
    }
    for attempt in attempts:
        if attempt.provider_alias not in totals:
            raise SuccessorAcceptanceLedgerConflict(
                "successor acceptance attempt Provider is not authorized"
            )
        values = totals[attempt.provider_alias]
        if attempt.state == "accounted":
            values[0] += 1
        elif attempt.state == "uncertain":
            values[1] += 1
        else:
            values[2] += 1
        values[3] += attempt.input_tokens
        values[4] += attempt.output_tokens
        values[5] += attempt.total_tokens
    result = []
    for alias in sorted(totals):
        accounted, uncertain, released, input_tokens, output_tokens, total = (
            totals[alias]
        )
        priced = prices[alias]
        amount = (
            Decimal(input_tokens)
            * priced.pricing.input_cache_miss_per_million
            + Decimal(output_tokens) * priced.pricing.output_per_million
        ) / Decimal(1_000_000)
        result.append(SuccessorActualProviderUsage(
            provider_alias=alias,
            provider_model=priced.provider_model,
            accounted_attempts=accounted,
            uncertain_attempts=uncertain,
            released_pre_dispatch_attempts=released,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total,
            currency=priced.pricing.currency,
            cost_upper_bound=amount.quantize(
                _PRICE_QUANTUM,
                rounding=ROUND_CEILING,
            ),
        ))
    return tuple(result)


def _assert_usage_within_bounds(
    authorization: RequiredBookSuccessorAcceptanceAuthorization,
    *,
    all_attempts: Sequence[SuccessorAcceptanceAttemptEvidence],
    outline_attempts: Sequence[SuccessorAcceptanceAttemptEvidence],
    root_attempts: Sequence[SuccessorAcceptanceAttemptEvidence],
) -> None:
    attempt_ids = [item.attempt_digest for item in all_attempts]
    if len(attempt_ids) != len(set(attempt_ids)):
        raise SuccessorAcceptanceLedgerConflict(
            "successor acceptance attempt identity was reused"
        )
    authorized_models = {
        item.provider_alias: item.provider_model
        for item in authorization.provider_usage_bounds
    }
    if any(
        authorized_models.get(item.provider_alias) != item.provider_model
        for item in all_attempts
    ):
        raise SuccessorAcceptanceLedgerConflict(
            "successor acceptance attempt Provider model changed"
        )

    def aggregate(
        attempts: Sequence[SuccessorAcceptanceAttemptEvidence],
    ) -> dict[str, tuple[int, int, int]]:
        values: dict[str, list[int]] = {}
        for attempt in attempts:
            current = values.setdefault(attempt.provider_alias, [0, 0, 0])
            if attempt.state != "released_pre_dispatch":
                current[0] += 1
            current[1] += attempt.input_tokens
            current[2] += attempt.output_tokens
        return {alias: tuple(item) for alias, item in values.items()}

    maximum = {
        item.provider_alias: (
            item.maximum_paid_attempts,
            item.maximum_input_tokens,
            item.maximum_output_tokens,
        )
        for item in authorization.provider_usage_bounds
    }
    for alias, actual in aggregate(all_attempts).items():
        if alias not in maximum or any(
            actual[index] > maximum[alias][index] for index in range(3)
        ):
            raise SuccessorAcceptanceLedgerConflict(
                "successor acceptance aggregate budget was exceeded"
            )

    outline_alias = authorization.outline_stage.generation.provider_alias
    if any(
        item.state != "released_pre_dispatch"
        and (
            item.input_tokens
            > authorization.outline_stage.maximum_input_tokens_per_attempt
            or item.output_tokens
            > authorization.outline_stage.maximum_output_tokens_per_attempt
        )
        for item in outline_attempts
    ):
        raise SuccessorAcceptanceLedgerConflict(
            "successor acceptance outline attempt budget was exceeded"
        )
    outline_actual = aggregate(outline_attempts)
    outline_maximum = (
        authorization.outline_stage.maximum_provider_attempts_total,
        authorization.outline_stage.maximum_input_tokens_total,
        authorization.outline_stage.maximum_output_tokens_total,
    )
    if set(outline_actual) - {outline_alias} or any(
        outline_actual.get(outline_alias, (0, 0, 0))[index]
        > outline_maximum[index]
        for index in range(3)
    ):
        raise SuccessorAcceptanceLedgerConflict(
            "successor acceptance outline budget was exceeded"
        )

    root_maximum = {
        item.provider_alias: (
            item.maximum_paid_attempts,
            item.maximum_input_tokens,
            item.maximum_output_tokens,
        )
        for item in authorization.root_projection.provider_usage_bounds
    }
    for alias, actual in aggregate(root_attempts).items():
        if alias not in root_maximum or any(
            actual[index] > root_maximum[alias][index] for index in range(3)
        ):
            raise SuccessorAcceptanceLedgerConflict(
                "successor acceptance root budget was exceeded"
            )


class RequiredBookSuccessorAcceptanceLedger:
    """Deep pure Module for claim, phase advancement and terminal inspection."""

    def claim(
        self,
        readiness: Mapping[str, Any],
        *,
        owner_id: str,
        config: Mapping[str, Any],
        prior_journals: Sequence[SuccessorAcceptanceRunJournal | Mapping[str, Any]],
        acknowledged_authorization_codes: Sequence[str],
        claimed_at: datetime,
    ) -> SuccessorAcceptanceRunJournal:
        authorization = validate_required_book_successor_acceptance_readiness(
            readiness
        )
        validate_current_required_book_successor_acceptance_authorization(
            authorization,
            config=config,
        )
        if (
            claimed_at.tzinfo is None
            or claimed_at < authorization.created_at
            or claimed_at >= authorization.deadline_at
        ):
            raise SuccessorAcceptanceLedgerConflict(
                "successor acceptance claim time is outside authority"
            )
        supplied_codes = tuple(acknowledged_authorization_codes)
        supplied_code_set = set(supplied_codes)
        codes = tuple(
            code
            for code in REQUIRED_AUTHORIZATION_CODES
            if code in supplied_code_set
        )
        if (
            codes != REQUIRED_AUTHORIZATION_CODES
            or len(supplied_code_set) != len(REQUIRED_AUTHORIZATION_CODES)
            or len(supplied_codes) != len(REQUIRED_AUTHORIZATION_CODES)
        ):
            raise SuccessorAcceptanceLedgerConflict(
                "successor acceptance requires three exact authorizations"
            )
        previous = [
            parse_successor_acceptance_run_journal(item)
            for item in prior_journals
        ]
        if len(previous) >= authorization.sample.maximum_real_runs:
            raise SuccessorAcceptanceLedgerConflict(
                "successor acceptance two-run stop-loss is exhausted"
            )
        for expected_ordinal, journal in enumerate(previous, start=1):
            progress = self.inspect(readiness, journal)
            if (
                journal.claim.ordinal != expected_ordinal
                or journal.claim.owner_id != owner_id
                or journal.claim.readiness_digest != readiness.get("digest")
                or journal.claim.authorization_contract_digest
                != authorization.contract_digest
                or progress.status not in {"passed", "failed"}
                or not progress.cleanup_proven
                or progress.uncertain_attempt_count != 0
            ):
                raise SuccessorAcceptanceLedgerConflict(
                    "successor acceptance prior run is unsettled or changed"
                )
            if progress.status == "passed":
                raise SuccessorAcceptanceLedgerConflict(
                    "successor acceptance already passed"
                )
        if previous and claimed_at <= previous[-1].claim.claimed_at:
            raise SuccessorAcceptanceLedgerConflict(
                "successor acceptance claim time did not advance"
            )
        identity = {
            "schema_version": "successor_acceptance_run_claim.v1",
            "sample_id": authorization.sample.sample_id,
            "ordinal": len(previous) + 1,
            "owner_id": owner_id,
            "readiness_digest": str(readiness.get("digest") or ""),
            "authorization_contract_digest": authorization.contract_digest,
            "acknowledged_authorization_codes": REQUIRED_AUTHORIZATION_CODES,
            "claimed_at": claimed_at,
            "deadline_at": authorization.deadline_at,
        }
        claim = SuccessorAcceptanceRunClaim(
            **identity,
            claim_digest=required_book_successor_digest(identity),
        )
        return _journal(claim, ())

    def advance(
        self,
        readiness: Mapping[str, Any],
        journal: SuccessorAcceptanceRunJournal | Mapping[str, Any],
        observation: SuccessorAcceptanceObservation,
    ) -> SuccessorAcceptanceRunJournal:
        authorization, current = self._bound(readiness, journal)
        event: _StoredEvent
        if isinstance(observation, FixtureReadyObservation):
            event = _FixtureReadyEvent(
                fixture_binding_digest=observation.fixture_binding_digest,
            )
        elif isinstance(observation, OutlineAcceptedObservation):
            provider = validate_required_book_successor_acceptance_outline(
                authorization,
                observation.provider_outline,
            )
            formal = validate_required_book_successor_acceptance_outline(
                authorization,
                observation.formal_outline,
            )
            provider_digest = prose_revision(provider)
            formal_digest = prose_revision(formal)
            if provider_digest != formal_digest:
                raise SuccessorAcceptanceLedgerConflict(
                    "successor acceptance formal outline changed"
                )
            attempts = tuple(observation.attempts)
            event = _OutlineAcceptedEvent(
                chapter_order=observation.chapter_order,
                provider_response_digest=provider_digest,
                formal_outline_revision=formal_digest,
                attempts=attempts,
                attempt_set_digest=_attempt_set_digest(attempts),
            )
        elif isinstance(observation, RootBoundObservation):
            validate_required_book_successor_acceptance_root_readiness(
                authorization,
                observation.root_readiness,
            )
            event = _RootBoundEvent(
                root_readiness_digest=str(
                    observation.root_readiness.get("digest") or ""
                ),
                root_projection_digest=(
                    authorization.root_projection.projection_digest
                ),
            )
        elif isinstance(observation, RootStartedObservation):
            root_bound = next(
                (
                    item
                    for item in reversed(current.events)
                    if isinstance(item, _RootBoundEvent)
                ),
                None,
            )
            if root_bound is None:
                raise SuccessorAcceptanceLedgerConflict(
                    "successor acceptance root start preceded root binding"
                )
            event = _RootStartedEvent(
                root_job_binding_digest=observation.root_job_binding_digest,
                root_readiness_digest=(
                    root_bound.root_readiness_digest
                ),
                execution_epoch=observation.execution_epoch,
            )
        elif isinstance(observation, RecoveryProvenObservation):
            event = _RecoveryProvenEvent(
                root_job_binding_digest=observation.root_job_binding_digest,
                checkpoint_digest=observation.checkpoint_digest,
                before_execution_epoch=observation.before_execution_epoch,
                after_execution_epoch=observation.after_execution_epoch,
            )
        elif isinstance(observation, RootSettledObservation):
            attempts = tuple(observation.attempts)
            event = _RootSettledEvent(
                root_job_binding_digest=observation.root_job_binding_digest,
                status=observation.status,
                result_digest=observation.result_digest,
                final_audit_complete=observation.final_audit_complete,
                formal_prose_write_count=observation.formal_prose_write_count,
                formal_state_accept_count=observation.formal_state_accept_count,
                narrative_revision_increment=(
                    observation.narrative_revision_increment
                ),
                failure_code=observation.failure_code,
                attempts=attempts,
                authoritative_attempt_count=(
                    observation.authoritative_attempt_count
                ),
                attempt_set_digest=_attempt_set_digest(attempts),
            )
        elif isinstance(observation, FailureObservedObservation):
            attempts = tuple(observation.attempts)
            event = _FailureObservedEvent(
                stage=observation.stage,
                failure_code=observation.failure_code,
                attempts=attempts,
                authoritative_attempt_count=(
                    observation.authoritative_attempt_count
                ),
                attempt_set_digest=_attempt_set_digest(attempts),
            )
        elif isinstance(observation, CleanupProvenObservation):
            event = _CleanupProvenEvent(
                scanned_collection_count=observation.scanned_collection_count,
                residual_counts_digest=observation.residual_counts_digest,
            )
        else:
            raise TypeError("unsupported successor acceptance observation")
        candidate = _journal(current.claim, (*current.events, event))
        self.inspect(readiness, candidate)
        return candidate

    def inspect(
        self,
        readiness: Mapping[str, Any],
        journal: SuccessorAcceptanceRunJournal | Mapping[str, Any],
    ) -> SuccessorAcceptanceRunProgress:
        authorization, parsed = self._bound(readiness, journal)
        phase = "claimed"
        outline_count = 0
        root_readiness_digest: str | None = None
        root_job_digest: str | None = None
        root_epoch: int | None = None
        recovery = False
        terminal: _RootSettledEvent | None = None
        failure: _FailureObservedEvent | None = None
        cleanup = False
        all_attempts: list[SuccessorAcceptanceAttemptEvidence] = []
        outline_attempts: list[SuccessorAcceptanceAttemptEvidence] = []
        root_attempts: list[SuccessorAcceptanceAttemptEvidence] = []

        for event in parsed.events:
            if cleanup or terminal is not None or failure is not None:
                if not isinstance(event, _CleanupProvenEvent) or cleanup:
                    raise SuccessorAcceptanceLedgerConflict(
                        "successor acceptance event followed a terminal observation"
                    )
            attempts = list(_attempts_for_event(event))
            all_attempts.extend(attempts)
            if isinstance(event, _FixtureReadyEvent):
                if phase != "claimed":
                    raise SuccessorAcceptanceLedgerConflict(
                        "successor acceptance fixture event is out of order"
                    )
                phase = "fixture_ready"
            elif isinstance(event, _OutlineAcceptedEvent):
                if phase not in {"fixture_ready", "outlines"} or (
                    event.chapter_order != outline_count + 1
                ):
                    raise SuccessorAcceptanceLedgerConflict(
                        "successor acceptance outline event is out of order"
                    )
                if (
                    any(item.state == "uncertain" for item in attempts)
                    or not any(item.state == "accounted" for item in attempts)
                    or len(attempts)
                    > authorization.outline_stage.maximum_semantic_attempts_per_chapter
                ):
                    raise SuccessorAcceptanceLedgerConflict(
                        "accepted outline requires settled accounted attempts"
                    )
                outline_attempts.extend(attempts)
                outline_count += 1
                phase = "outlines"
            elif isinstance(event, _RootBoundEvent):
                if phase != "outlines" or outline_count != 3:
                    raise SuccessorAcceptanceLedgerConflict(
                        "successor acceptance root was bound before three outlines"
                    )
                if event.root_projection_digest != (
                    authorization.root_projection.projection_digest
                ):
                    raise SuccessorAcceptanceLedgerConflict(
                        "successor acceptance root projection changed"
                    )
                root_readiness_digest = event.root_readiness_digest
                phase = "root_bound"
            elif isinstance(event, _RootStartedEvent):
                if (
                    phase != "root_bound"
                    or event.root_readiness_digest != root_readiness_digest
                ):
                    raise SuccessorAcceptanceLedgerConflict(
                        "successor acceptance root start is out of order"
                    )
                root_job_digest = event.root_job_binding_digest
                root_epoch = event.execution_epoch
                phase = "root_started"
            elif isinstance(event, _RecoveryProvenEvent):
                if (
                    phase != "root_started"
                    or event.root_job_binding_digest != root_job_digest
                    or event.before_execution_epoch != root_epoch
                ):
                    raise SuccessorAcceptanceLedgerConflict(
                        "successor acceptance recovery evidence changed"
                    )
                root_epoch = event.after_execution_epoch
                recovery = True
                phase = "recovery_proven"
            elif isinstance(event, _RootSettledEvent):
                if (
                    phase not in {"root_started", "recovery_proven"}
                    or event.root_job_binding_digest != root_job_digest
                ):
                    raise SuccessorAcceptanceLedgerConflict(
                        "successor acceptance root terminal is out of order"
                    )
                root_attempts.extend(attempts)
                terminal = event
                phase = "root_settled"
            elif isinstance(event, _FailureObservedEvent):
                allowed = {
                    "claimed": {"fixture"},
                    "fixture_ready": {"outlines"},
                    "outlines": {"outlines", "root_binding"},
                    "root_bound": {"root_start"},
                    "root_started": {"recovery"},
                }
                if event.stage not in allowed.get(phase, set()):
                    raise SuccessorAcceptanceLedgerConflict(
                        "successor acceptance failure event is out of order"
                    )
                if (
                    event.stage == "root_binding" and outline_count != 3
                ) or (
                    event.stage in {"fixture", "root_binding", "root_start"}
                    and attempts
                ):
                    raise SuccessorAcceptanceLedgerConflict(
                        "successor acceptance failure evidence changed"
                    )
                if event.stage == "outlines":
                    outline_attempts.extend(attempts)
                else:
                    root_attempts.extend(attempts)
                failure = event
                phase = "failure_observed"
            elif isinstance(event, _CleanupProvenEvent):
                if terminal is None and failure is None:
                    raise SuccessorAcceptanceLedgerConflict(
                        "successor acceptance cleanup preceded a terminal observation"
                    )
                cleanup = True
                phase = "cleanup_proven"

        _assert_usage_within_bounds(
            authorization,
            all_attempts=all_attempts,
            outline_attempts=outline_attempts,
            root_attempts=root_attempts,
        )
        usage = _usage(authorization, all_attempts)
        uncertain = sum(item.uncertain_attempts for item in usage)
        root_passed = bool(
            terminal is not None
            and terminal.status == "completed"
            and terminal.final_audit_complete
            and terminal.formal_prose_write_count == 3
            and terminal.formal_state_accept_count == 3
            and terminal.narrative_revision_increment == 3
            and recovery
            and uncertain == 0
        )
        if cleanup:
            status: Literal["running", "awaiting_cleanup", "passed", "failed"] = (
                "passed" if root_passed and failure is None else "failed"
            )
        elif terminal is not None or failure is not None:
            status = "awaiting_cleanup"
        else:
            status = "running"
        failure_code = (
            failure.failure_code
            if failure is not None
            else terminal.failure_code
            if terminal is not None and terminal.status == "failed"
            else "successor_acceptance_uncertain_attempts"
            if terminal is not None and uncertain
            else "successor_acceptance_recovery_not_proven"
            if terminal is not None and not recovery
            else "successor_acceptance_terminal_contract_failed"
            if terminal is not None and not root_passed
            else None
        )
        return SuccessorAcceptanceRunProgress(
            sample_id=authorization.sample.sample_id,
            ordinal=parsed.claim.ordinal,
            status=status,
            phase=phase,
            accepted_outline_count=outline_count,
            recovery_proven=recovery,
            final_audit_complete=bool(
                terminal is not None and terminal.final_audit_complete
            ),
            cleanup_proven=cleanup,
            failure_code=failure_code,
            provider_usage=usage,
            uncertain_attempt_count=uncertain,
            can_close_issue_20=status == "passed",
        )

    @staticmethod
    def _bound(
        readiness: Mapping[str, Any],
        journal: SuccessorAcceptanceRunJournal | Mapping[str, Any],
    ) -> tuple[
        RequiredBookSuccessorAcceptanceAuthorization,
        SuccessorAcceptanceRunJournal,
    ]:
        authorization = validate_required_book_successor_acceptance_readiness(
            readiness
        )
        parsed = parse_successor_acceptance_run_journal(journal)
        if (
            parsed.claim.sample_id != authorization.sample.sample_id
            or parsed.claim.readiness_digest != readiness.get("digest")
            or parsed.claim.authorization_contract_digest
            != authorization.contract_digest
            or parsed.claim.deadline_at != authorization.deadline_at
        ):
            raise SuccessorAcceptanceLedgerConflict(
                "successor acceptance journal binding changed"
            )
        return authorization, parsed


required_book_successor_acceptance_ledger = (
    RequiredBookSuccessorAcceptanceLedger()
)


__all__ = [
    "CleanupProvenObservation",
    "FailureObservedObservation",
    "FixtureReadyObservation",
    "OutlineAcceptedObservation",
    "RecoveryProvenObservation",
    "RequiredBookSuccessorAcceptanceLedger",
    "RootBoundObservation",
    "RootSettledObservation",
    "RootStartedObservation",
    "SuccessorAcceptanceAttemptEvidence",
    "SuccessorAcceptanceLedgerConflict",
    "SuccessorAcceptanceRunJournal",
    "SuccessorAcceptanceRunProgress",
    "parse_successor_acceptance_run_journal",
    "required_book_successor_acceptance_ledger",
]
