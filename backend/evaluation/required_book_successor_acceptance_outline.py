"""Durable outline pre-stage for strict successor #20 acceptance.

One deterministic control Job owns all three paid outline calls.  Each chapter
request is journaled before dispatch, the Provider candidate is persisted
before formal acceptance, and the accepted outline is re-read from storage.
This closes the confirmation-loss window without granting the parent host
journal direct access to raw Provider material.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import json
import re
from typing import Any, Literal, Protocol

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from backend.evaluation.required_book_successor_acceptance import (
    RequiredBookSuccessorAcceptanceAuthorization,
    validate_required_book_successor_acceptance_outline,
    validate_required_book_successor_acceptance_readiness,
)
from backend.evaluation.required_book_successor_acceptance_identity import (
    SuccessorAcceptanceFixtureIdentity,
)
from backend.evaluation.required_book_successor_acceptance_ledger import (
    SuccessorAcceptanceRunClaim,
    SuccessorAcceptanceRunJournal,
    required_book_successor_acceptance_ledger,
)
from backend.llm.schemas.novel_pydantic import ChapterOutlineResultSchema
from backend.services.generation.attempt_scope import JobAttemptScope
from backend.services.generation.candidate_repair_contracts import (
    JobMutationRecoveryBindingV1,
)
from backend.services.generation.required_book_successor import (
    required_book_successor_digest,
)
from backend.services.generation.required_chapter_review_job import (
    RequiredGenerationPlanSnapshot,
)
from backend.services.generation.prose_runs import prose_revision


SUCCESSOR_ACCEPTANCE_OUTLINE_JOB_KIND = (
    "successor_acceptance_outline_control"
)
SUCCESSOR_ACCEPTANCE_OUTLINE_STEP_PREFIX = "successor-acceptance-outline:"
SUCCESSOR_ACCEPTANCE_OUTLINES_READY = "successor_acceptance_outlines_ready"
MAX_SUCCESSOR_ACCEPTANCE_OUTLINE_ATTEMPTS_PER_CHAPTER = 2

_SHA256 = r"^[0-9a-f]{64}$"
_OBJECT_ID = r"^[0-9a-f]{24}$"
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_MAX = 2**63 - 1


class SuccessorAcceptanceOutlineConflict(ValueError):
    """The outline authority, journal, attempt ledger, or source diverged."""


class SuccessorAcceptanceOutlineDispatchRejected(
    SuccessorAcceptanceOutlineConflict
):
    """A physical Provider claim did not cross the dispatch boundary."""

    provider_request_not_dispatched = True


class _Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class SuccessorAcceptanceOutlineRequest(_Closed):
    schema_version: Literal["successor_acceptance_outline_request.v1"] = (
        "successor_acceptance_outline_request.v1"
    )
    request_digest: str = Field(pattern=_SHA256)
    operation_digest: str = Field(pattern=_SHA256)
    claim_digest: str = Field(pattern=_SHA256)
    readiness_digest: str = Field(pattern=_SHA256)
    authorization_contract_digest: str = Field(pattern=_SHA256)
    outline_job_id: str = Field(pattern=_OBJECT_ID)
    owner_id: str = Field(pattern=_OBJECT_ID)
    novel_id: str = Field(pattern=_OBJECT_ID)
    chapter_id: str = Field(pattern=_OBJECT_ID)
    chapter_order: int = Field(ge=1, le=3)
    expected_narrative_revision: int = Field(ge=0, le=2)
    authorization_revision: int = Field(ge=1, le=_MAX)
    generation: RequiredGenerationPlanSnapshot
    maximum_provider_attempts: Literal[2] = (
        MAX_SUCCESSOR_ACCEPTANCE_OUTLINE_ATTEMPTS_PER_CHAPTER
    )
    maximum_input_tokens_per_attempt: int = Field(ge=1, le=_MAX)
    maximum_output_tokens_per_attempt: int = Field(ge=1, le=_MAX)
    maximum_tokens_per_attempt: int = Field(ge=1, le=_MAX)

    @model_validator(mode="after")
    def validate_request(self) -> "SuccessorAcceptanceOutlineRequest":
        if (
            self.expected_narrative_revision != self.chapter_order - 1
            or self.generation.call_kind != "structured"
            or self.maximum_tokens_per_attempt
            != self.maximum_input_tokens_per_attempt
            + self.maximum_output_tokens_per_attempt
        ):
            raise ValueError("successor_acceptance_outline_request_invalid")
        identity = self.model_dump(mode="python", exclude={"request_digest"})
        if required_book_successor_digest(identity) != self.request_digest:
            raise ValueError("successor_acceptance_outline_request_changed")
        return self

    @property
    def step_id(self) -> str:
        return (
            f"{SUCCESSOR_ACCEPTANCE_OUTLINE_STEP_PREFIX}"
            f"{self.chapter_order}:{self.request_digest}"
        )

    @property
    def mutation_binding(self) -> JobMutationRecoveryBindingV1:
        return JobMutationRecoveryBindingV1(
            novel_id=self.novel_id,
            job_id=self.outline_job_id,
            chapter_id=self.chapter_id,
            readiness_digest=self.readiness_digest,
            authorization_revision=self.authorization_revision,
            expected_narrative_revision=self.expected_narrative_revision,
            operation="accept_chapter_outline",
            idempotency_key=(
                f"successor-outline:{self.claim_digest}:"
                f"{self.chapter_order}"
            ),
        )


class SuccessorAcceptanceOutlineEntry(_Closed):
    schema_version: Literal["successor_acceptance_outline_entry.v1"] = (
        "successor_acceptance_outline_entry.v1"
    )
    request: SuccessorAcceptanceOutlineRequest
    phase: Literal["reserved", "produced", "accepted", "blocked"]
    provider_outline: dict[str, Any] | None = None
    provider_outline_revision: str | None = Field(default=None, pattern=_SHA256)
    formal_outline_revision: str | None = Field(default=None, pattern=_SHA256)
    attempt_ids: tuple[str, ...] = Field(default=(), max_length=2)
    failure_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        pattern=r"^[a-z][a-z0-9_]{0,79}$",
    )

    @model_validator(mode="after")
    def validate_entry(self) -> "SuccessorAcceptanceOutlineEntry":
        candidate_fields = (
            self.provider_outline,
            self.provider_outline_revision,
        )
        has_candidate = all(value is not None for value in candidate_fields)
        if any(value is not None for value in candidate_fields) != has_candidate:
            raise ValueError("successor_acceptance_outline_candidate_incomplete")
        if has_candidate:
            assert self.provider_outline is not None
            if (
                prose_revision(self.provider_outline)
                != self.provider_outline_revision
                or len(json.dumps(
                    self.provider_outline,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")) > 16_000
            ):
                raise ValueError(
                    "successor_acceptance_outline_candidate_changed"
                )
        if self.phase == "reserved" and (
            has_candidate
            or self.formal_outline_revision is not None
            or self.attempt_ids
            or self.failure_code is not None
        ):
            raise ValueError("successor_acceptance_outline_reserved_invalid")
        if self.phase == "produced" and (
            not has_candidate
            or not self.attempt_ids
            or self.formal_outline_revision is not None
            or self.failure_code is not None
        ):
            raise ValueError("successor_acceptance_outline_produced_invalid")
        if self.phase == "accepted" and (
            not has_candidate
            or not self.attempt_ids
            or self.formal_outline_revision
            != self.provider_outline_revision
            or self.failure_code is not None
        ):
            raise ValueError("successor_acceptance_outline_accepted_invalid")
        if self.phase == "blocked" and (
            self.failure_code is None
            or self.formal_outline_revision is not None
            or has_candidate != bool(self.attempt_ids)
        ):
            raise ValueError("successor_acceptance_outline_blocked_invalid")
        return self


class SuccessorAcceptanceOutlineJournal(_Closed):
    schema_version: Literal["successor_acceptance_outline_journal.v1"] = (
        "successor_acceptance_outline_journal.v1"
    )
    claim_digest: str = Field(pattern=_SHA256)
    authorization_contract_digest: str = Field(pattern=_SHA256)
    entries: tuple[SuccessorAcceptanceOutlineEntry, ...] = Field(
        min_length=1,
        max_length=3,
    )

    @model_validator(mode="after")
    def validate_chain(self) -> "SuccessorAcceptanceOutlineJournal":
        first = self.entries[0].request
        for index, entry in enumerate(self.entries, start=1):
            request = entry.request
            if (
                request.chapter_order != index
                or request.expected_narrative_revision != index - 1
                or request.claim_digest != self.claim_digest
                or request.authorization_contract_digest
                != self.authorization_contract_digest
                or request.outline_job_id != first.outline_job_id
                or request.owner_id != first.owner_id
                or request.novel_id != first.novel_id
                or request.readiness_digest != first.readiness_digest
                or request.authorization_revision
                != first.authorization_revision
                or request.generation != first.generation
                or request.maximum_tokens_per_attempt
                != first.maximum_tokens_per_attempt
                or request.maximum_input_tokens_per_attempt
                != first.maximum_input_tokens_per_attempt
                or request.maximum_output_tokens_per_attempt
                != first.maximum_output_tokens_per_attempt
            ):
                raise ValueError(
                    "successor_acceptance_outline_journal_chain_changed"
                )
            if index > 1 and self.entries[index - 2].phase != "accepted":
                raise ValueError(
                    "successor_acceptance_outline_journal_order_invalid"
                )
        if any(entry.phase != "accepted" for entry in self.entries[:-1]):
            raise ValueError(
                "successor_acceptance_outline_journal_terminal_not_last"
            )
        return self


@dataclass(frozen=True)
class SuccessorAcceptanceOutlineJobOutcome:
    status: Literal["ready", "blocked"]
    chapter_order: int
    provider_outline: Mapping[str, Any] | None
    formal_outline: Mapping[str, Any] | None
    attempts: tuple[Mapping[str, Any], ...]
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if (
            (self.status == "ready")
            != (
                self.provider_outline is not None
                and self.formal_outline is not None
                and self.failure_code is None
            )
            or self.status == "blocked"
            and (
                self.provider_outline is not None
                or self.formal_outline is not None
                or not isinstance(self.failure_code, str)
                or _SAFE_REASON.fullmatch(self.failure_code) is None
            )
        ):
            raise ValueError("successor_acceptance_outline_outcome_invalid")


class SuccessorAcceptanceOutlineRepository(Protocol):
    async def get_job(self, job_id: str) -> Mapping[str, Any]: ...

    async def begin_successor_acceptance_outline(
        self,
        job_id: str,
        request: SuccessorAcceptanceOutlineRequest,
    ) -> bool: ...

    async def publish_successor_acceptance_outline_candidate(
        self,
        job_id: str,
        request: SuccessorAcceptanceOutlineRequest,
        provider_outline: Mapping[str, Any],
        attempt_ids: Sequence[str],
    ) -> bool: ...

    async def publish_successor_acceptance_outline_accepted(
        self,
        job_id: str,
        request: SuccessorAcceptanceOutlineRequest,
        formal_outline_revision: str,
    ) -> bool: ...

    async def block_successor_acceptance_outline(
        self,
        job_id: str,
        request: SuccessorAcceptanceOutlineRequest,
        failure_code: str,
    ) -> bool: ...

    async def reserve_attempts(
        self,
        job_id: str,
        chapter_id: str,
        slots: int,
    ) -> Mapping[str, Any]: ...

    async def finish_attempt_reservation(
        self,
        job_id: str,
        chapter_id: str,
    ) -> bool: ...

    async def list_attempt_slots(
        self,
        job_id: str,
        *,
        chapter_id: str,
        step_prefix: str,
    ) -> Sequence[Mapping[str, Any]]: ...


def parse_successor_acceptance_outline_journal(
    value: Any,
) -> SuccessorAcceptanceOutlineJournal:
    try:
        return SuccessorAcceptanceOutlineJournal.model_validate_json(
            json.dumps(value, ensure_ascii=False, default=str)
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise SuccessorAcceptanceOutlineConflict(
            "successor_acceptance_outline_journal_invalid"
        ) from exc


def validate_successor_acceptance_outline_control_job(
    job: Mapping[str, Any],
    request: SuccessorAcceptanceOutlineRequest,
    *,
    allowed_revision_offsets: tuple[int, ...] = (0,),
) -> RequiredBookSuccessorAcceptanceAuthorization:
    try:
        authorization = validate_required_book_successor_acceptance_readiness(
            job.get("readiness") or {}
        )
        claim = SuccessorAcceptanceRunClaim.model_validate_json(
            json.dumps(
                job.get("successor_acceptance_claim"),
                ensure_ascii=False,
                default=str,
            )
        )
        identity = SuccessorAcceptanceFixtureIdentity.from_claim(claim)
    except (TypeError, ValueError) as exc:
        raise SuccessorAcceptanceOutlineConflict(
            "successor_acceptance_outline_job_authority_invalid"
        ) from exc
    if (
        str(job.get("_id") or "") != request.outline_job_id
        or str(job.get("owner_id") or "") != request.owner_id
        or str(job.get("novel_id") or "") != request.novel_id
        or job.get("job_kind") != SUCCESSOR_ACCEPTANCE_OUTLINE_JOB_KIND
        or job.get("scope") != "book"
        or job.get("volume_id") is not None
        or job.get("is_deleted") is not False
        or job.get("progress") not in (None, [])
        or job.get("authorization_revision")
        != request.authorization_revision
        or job.get("expected_narrative_revision")
        not in {
            request.expected_narrative_revision + offset
            for offset in allowed_revision_offsets
        }
        or (job.get("readiness") or {}).get("digest")
        != request.readiness_digest
        or authorization.contract_digest
        != request.authorization_contract_digest
        or claim.claim_digest != request.claim_digest
        or identity.outline_job_id != request.outline_job_id
        or identity.novel_id != request.novel_id
        or identity.owner_id != request.owner_id
        or identity.chapter_ids[request.chapter_order - 1]
        != request.chapter_id
        or authorization.outline_stage.generation != request.generation
        or authorization.outline_stage.maximum_input_tokens_per_attempt
        + authorization.outline_stage.maximum_output_tokens_per_attempt
        != request.maximum_tokens_per_attempt
    ):
        raise SuccessorAcceptanceOutlineConflict(
            "successor_acceptance_outline_job_authority_changed"
        )
    return authorization


def _attempts_for_request(
    job: Mapping[str, Any],
    request: SuccessorAcceptanceOutlineRequest,
) -> tuple[Mapping[str, Any], ...]:
    slots = job.get("attempt_slots")
    if not isinstance(slots, list):
        raise SuccessorAcceptanceOutlineConflict(
            "successor_acceptance_outline_attempt_ledger_invalid"
        )
    return tuple(
        item
        for item in slots
        if isinstance(item, Mapping)
        and str(item.get("chapter_id") or "") == request.chapter_id
        and str(item.get("step_id") or "") == request.step_id
    )


def validate_successor_acceptance_outline_attempts(
    job: Mapping[str, Any],
    request: SuccessorAcceptanceOutlineRequest,
    *,
    expected_accounted_attempt_ids: Sequence[str] | None = None,
    allowed_revision_offsets: tuple[int, ...] = (0,),
) -> tuple[Mapping[str, Any], ...]:
    from backend.db.required_adherence_journal import _checked_bookkeeping

    authorization = validate_successor_acceptance_outline_control_job(
        job,
        request,
        allowed_revision_offsets=allowed_revision_offsets,
    )
    try:
        _checked_bookkeeping(job)
    except ValueError as exc:
        raise SuccessorAcceptanceOutlineConflict(
            "successor_acceptance_outline_attempt_accounting_invalid"
        ) from exc
    attempts = _attempts_for_request(job, request)
    if len(attempts) > request.maximum_provider_attempts:
        raise SuccessorAcceptanceOutlineConflict(
            "successor_acceptance_outline_attempt_count_changed"
        )
    seen: set[str] = set()
    for attempt in attempts:
        attempt_id = attempt.get("attempt_id")
        state = attempt.get("state")
        bound = attempt.get("conservative_tokens")
        if (
            not isinstance(attempt_id, str)
            or not attempt_id
            or attempt_id in seen
            or attempt.get("provider_alias")
            != request.generation.provider_alias
            or not isinstance(attempt.get("phase"), str)
            or not attempt.get("phase")
            or type(bound) is not int
            or not 0 < bound <= request.maximum_tokens_per_attempt
            or state
            not in {
                "claimed",
                "accounted",
                "released_pre_dispatch",
                "uncertain",
            }
        ):
            raise SuccessorAcceptanceOutlineConflict(
                "successor_acceptance_outline_attempt_identity_changed"
            )
        seen.add(attempt_id)
        usage = attempt.get("usage")
        if state == "accounted":
            if (
                not isinstance(usage, Mapping)
                or set(usage)
                != {"input_tokens", "output_tokens", "total_tokens"}
                or any(type(value) is not int or value < 0 for value in usage.values())
                or usage["input_tokens"] + usage["output_tokens"]
                != usage["total_tokens"]
                or not 0 < usage["total_tokens"] <= bound
                or usage["input_tokens"]
                > authorization.outline_stage.maximum_input_tokens_per_attempt
                or usage["output_tokens"]
                > authorization.outline_stage.maximum_output_tokens_per_attempt
                or attempt.get("charged_tokens") != usage["total_tokens"]
            ):
                raise SuccessorAcceptanceOutlineConflict(
                    "successor_acceptance_outline_attempt_accounting_invalid"
                )
        elif state == "released_pre_dispatch":
            if usage is not None or attempt.get("charged_tokens") not in (None, 0):
                raise SuccessorAcceptanceOutlineConflict(
                    "successor_acceptance_outline_attempt_accounting_invalid"
                )
        elif usage is not None:
            raise SuccessorAcceptanceOutlineConflict(
                "successor_acceptance_outline_attempt_accounting_invalid"
            )
    accounted = tuple(
        str(item.get("attempt_id"))
        for item in attempts
        if item.get("state") == "accounted"
    )
    if (
        expected_accounted_attempt_ids is not None
        and accounted != tuple(expected_accounted_attempt_ids)
    ):
        raise SuccessorAcceptanceOutlineConflict(
            "successor_acceptance_outline_attempt_identity_changed"
        )
    return attempts


def project_successor_acceptance_outline_attempt_receipts(
    authorization: RequiredBookSuccessorAcceptanceAuthorization,
    slots: Sequence[Mapping[str, Any]],
    request: SuccessorAcceptanceOutlineRequest,
) -> tuple[Mapping[str, Any], ...]:
    """Project already-validated physical slots into redacted parent receipts."""

    if not isinstance(
        authorization,
        RequiredBookSuccessorAcceptanceAuthorization,
    ):
        raise SuccessorAcceptanceOutlineConflict(
            "successor_acceptance_outline_authorization_invalid"
        )
    pricing_matches = tuple(
        item.pricing
        for item in authorization.provider_usage_bounds
        if item.provider_alias == request.generation.provider_alias
    )
    if len(pricing_matches) != 1:
        raise SuccessorAcceptanceOutlineConflict(
            "successor_acceptance_outline_attempt_provider_changed"
        )
    pricing = pricing_matches[0]
    receipts: list[Mapping[str, Any]] = []
    for slot in slots:
        state = str(slot.get("state") or "")
        if state == "accounted":
            usage = dict(slot.get("usage") or {})
            basis = "provider_receipt"
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            total_tokens = int(usage.get("total_tokens") or 0)
        elif state == "uncertain":
            basis = "conservative_upper_bound"
            total_tokens = int(slot.get("conservative_tokens") or 0)
            minimum_input = max(
                0,
                total_tokens - request.maximum_output_tokens_per_attempt,
            )
            maximum_input = min(
                total_tokens,
                request.maximum_input_tokens_per_attempt,
            )
            input_tokens = (
                maximum_input
                if pricing.input_cache_miss_per_million
                >= pricing.output_per_million
                else minimum_input
            )
            output_tokens = total_tokens - input_tokens
        elif state == "released_pre_dispatch":
            basis = "not_dispatched"
            input_tokens = output_tokens = total_tokens = 0
        else:
            raise SuccessorAcceptanceOutlineConflict(
                "successor_acceptance_outline_attempt_unsettled"
            )
        receipts.append({
            "attempt_id": str(slot.get("attempt_id") or ""),
            "provider_alias": str(slot.get("provider_alias") or ""),
            "state": state,
            "usage_basis": basis,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        })
    return tuple(receipts)


async def write_successor_acceptance_outline_claim(
    job: Mapping[str, Any],
    *,
    chapter_id: str,
    step_id: str,
    phase: str,
    provider_alias: str,
    conservative_tokens: int,
    query: dict[str, Any],
    update: dict[str, Any],
    write_job: Callable[..., Awaitable[Any]],
) -> Any:
    """Fence one physical outline claim to the persisted ordered request."""

    try:
        journal = parse_successor_acceptance_outline_journal(
            job.get("successor_acceptance_outline_journal")
        )
        entry = journal.entries[-1]
        request = entry.request
        authorization = validate_successor_acceptance_outline_control_job(
            job,
            request,
        )
        attempts = validate_successor_acceptance_outline_attempts(
            job,
            request,
        )
        allowed_phases = {
            "primary",
            "repair",
            authorization.outline_stage.regeneration_phase,
        }
        if (
            job.get("status") != "running"
            or entry.phase != "reserved"
            or str(chapter_id) != request.chapter_id
            or str(step_id) != request.step_id
            or not str(step_id).startswith(
                SUCCESSOR_ACCEPTANCE_OUTLINE_STEP_PREFIX
            )
            or phase not in allowed_phases
            or provider_alias != request.generation.provider_alias
            or type(conservative_tokens) is not int
            or not 0 < conservative_tokens <= request.maximum_tokens_per_attempt
            or len(attempts) >= request.maximum_provider_attempts
            or any(
                item.get("state") in {"claimed", "uncertain"}
                for item in attempts
            )
        ):
            raise ValueError("successor outline dispatch changed")
    except (KeyError, TypeError, ValueError, ValidationError):
        raise SuccessorAcceptanceOutlineDispatchRejected(
            "successor_acceptance_outline_dispatch_rejected"
        ) from None

    query.update({
        "job_kind": SUCCESSOR_ACCEPTANCE_OUTLINE_JOB_KIND,
        "status": "running",
        "expected_narrative_revision": request.expected_narrative_revision,
        "successor_acceptance_outline_journal": journal.model_dump(
            mode="json"
        ),
        "successor_acceptance_claim.claim_digest": request.claim_digest,
        "readiness.digest": request.readiness_digest,
        "attempt_slots": deepcopy(list(job.get("attempt_slots") or [])),
    })
    return await write_job(query, update)


def begin_successor_acceptance_outline_value(
    job: Mapping[str, Any],
    request: SuccessorAcceptanceOutlineRequest,
) -> tuple[SuccessorAcceptanceOutlineJournal, bool]:
    authorization = validate_successor_acceptance_outline_control_job(
        job,
        request,
        allowed_revision_offsets=(0, 1),
    )
    raw = job.get("successor_acceptance_outline_journal")
    if raw is None:
        if request.chapter_order != 1:
            raise SuccessorAcceptanceOutlineConflict(
                "successor_acceptance_outline_journal_prefix_missing"
            )
        if (
            job.get("status") != "running"
            or job.get("expected_narrative_revision")
            != request.expected_narrative_revision
        ):
            raise SuccessorAcceptanceOutlineConflict(
                "successor_acceptance_outline_job_not_running"
            )
        return SuccessorAcceptanceOutlineJournal(
            claim_digest=request.claim_digest,
            authorization_contract_digest=authorization.contract_digest,
            entries=(SuccessorAcceptanceOutlineEntry(
                request=request,
                phase="reserved",
            ),),
        ), True
    journal = parse_successor_acceptance_outline_journal(raw)
    if (
        journal.claim_digest != request.claim_digest
        or journal.authorization_contract_digest != authorization.contract_digest
    ):
        raise SuccessorAcceptanceOutlineConflict(
            "successor_acceptance_outline_journal_authority_changed"
        )
    index = request.chapter_order - 1
    if index < len(journal.entries):
        existing = journal.entries[index]
        if existing.request != request:
            raise SuccessorAcceptanceOutlineConflict(
                "successor_acceptance_outline_request_replay_changed"
            )
        expected_revision = request.expected_narrative_revision + (
            1 if existing.phase == "accepted" else 0
        )
        expected_status = (
            "paused" if existing.phase in {"accepted", "blocked"} else "running"
        )
        if (
            job.get("expected_narrative_revision") != expected_revision
            or job.get("status") != expected_status
        ):
            raise SuccessorAcceptanceOutlineConflict(
                "successor_acceptance_outline_replay_state_changed"
            )
        return journal, False
    if (
        index != len(journal.entries)
        or journal.entries[-1].phase != "accepted"
        or job.get("status") != "running"
        or job.get("expected_narrative_revision")
        != request.expected_narrative_revision
    ):
        raise SuccessorAcceptanceOutlineConflict(
            "successor_acceptance_outline_journal_append_invalid"
        )
    return SuccessorAcceptanceOutlineJournal(
        claim_digest=journal.claim_digest,
        authorization_contract_digest=journal.authorization_contract_digest,
        entries=journal.entries + (SuccessorAcceptanceOutlineEntry(
            request=request,
            phase="reserved",
        ),),
    ), True


def publish_successor_acceptance_outline_candidate_value(
    job: Mapping[str, Any],
    request: SuccessorAcceptanceOutlineRequest,
    provider_outline: Mapping[str, Any],
    attempt_ids: Sequence[str],
) -> tuple[SuccessorAcceptanceOutlineJournal, SuccessorAcceptanceOutlineJournal]:
    authorization = validate_successor_acceptance_outline_control_job(
        job,
        request,
    )
    journal = parse_successor_acceptance_outline_journal(
        job.get("successor_acceptance_outline_journal")
    )
    index = request.chapter_order - 1
    if index >= len(journal.entries) or journal.entries[index].request != request:
        raise SuccessorAcceptanceOutlineConflict(
            "successor_acceptance_outline_entry_changed"
        )
    candidate = validate_required_book_successor_acceptance_outline(
        authorization,
        provider_outline,
    )
    produced = journal.entries[index].model_copy(update={
        "phase": "produced",
        "provider_outline": candidate,
        "provider_outline_revision": prose_revision(candidate),
        "attempt_ids": tuple(attempt_ids),
    })
    validate_successor_acceptance_outline_attempts(
        job,
        request,
        expected_accounted_attempt_ids=tuple(attempt_ids),
    )
    current = journal.entries[index]
    if current.phase == "produced":
        if current != produced:
            raise SuccessorAcceptanceOutlineConflict(
                "successor_acceptance_outline_candidate_replay_changed"
            )
        return journal, journal
    if current.phase != "reserved" or index != len(journal.entries) - 1:
        raise SuccessorAcceptanceOutlineConflict(
            "successor_acceptance_outline_candidate_order_changed"
        )
    updated = journal.model_copy(
        update={"entries": journal.entries[:-1] + (produced,)}
    )
    return journal, SuccessorAcceptanceOutlineJournal.model_validate(updated)


def publish_successor_acceptance_outline_accepted_value(
    job: Mapping[str, Any],
    request: SuccessorAcceptanceOutlineRequest,
    formal_outline_revision: str,
) -> tuple[SuccessorAcceptanceOutlineJournal, SuccessorAcceptanceOutlineJournal]:
    validate_successor_acceptance_outline_control_job(job, request)
    journal = parse_successor_acceptance_outline_journal(
        job.get("successor_acceptance_outline_journal")
    )
    index = request.chapter_order - 1
    if index >= len(journal.entries) or journal.entries[index].request != request:
        raise SuccessorAcceptanceOutlineConflict(
            "successor_acceptance_outline_entry_changed"
        )
    current = journal.entries[index]
    if (
        current.provider_outline_revision != formal_outline_revision
        or current.phase not in {"produced", "accepted"}
    ):
        raise SuccessorAcceptanceOutlineConflict(
            "successor_acceptance_outline_formal_revision_changed"
        )
    accepted = current.model_copy(update={
        "phase": "accepted",
        "formal_outline_revision": formal_outline_revision,
    })
    if current.phase == "accepted":
        if current != accepted:
            raise SuccessorAcceptanceOutlineConflict(
                "successor_acceptance_outline_acceptance_replay_changed"
            )
        return journal, journal
    updated = journal.model_copy(update={
        "entries": journal.entries[:index] + (accepted,) + journal.entries[index + 1:]
    })
    return journal, SuccessorAcceptanceOutlineJournal.model_validate(updated)


def block_successor_acceptance_outline_value(
    job: Mapping[str, Any],
    request: SuccessorAcceptanceOutlineRequest,
    failure_code: str,
) -> tuple[SuccessorAcceptanceOutlineJournal, SuccessorAcceptanceOutlineJournal]:
    validate_successor_acceptance_outline_control_job(job, request)
    if _SAFE_REASON.fullmatch(failure_code) is None:
        raise SuccessorAcceptanceOutlineConflict(
            "successor_acceptance_outline_failure_code_invalid"
        )
    journal = parse_successor_acceptance_outline_journal(
        job.get("successor_acceptance_outline_journal")
    )
    index = request.chapter_order - 1
    if index >= len(journal.entries) or journal.entries[index].request != request:
        raise SuccessorAcceptanceOutlineConflict(
            "successor_acceptance_outline_entry_changed"
        )
    current = journal.entries[index]
    if current.phase == "accepted":
        raise SuccessorAcceptanceOutlineConflict(
            "successor_acceptance_outline_accepted_cannot_block"
        )
    blocked = current.model_copy(update={
        "phase": "blocked",
        "failure_code": failure_code,
    })
    if current.phase == "blocked":
        if current != blocked:
            raise SuccessorAcceptanceOutlineConflict(
                "successor_acceptance_outline_failure_replay_changed"
            )
        return journal, journal
    updated = journal.model_copy(update={
        "entries": journal.entries[:index] + (blocked,) + journal.entries[index + 1:]
    })
    return journal, SuccessorAcceptanceOutlineJournal.model_validate(updated)


def build_successor_acceptance_outline_request(
    *,
    readiness: Mapping[str, Any],
    journal: SuccessorAcceptanceRunJournal,
    operation_digest: str,
    chapter_order: int,
) -> SuccessorAcceptanceOutlineRequest:
    authorization = validate_required_book_successor_acceptance_readiness(
        readiness
    )
    progress = required_book_successor_acceptance_ledger.inspect(
        readiness,
        journal,
    )
    if (
        progress.status != "running"
        or progress.phase not in {"fixture_ready", "outlines"}
        or progress.accepted_outline_count != chapter_order - 1
        or not re.fullmatch(_SHA256, str(operation_digest))
    ):
        raise SuccessorAcceptanceOutlineConflict(
            "successor_acceptance_outline_parent_phase_changed"
        )
    identity = SuccessorAcceptanceFixtureIdentity.from_claim(journal.claim)
    stage = authorization.outline_stage
    request = {
        "schema_version": "successor_acceptance_outline_request.v1",
        "operation_digest": operation_digest,
        "claim_digest": journal.claim.claim_digest,
        "readiness_digest": str(readiness.get("digest") or ""),
        "authorization_contract_digest": authorization.contract_digest,
        "outline_job_id": identity.outline_job_id,
        "owner_id": identity.owner_id,
        "novel_id": identity.novel_id,
        "chapter_id": identity.chapter_ids[chapter_order - 1],
        "chapter_order": chapter_order,
        "expected_narrative_revision": chapter_order - 1,
        "authorization_revision": authorization.authorization_revision,
        "generation": stage.generation,
        "maximum_provider_attempts": 2,
        "maximum_input_tokens_per_attempt": (
            stage.maximum_input_tokens_per_attempt
        ),
        "maximum_output_tokens_per_attempt": (
            stage.maximum_output_tokens_per_attempt
        ),
        "maximum_tokens_per_attempt": (
            stage.maximum_input_tokens_per_attempt
            + stage.maximum_output_tokens_per_attempt
        ),
    }
    return SuccessorAcceptanceOutlineRequest(
        **request,
        request_digest=required_book_successor_digest(request),
    )


def build_successor_acceptance_outline_control_job(
    *,
    readiness: Mapping[str, Any],
    journal: SuccessorAcceptanceRunJournal,
) -> dict[str, Any]:
    authorization = validate_required_book_successor_acceptance_readiness(
        readiness
    )
    identity = SuccessorAcceptanceFixtureIdentity.from_claim(journal.claim)
    if (
        journal.claim.readiness_digest != readiness.get("digest")
        or journal.claim.authorization_contract_digest
        != authorization.contract_digest
    ):
        raise SuccessorAcceptanceOutlineConflict(
            "successor_acceptance_outline_claim_changed"
        )
    return {
        "_id": ObjectId(identity.outline_job_id),
        "novel_id": ObjectId(identity.novel_id),
        "owner_id": ObjectId(identity.owner_id),
        "scope": "book",
        "volume_id": None,
        "job_kind": SUCCESSOR_ACCEPTANCE_OUTLINE_JOB_KIND,
        "status": "running",
        "pause_reason": None,
        "active_slot": "global",
        "token_budget": authorization.outline_stage.maximum_tokens_total,
        "tokens_used": 0,
        "tokens_reserved": 0,
        "active_token_reservations": [],
        "usage_attempt_capacity": (
            authorization.outline_stage.maximum_provider_attempts_total
        ),
        "usage_attempt_claimed": 0,
        "usage_attempt_ids": [],
        "usage_attempt_summaries": [],
        "attempt_slots": [],
        "attempt_reservation": None,
        "has_uncertain_attempts": False,
        "uncertain_attempt_ids": [],
        "state_dispatch_resolution": None,
        "execution_epoch": 0,
        "execution_lease": None,
        "expected_narrative_revision": 0,
        "authorization_revision": authorization.authorization_revision,
        "generation_params": {},
        "readiness": deepcopy(dict(readiness)),
        "successor_acceptance_claim": journal.claim.model_dump(mode="json"),
        "successor_acceptance_outline_journal": None,
        "progress": [],
        "current_chapter_id": None,
        "required_initial_prose_journal": None,
        "required_prose_rewrite_journal": None,
        "required_adherence_journal": None,
        "required_reviewed_candidate": None,
        "required_state_candidate_journal": None,
        "required_state_candidate": None,
        "required_chapter_finalization_result": None,
        "required_book_successor_journal": None,
        "required_book_successor_recovery_checkpoint": None,
        "required_book_successor_action": None,
        "required_book_successor_parent_job_id": None,
        "candidate_pipeline_checkpoints": [],
        "candidate_manual_takeover": None,
        "candidate_manual_takeover_events": [],
        "chapter_completion_decisions": [],
        "reference_card_auto_creation_events": [],
        "reference_card_repair_events": [],
        "diagnostics": [],
        "diagnostic_schema_version": 1,
        "current_failure_event_id": None,
        "error": None,
        "is_deleted": False,
        "deleted_at": None,
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def project_formal_outline(
    authorization: RequiredBookSuccessorAcceptanceAuthorization,
    stored_outline: Mapping[str, Any],
) -> dict[str, Any]:
    raw = {
        name: _jsonable(stored_outline.get(name))
        for name in ChapterOutlineResultSchema.model_fields
        if name in stored_outline
    }
    raw["new_threads"] = []
    raw["new_reference_card_candidates"] = []
    return validate_required_book_successor_acceptance_outline(
        authorization,
        raw,
    )


class RequiredSuccessorAcceptanceOutlineJobRunner:
    """Drive exactly one ordered chapter through the durable outline journal."""

    def __init__(
        self,
        *,
        readiness: Mapping[str, Any],
        journal: SuccessorAcceptanceRunJournal,
        repository: SuccessorAcceptanceOutlineRepository,
        get_chapter: Callable[[str], Awaitable[Mapping[str, Any]]],
        get_narrative_revision: Callable[[str], Awaitable[int]],
        generate_outline: Callable[..., Awaitable[tuple]],
        accept_outline: Callable[..., Awaitable[Mapping[str, Any]]],
        attempt_scope_factory: Callable[..., Any] = JobAttemptScope,
    ) -> None:
        self._readiness = deepcopy(dict(readiness))
        self._journal = journal
        self._repository = repository
        self._get_chapter = get_chapter
        self._get_narrative_revision = get_narrative_revision
        self._generate_outline = generate_outline
        self._accept_outline = accept_outline
        self._attempt_scope_factory = attempt_scope_factory
        self._authorization = (
            validate_required_book_successor_acceptance_readiness(readiness)
        )

    async def run(
        self,
        *,
        operation_digest: str,
        chapter_order: int,
    ) -> SuccessorAcceptanceOutlineJobOutcome:
        request = build_successor_acceptance_outline_request(
            readiness=self._readiness,
            journal=self._journal,
            operation_digest=operation_digest,
            chapter_order=chapter_order,
        )
        await self._repository.begin_successor_acceptance_outline(
            request.outline_job_id,
            request,
        )
        entry = await self._entry(request)
        if entry.phase == "blocked":
            return await self._blocked_outcome(request, entry.failure_code or "")
        if entry.phase == "accepted":
            return await self._ready_outcome(request, entry)
        if entry.phase == "reserved":
            produced = await self._produce(request)
            if produced is not None:
                return produced
            entry = await self._entry(request)
        if entry.phase != "produced":
            raise SuccessorAcceptanceOutlineConflict(
                "successor_acceptance_outline_phase_changed"
            )
        return await self._accept(request, entry)

    async def _produce(
        self,
        request: SuccessorAcceptanceOutlineRequest,
    ) -> SuccessorAcceptanceOutlineJobOutcome | None:
        for _ in range(request.maximum_provider_attempts):
            slots = await self._slots(request)
            if any(item.get("state") == "claimed" for item in slots):
                raise SuccessorAcceptanceOutlineConflict(
                    "successor_acceptance_outline_attempt_inflight"
                )
            if any(item.get("state") == "uncertain" for item in slots):
                return await self._block(request, "outline_attempt_uncertain")
            if any(item.get("state") == "accounted" for item in slots):
                return await self._block(
                    request,
                    "outline_result_confirmation_lost",
                )
            remaining = request.maximum_provider_attempts - len(slots)
            if remaining <= 0:
                return await self._block(
                    request,
                    "outline_attempt_capacity_exhausted",
                )
            await self._repository.reserve_attempts(
                request.outline_job_id,
                request.chapter_id,
                remaining,
            )
            scope = self._attempt_scope_factory(
                request.outline_job_id,
                request.chapter_id,
                request.step_id,
                repo=self._repository,
                existing_attempt_slots=slots,
            )
            chapter = await self._checked_chapter(request)
            try:
                result = await self._generate_outline(
                    request.novel_id,
                    dict(chapter),
                    attempt_scope=scope,
                    generation_params={},
                    generation_plan=request.generation.thaw(),
                )
            except Exception:  # noqa: BLE001 - classify from durable slots
                await self._repository.finish_attempt_reservation(
                    request.outline_job_id,
                    request.chapter_id,
                )
                latest = await self._slots(request)
                if (
                    latest
                    and all(
                        item.get("state") == "released_pre_dispatch"
                        for item in latest
                    )
                    and len(latest) < request.maximum_provider_attempts
                ):
                    continue
                if any(item.get("state") == "uncertain" for item in latest):
                    return await self._block(
                        request,
                        "outline_attempt_uncertain",
                    )
                if any(item.get("state") == "accounted" for item in latest):
                    return await self._block(
                        request,
                        "outline_result_confirmation_lost",
                    )
                return await self._block(request, "outline_generation_failed")
            finally:
                current = await self._repository.get_job(
                    request.outline_job_id
                )
                if current.get("attempt_reservation") is not None:
                    await self._repository.finish_attempt_reservation(
                        request.outline_job_id,
                        request.chapter_id,
                    )
            try:
                raw_outline = result[0]
                result_attempts = tuple(
                    str(item.get("attempt_id") or "")
                    for item in result[4]
                    if isinstance(item, Mapping)
                )
                provider_outline = (
                    validate_required_book_successor_acceptance_outline(
                        self._authorization,
                        raw_outline,
                    )
                )
            except (IndexError, TypeError, ValueError):
                return await self._block(request, "outline_shape_changed")
            slots = await self._slots(request)
            accounted = tuple(
                str(item.get("attempt_id") or "")
                for item in slots
                if item.get("state") == "accounted"
            )
            if (
                not accounted
                or result_attempts != accounted
                or any(
                    item.get("state")
                    not in {"accounted", "released_pre_dispatch"}
                    for item in slots
                )
            ):
                return await self._block(
                    request,
                    "outline_attempt_evidence_changed",
                )
            await self._repository.publish_successor_acceptance_outline_candidate(
                request.outline_job_id,
                request,
                provider_outline,
                accounted,
            )
            return None
        return await self._block(request, "outline_attempt_capacity_exhausted")

    async def _accept(
        self,
        request: SuccessorAcceptanceOutlineRequest,
        entry: SuccessorAcceptanceOutlineEntry,
    ) -> SuccessorAcceptanceOutlineJobOutcome:
        assert entry.provider_outline is not None
        chapter = await self._checked_chapter(request)
        current_revision = await self._get_narrative_revision(request.novel_id)
        formal = await self._recover_formal(request, chapter, current_revision)
        if formal is None:
            if (
                chapter.get("outline") is not None
                or current_revision != request.expected_narrative_revision
            ):
                return await self._block(request, "outline_source_stale")
            binding = request.mutation_binding
            try:
                await self._accept_outline(
                    request.chapter_id,
                    deepcopy(entry.provider_outline),
                    edited_by_human=False,
                    expected_narrative_revision=(
                        request.expected_narrative_revision
                    ),
                    idempotency_key=binding.idempotency_key,
                    job_mutation_binding=binding,
                )
            except Exception:  # noqa: BLE001 - re-read idempotent mutation
                pass
            chapter = await self._checked_chapter(request)
            current_revision = await self._get_narrative_revision(
                request.novel_id
            )
            formal = await self._recover_formal(
                request,
                chapter,
                current_revision,
            )
        if formal is None:
            return await self._block(request, "outline_acceptance_failed")
        revision = prose_revision(formal)
        await self._repository.publish_successor_acceptance_outline_accepted(
            request.outline_job_id,
            request,
            revision,
        )
        return await self._ready_outcome(request, await self._entry(request))

    async def _recover_formal(
        self,
        request: SuccessorAcceptanceOutlineRequest,
        chapter: Mapping[str, Any],
        current_revision: int,
    ) -> dict[str, Any] | None:
        raw = chapter.get("outline")
        if not isinstance(raw, Mapping):
            return None
        try:
            formal = project_formal_outline(self._authorization, raw)
        except (TypeError, ValueError):
            return None
        entry = await self._entry(request)
        if (
            entry.provider_outline_revision != prose_revision(formal)
            or current_revision != request.expected_narrative_revision + 1
        ):
            return None
        return formal

    async def _ready_outcome(
        self,
        request: SuccessorAcceptanceOutlineRequest,
        entry: SuccessorAcceptanceOutlineEntry,
    ) -> SuccessorAcceptanceOutlineJobOutcome:
        if entry.phase != "accepted" or entry.provider_outline is None:
            raise SuccessorAcceptanceOutlineConflict(
                "successor_acceptance_outline_not_accepted"
            )
        chapter = await self._checked_chapter(request)
        formal = project_formal_outline(
            self._authorization,
            chapter.get("outline") or {},
        )
        if (
            prose_revision(formal) != entry.formal_outline_revision
            or await self._get_narrative_revision(request.novel_id)
            < request.expected_narrative_revision + 1
        ):
            raise SuccessorAcceptanceOutlineConflict(
                "successor_acceptance_outline_formal_changed"
            )
        return SuccessorAcceptanceOutlineJobOutcome(
            status="ready",
            chapter_order=request.chapter_order,
            provider_outline=deepcopy(entry.provider_outline),
            formal_outline=formal,
            attempts=self._attempt_receipts(
                await self._slots(request),
                request,
            ),
        )

    async def _block(
        self,
        request: SuccessorAcceptanceOutlineRequest,
        failure_code: str,
    ) -> SuccessorAcceptanceOutlineJobOutcome:
        await self._repository.block_successor_acceptance_outline(
            request.outline_job_id,
            request,
            failure_code,
        )
        return await self._blocked_outcome(request, failure_code)

    async def _blocked_outcome(
        self,
        request: SuccessorAcceptanceOutlineRequest,
        failure_code: str,
    ) -> SuccessorAcceptanceOutlineJobOutcome:
        return SuccessorAcceptanceOutlineJobOutcome(
            status="blocked",
            chapter_order=request.chapter_order,
            provider_outline=None,
            formal_outline=None,
            attempts=self._attempt_receipts(
                await self._slots(request),
                request,
            ),
            failure_code=failure_code,
        )

    async def _entry(
        self,
        request: SuccessorAcceptanceOutlineRequest,
    ) -> SuccessorAcceptanceOutlineEntry:
        job = await self._repository.get_job(request.outline_job_id)
        journal = parse_successor_acceptance_outline_journal(
            job.get("successor_acceptance_outline_journal")
        )
        index = request.chapter_order - 1
        if index >= len(journal.entries):
            raise SuccessorAcceptanceOutlineConflict(
                "successor_acceptance_outline_entry_missing"
            )
        entry = journal.entries[index]
        if entry.request != request:
            raise SuccessorAcceptanceOutlineConflict(
                "successor_acceptance_outline_request_changed"
            )
        return entry

    async def _checked_chapter(
        self,
        request: SuccessorAcceptanceOutlineRequest,
    ) -> Mapping[str, Any]:
        chapter = await self._get_chapter(request.chapter_id)
        if (
            str(chapter.get("_id") or "") != request.chapter_id
            or str(chapter.get("novel_id") or "") != request.novel_id
            or int(chapter.get("order_index") or 0) != request.chapter_order
        ):
            raise SuccessorAcceptanceOutlineConflict(
                "successor_acceptance_outline_chapter_changed"
            )
        return chapter

    async def _slots(
        self,
        request: SuccessorAcceptanceOutlineRequest,
    ) -> tuple[Mapping[str, Any], ...]:
        return tuple(await self._repository.list_attempt_slots(
            request.outline_job_id,
            chapter_id=request.chapter_id,
            step_prefix=request.step_id,
        ))

    def _attempt_receipts(
        self,
        slots: Sequence[Mapping[str, Any]],
        request: SuccessorAcceptanceOutlineRequest,
    ) -> tuple[Mapping[str, Any], ...]:
        return project_successor_acceptance_outline_attempt_receipts(
            self._authorization,
            slots,
            request,
        )


__all__ = [
    "MAX_SUCCESSOR_ACCEPTANCE_OUTLINE_ATTEMPTS_PER_CHAPTER",
    "RequiredSuccessorAcceptanceOutlineJobRunner",
    "SUCCESSOR_ACCEPTANCE_OUTLINE_JOB_KIND",
    "SUCCESSOR_ACCEPTANCE_OUTLINE_STEP_PREFIX",
    "SUCCESSOR_ACCEPTANCE_OUTLINES_READY",
    "SuccessorAcceptanceOutlineConflict",
    "SuccessorAcceptanceOutlineDispatchRejected",
    "SuccessorAcceptanceOutlineEntry",
    "SuccessorAcceptanceOutlineJobOutcome",
    "SuccessorAcceptanceOutlineJournal",
    "SuccessorAcceptanceOutlineRepository",
    "SuccessorAcceptanceOutlineRequest",
    "build_successor_acceptance_outline_control_job",
    "build_successor_acceptance_outline_request",
    "begin_successor_acceptance_outline_value",
    "block_successor_acceptance_outline_value",
    "parse_successor_acceptance_outline_journal",
    "project_successor_acceptance_outline_attempt_receipts",
    "project_formal_outline",
    "publish_successor_acceptance_outline_accepted_value",
    "publish_successor_acceptance_outline_candidate_value",
    "validate_successor_acceptance_outline_attempts",
    "validate_successor_acceptance_outline_control_job",
    "write_successor_acceptance_outline_claim",
]
