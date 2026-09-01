"""Append-only Job journal for the bounded successor state stage."""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
import json
from typing import Any, Literal

from bson.int64 import Int64
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from backend.services.generation.required_chapter_state_contracts import (
    required_state_digest,
)
from backend.services.generation.required_chapter_state_job import (
    MAX_REQUIRED_STATE_CALLS,
    REQUIRED_STATE_STEP_PREFIX,
    RequiredChapterStateAuthorization,
    RequiredStateCandidateObservation,
    RequiredStateCandidateRequest,
    RequiredStateDispatchRejected,
    validate_required_chapter_state_readiness,
)


class _Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class RequiredStateCandidateJournalEntry(_Closed):
    schema_version: Literal["required_state_candidate_journal_entry.v1"] = (
        "required_state_candidate_journal_entry.v1"
    )
    request: RequiredStateCandidateRequest
    phase: Literal["reserved", "produced"]
    observation: RequiredStateCandidateObservation | None = None

    @model_validator(mode="after")
    def validate_entry(self) -> "RequiredStateCandidateJournalEntry":
        if (self.phase == "produced") != (self.observation is not None):
            raise ValueError("required_state_candidate_journal_phase_invalid")
        return self


class RequiredStateCandidateJournal(_Closed):
    schema_version: Literal["required_state_candidate_journal.v1"] = (
        "required_state_candidate_journal.v1"
    )
    authorization_contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    entries: tuple[RequiredStateCandidateJournalEntry, ...] = Field(
        min_length=1,
        max_length=MAX_REQUIRED_STATE_CALLS,
    )

    @model_validator(mode="after")
    def validate_chain(self) -> "RequiredStateCandidateJournal":
        first = self.entries[0].request.binding
        for index, entry in enumerate(self.entries):
            binding = entry.request.binding
            if (
                binding.ordinal != index
                or binding.job_id != first.job_id
                or binding.owner_id != first.owner_id
                or binding.novel_id != first.novel_id
                or binding.chapter_id != first.chapter_id
                or binding.readiness_digest != first.readiness_digest
                or binding.authorization_revision
                != first.authorization_revision
                or binding.expected_narrative_revision
                != first.expected_narrative_revision
                or binding.predecessor_job_id != first.predecessor_job_id
                or binding.predecessor_result_digest
                != first.predecessor_result_digest
                or binding.source_run_id != first.source_run_id
                or binding.source_run_revision != first.source_run_revision
                or binding.source_content_digest != first.source_content_digest
            ):
                raise ValueError("required_state_candidate_journal_chain_changed")
            if index == 0:
                continue
            previous = self.entries[index - 1]
            if (
                previous.phase != "produced"
                or previous.observation is None
                or previous.observation.gate_passed
                or entry.request.prior_proposal_id
                != previous.observation.proposal_id
            ):
                raise ValueError("required_state_candidate_journal_order_invalid")
        if any(
            entry.phase == "reserved" for entry in self.entries[:-1]
        ):
            raise ValueError("required_state_candidate_journal_reserved_not_last")
        return self


def parse_required_state_candidate_journal(
    value: Any,
) -> RequiredStateCandidateJournal:
    try:
        return RequiredStateCandidateJournal.model_validate_json(
            json.dumps(value, default=str)
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise ValueError("required_state_candidate_journal_invalid") from exc


def required_state_journal_digest(
    journal: RequiredStateCandidateJournal,
) -> str:
    return required_state_digest(journal.model_dump(mode="json"))


def _stored_integer(value: Any) -> bool:
    return type(value) in {int, Int64} and 0 <= int(value) <= 2**63 - 1


def _attempts_for_entry(
    job: Mapping[str, Any],
    entry: RequiredStateCandidateJournalEntry,
) -> tuple[Mapping[str, Any], ...]:
    slots = job.get("attempt_slots")
    if not isinstance(slots, list):
        raise ValueError("required_state_attempt_ledger_invalid")
    return tuple(
        item
        for item in slots
        if isinstance(item, Mapping)
        and item.get("chapter_id") == entry.request.binding.chapter_id
        and item.get("step_id") == entry.request.step_id
    )


def validate_required_state_attempt_accounting(
    job: Mapping[str, Any],
    entry: RequiredStateCandidateJournalEntry,
    authorization: RequiredChapterStateAuthorization,
) -> tuple[Mapping[str, Any], ...]:
    # Reuse the Job-wide conservation proof.  This stage has its own fresh Job,
    # so every physical attempt in that ledger belongs to the state successor.
    from backend.db.required_adherence_journal import _checked_bookkeeping

    _checked_bookkeeping(job)
    attempts = _attempts_for_entry(job, entry)
    maximum = authorization.maximum_provider_attempts_per_call
    if not 0 <= len(attempts) <= maximum:
        raise ValueError("required_state_attempt_count_invalid")
    accounted_attempt_ids = tuple(
        item.get("attempt_id")
        for item in attempts
        if item.get("state") == "accounted"
    )
    if entry.phase == "produced" and (
        not accounted_attempt_ids
        or entry.observation is None
        or accounted_attempt_ids != entry.observation.attempt_ids
    ):
        raise ValueError("required_state_attempt_identity_invalid")
    allowed_providers = {item.provider_alias for item in authorization.provider_bounds}
    maximum_tokens = authorization.maximum_tokens_per_call
    seen: set[str] = set()
    for attempt in attempts:
        attempt_id = attempt.get("attempt_id")
        usage = attempt.get("usage")
        if (
            not isinstance(attempt_id, str)
            or not attempt_id
            or attempt_id in seen
            or attempt.get("provider_alias") not in allowed_providers
            or not isinstance(attempt.get("phase"), str)
            or not attempt.get("phase")
            or not _stored_integer(attempt.get("conservative_tokens"))
            or not 0 < int(attempt["conservative_tokens"]) <= maximum_tokens
        ):
            raise ValueError("required_state_attempt_identity_invalid")
        seen.add(attempt_id)
        if entry.phase == "produced" and attempt.get("state") == "accounted":
            if (
                not isinstance(usage, Mapping)
                or set(usage) != {
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                }
                or any(not _stored_integer(value) for value in usage.values())
                or int(usage["input_tokens"]) + int(usage["output_tokens"])
                != int(usage["total_tokens"])
                or attempt.get("charged_tokens") != usage["total_tokens"]
                or not 0 < int(usage["total_tokens"])
                <= int(attempt["conservative_tokens"])
            ):
                raise ValueError("required_state_attempt_accounting_invalid")
        elif entry.phase == "produced" and (
            attempt.get("state") != "released_pre_dispatch"
            or usage is not None
            or attempt.get("charged_tokens") not in (None, 0)
        ):
            raise ValueError("required_state_attempt_accounting_invalid")
    if entry.phase == "reserved" and any(
        item.get("state") not in {
            "claimed",
            "accounted",
            "released_pre_dispatch",
            "uncertain",
            "uncertain_retry_acknowledged",
            "uncertain_skip_acknowledged",
            "uncertain_abort_acknowledged",
        }
        for item in attempts
    ):
        raise ValueError("required_state_attempt_state_invalid")
    return attempts


def _validate_job_request(
    job: Mapping[str, Any],
    request: RequiredStateCandidateRequest,
) -> RequiredChapterStateAuthorization:
    authorization = validate_required_chapter_state_readiness(job["readiness"])
    binding = request.binding
    if (
        str(job.get("_id")) != binding.job_id
        or str(job.get("owner_id")) != binding.owner_id
        or str(job.get("novel_id")) != binding.novel_id
        or job.get("is_deleted") is not False
        or job.get("status") != "running"
        or job.get("current_chapter_id") != binding.chapter_id
        or job.get("authorization_revision")
        != binding.authorization_revision
        or job.get("expected_narrative_revision")
        != binding.expected_narrative_revision
        or job["readiness"].get("digest") != binding.readiness_digest
        or authorization.contract_digest
        != job["readiness"]["planning"][
            "required_chapter_state_authorization"
        ]["contract_digest"]
        or authorization.owner_id != binding.owner_id
        or authorization.novel_id != binding.novel_id
        or authorization.chapter_id != binding.chapter_id
        or authorization.authorization_revision
        != binding.authorization_revision
        or authorization.narrative_revision
        != binding.expected_narrative_revision
        or authorization.predecessor_candidate.job_id
        != binding.predecessor_job_id
        or authorization.predecessor_candidate.result_digest
        != binding.predecessor_result_digest
        or authorization.predecessor_candidate.source_run_id
        != binding.source_run_id
        or authorization.predecessor_candidate.source_run_revision
        != binding.source_run_revision
        or authorization.predecessor_candidate.source_content_digest
        != binding.source_content_digest
        or job.get("required_state_candidate") is not None
        or job.get("progress") not in (None, [])
    ):
        raise ValueError("required_state_candidate_job_contract_invalid")
    return authorization


def begin_required_state_entry_value(
    job: Mapping[str, Any],
    request: RequiredStateCandidateRequest,
) -> tuple[RequiredStateCandidateJournal, bool]:
    authorization = _validate_job_request(job, request)
    raw = job.get("required_state_candidate_journal")
    if raw is None:
        if request.binding.ordinal != 0:
            raise ValueError("required_state_candidate_journal_missing_prefix")
        journal = RequiredStateCandidateJournal(
            authorization_contract_digest=authorization.contract_digest,
            entries=(RequiredStateCandidateJournalEntry(
                request=request,
                phase="reserved",
            ),),
        )
        return journal, True
    journal = parse_required_state_candidate_journal(raw)
    if journal.authorization_contract_digest != authorization.contract_digest:
        raise ValueError("required_state_candidate_authority_changed")
    expected_ordinal = len(journal.entries)
    latest = journal.entries[-1]
    if request.binding.ordinal < expected_ordinal:
        existing = journal.entries[request.binding.ordinal]
        if existing.request != request:
            raise ValueError("required_state_candidate_request_replay_diverged")
        return journal, False
    if (
        request.binding.ordinal != expected_ordinal
        or latest.phase != "produced"
        or latest.observation is None
        or latest.observation.gate_passed
    ):
        raise ValueError("required_state_candidate_journal_append_invalid")
    appended = RequiredStateCandidateJournal(
        authorization_contract_digest=journal.authorization_contract_digest,
        entries=journal.entries + (RequiredStateCandidateJournalEntry(
            request=request,
            phase="reserved",
        ),),
    )
    return appended, True


async def write_required_state_claim(
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
    """Fence a physical attempt to one already-persisted request entry."""

    try:
        authorization = validate_required_chapter_state_readiness(job["readiness"])
        journal = parse_required_state_candidate_journal(
            job.get("required_state_candidate_journal")
        )
        entry = journal.entries[-1]
        existing = _attempts_for_entry(job, entry)
        allowed_providers = {
            item.provider_alias for item in authorization.provider_bounds
        }
        if (
            entry.phase != "reserved"
            or entry.request.binding.chapter_id != str(chapter_id)
            or entry.request.step_id != str(step_id)
            or not str(step_id).startswith(REQUIRED_STATE_STEP_PREFIX)
            or not isinstance(phase, str)
            or not phase
            or provider_alias not in allowed_providers
            or conservative_tokens > authorization.maximum_tokens_per_call
            or len(existing) >= authorization.maximum_provider_attempts_per_call
        ):
            raise ValueError("required state dispatch changed")
        _validate_job_request(job, entry.request)
        validate_required_state_attempt_accounting(job, entry, authorization)
    except (KeyError, TypeError, ValueError, ValidationError):
        raise RequiredStateDispatchRejected(
            "required_state_dispatch_rejected"
        ) from None
    query.update({
        "required_state_candidate_journal": journal.model_dump(mode="json"),
        "required_state_candidate": None,
        "required_initial_prose_journal": None,
        "required_prose_rewrite_journal": None,
        "required_adherence_journal": None,
        "required_reviewed_candidate": None,
        "readiness.planning.required_chapter_state_pipeline_revision": (
            "required-chapter-state-job-r1"
        ),
        "readiness.planning.required_chapter_state_authorization.contract_digest": (
            authorization.contract_digest
        ),
    })
    return await write_job(query, update)


def observation_entry_value(
    job: Mapping[str, Any],
    request: RequiredStateCandidateRequest,
    observation: RequiredStateCandidateObservation,
) -> tuple[RequiredStateCandidateJournal, RequiredStateCandidateJournal]:
    authorization = _validate_job_request(job, request)
    journal = parse_required_state_candidate_journal(
        job.get("required_state_candidate_journal")
    )
    index = request.binding.ordinal
    if index >= len(journal.entries):
        raise ValueError("required_state_candidate_entry_missing")
    entry = journal.entries[index]
    if entry.request != request:
        raise ValueError("required_state_candidate_request_changed")
    if entry.phase == "produced":
        if entry.observation != observation:
            raise ValueError("required_state_candidate_observation_changed")
        return journal, journal
    if index != len(journal.entries) - 1:
        raise ValueError("required_state_candidate_observation_order_invalid")
    produced = entry.model_copy(update={
        "phase": "produced",
        "observation": observation,
    })
    updated = RequiredStateCandidateJournal(
        authorization_contract_digest=journal.authorization_contract_digest,
        entries=journal.entries[:-1] + (produced,),
    )
    attempt_ids = tuple(
        item.get("attempt_id")
        for item in validate_required_state_attempt_accounting(
            job,
            produced,
            authorization,
        )
        if item.get("state") == "accounted"
    )
    if attempt_ids != observation.attempt_ids:
        raise ValueError("required_state_candidate_attempts_changed")
    return journal, updated
