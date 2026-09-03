"""Opt-in Job persistence for ADR-0008; never a formal-content writer.

The factory on GenerationJobRepository supplies its lease-aware I/O. The live
candidate is an in-process snapshot from the existing ID-whitelisted context
builder, not data restored from the Job journal. No production entry point
constructs the new authorization yet.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from bson.int64 import Int64
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from backend.db.narrative_revision import narrative_revision_store
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db.repositories.prose_run_repository import prose_run_repo
from backend.db.utils import get_utc_now
from backend.llm.models import TokenUsage
from backend.services.generation.independent_outline_review import IndependentReviewPlan
from backend.services.generation.attempt_ledger_contracts import (
    MAX_PERSISTED_ATTEMPT_TOKENS, PERSISTED_ATTEMPT_STATES,
)
from backend.services.generation.job_execution import current_job_execution
from backend.services.generation.prose_runs import prose_revision
from backend.services.generation.required_adherence_capacity import RequiredAdherenceCapacity
from backend.services.generation.required_adherence_handoff import (
    AwaitingAdherenceReceipt, InitialAwaitingAdherenceReceipt,
    RequiredAdherenceCheckpoint, RequiredAdherenceHandoff,
    RequiredAdherenceHandoffError, RequiredAdherenceReceipt,
    RequiredReviewCandidate, required_review_ordinal,
)
from backend.services.llm.generation_runtime import AttemptUsage


_MAX = 2**63 - 1
REVIEW_STEP_PREFIX = "required-adherence:"


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _integer(value: Any) -> bool:
    return type(value) in {int, Int64} and 0 <= value <= _MAX


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class RequiredReviewJobBinding(_Closed):
    job_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    owner_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    novel_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    chapter_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    readiness_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_revision: int = Field(ge=1, le=_MAX)
    narrative_revision: int = Field(ge=0, le=_MAX)


class _ReviewAuthorization(_Closed):
    schema_version: Literal["required_adherence_job_authorization.v1"]
    chapter_ids: list[str] = Field(min_length=1, max_length=1000)
    capacity: RequiredAdherenceCapacity
    provider_alias: str = Field(min_length=1, max_length=64)
    deadline_at: datetime

    @model_validator(mode="after")
    def validate_identity(self) -> "_ReviewAuthorization":
        if (
            self.deadline_at.tzinfo is None
            or len(set(self.chapter_ids)) != len(self.chapter_ids)
            or any(len(value) != 24 or any(char not in "0123456789abcdef" for char in value) for value in self.chapter_ids)
        ):
            raise ValueError("review_authorization_invalid")
        return self


class _ReviewDispatchOwner(_Closed):
    epoch: int = Field(ge=1, le=_MAX)
    worker_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class _ReviewEntry(_Closed):
    checkpoint: RequiredAdherenceCheckpoint
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    outline_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    dispatch_owner: _ReviewDispatchOwner | None = None

    @model_validator(mode="after")
    def require_started_owner(self) -> "_ReviewEntry":
        if (self.checkpoint.phase == "awaiting_review") != (self.dispatch_owner is None):
            raise ValueError("review_dispatch_owner_invalid")
        return self


class _ReviewLedger(_Closed):
    schema_version: Literal["required_adherence_job_journal.v1", "required_adherence_job_journal.v2"]
    writer_contract_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    binding: RequiredReviewJobBinding
    authorization: _ReviewAuthorization
    entries: list[_ReviewEntry] = Field(default_factory=list, max_length=3)
    reserved_attempts: int = Field(default=0, ge=0, le=6)
    reserved_tokens: int = Field(default=0, ge=0, le=_MAX)
    reserved_seconds: int = Field(default=0, ge=0, le=_MAX)

    @property
    def checkpoints(self) -> list[RequiredAdherenceCheckpoint]:
        return [entry.checkpoint for entry in self.entries]

    @model_validator(mode="after")
    def require_charge_evidence(self) -> "_ReviewLedger":
        if (self.schema_version == "required_adherence_job_journal.v1") != (self.writer_contract_digest is None):
            raise ValueError("review_writer_contract_invalid")
        charged = [item for item in self.checkpoints if item.phase != "awaiting_review"]
        if (
            self.reserved_attempts != sum(item.review_capacity.max_attempts_per_review for item in charged)
            or self.reserved_tokens != sum(item.review_capacity.tokens_per_review for item in charged)
            or self.reserved_seconds != sum(item.review_capacity.seconds_per_review for item in charged)
            or len({item.receipt.view_digest for item in self.checkpoints}) != len(self.checkpoints)
            or len({required_review_ordinal(item.receipt) for item in self.checkpoints}) != len(self.checkpoints)
            or any(item.review_capacity != self.authorization.capacity for item in self.checkpoints)
        ):
            raise ValueError("review_journal_charge_invalid")
        for index, checkpoint in enumerate(self.checkpoints):
            ordinal = required_review_ordinal(checkpoint.receipt)
            if self.writer_contract_digest is None and ordinal != index + 1:
                raise ValueError("review_journal_sequence_invalid")
            if index == 0 and ordinal not in {0, 1, 2}:
                raise ValueError("review_journal_sequence_invalid")
            if index and isinstance(checkpoint.receipt, InitialAwaitingAdherenceReceipt):
                raise ValueError("review_journal_sequence_invalid")
            if index:
                previous = self.checkpoints[index - 1]
                previous_ordinal = required_review_ordinal(previous.receipt)
                if (
                    previous.phase != "review_settled"
                    or previous_ordinal >= ordinal
                    or previous.evidence.get("decision") != "repair"
                    or checkpoint.receipt.source_run_id != previous.receipt.source_run_id
                ):
                    raise ValueError("review_journal_sequence_invalid")
                # A partial rewrite is deliberately not reviewable.  Its next
                # complete revision may therefore skip one review ordinal.  The
                # required rewrite journal re-proves that missing link against
                # the exact incomplete receipt before this candidate can be
                # read or dispatched.  Adjacent reviews retain the stronger
                # direct source binding here.
                if ordinal == previous_ordinal + 1 and (
                    checkpoint.receipt.previous_revision
                    != previous.receipt.source_run_revision
                    or checkpoint.receipt.previous_content_digest
                    != previous.receipt.source_content_digest
                ):
                    raise ValueError("review_journal_sequence_invalid")
        return self


async def _validate_receipt_source(
    job: Mapping[str, Any],
    *,
    binding: RequiredReviewJobBinding,
    run: Mapping[str, Any],
    receipt: RequiredAdherenceReceipt,
) -> None:
    """Bind every review receipt to its exact durable writer checkpoint."""

    try:
        planning = job["readiness"]["planning"]
        from backend.services.generation.required_chapter_review_job import (
            required_chapter_review_planning_present,
            required_initial_authorization_from_planning,
            required_rewrite_authorization_from_planning,
        )

        if isinstance(receipt, InitialAwaitingAdherenceReceipt):
            # This receipt type was introduced by ADR-0008 and therefore has
            # no legacy lineage. A missing writer plan must never turn it into
            # a self-asserted complete initial draft.
            required_initial_authorization_from_planning(
                planning,
                chapter_id=binding.chapter_id,
            )
        else:
            try:
                required_rewrite_authorization_from_planning(planning)
                has_rewrite_authority = True
            except (KeyError, TypeError, ValueError):
                has_rewrite_authority = False
        if (
            not isinstance(receipt, InitialAwaitingAdherenceReceipt)
            and not has_rewrite_authority
        ):
            # The rewrite receipt predates ADR-0008. Preserve its original
            # live-candidate path for a purely legacy first prepare, or after
            # a v1 review ledger has already selected that protocol. Once a
            # v2 ledger or either writer journal exists, a missing rewrite
            # plan is protocol drift rather than legacy data.
            if run.get("required_initial_origin") is not None:
                raise ValueError("initial run cannot use a rewrite receipt")
            raw_review = job.get("required_adherence_journal")
            if raw_review is None:
                if (
                    "required_initial_prose" in planning
                    or required_chapter_review_planning_present(planning)
                    or job.get("required_initial_prose_journal") is not None
                    or job.get("required_prose_rewrite_journal") is not None
                ):
                    raise ValueError("rewrite writer plan missing")
                return
            if (
                _ReviewLedger.model_validate_json(_json(raw_review))
                .schema_version
                != "required_adherence_job_journal.v1"
                or job.get("required_initial_prose_journal") is not None
                or job.get("required_prose_rewrite_journal") is not None
            ):
                raise ValueError("rewrite writer plan missing")
            return
        remediation = run.get("remediation") or {}
        if isinstance(receipt, InitialAwaitingAdherenceReceipt):
            from backend.db.required_initial_prose_journal import (
                RequiredInitialProseJournal,
                validate_produced_initial_prose,
            )

            if (
                remediation.get("schema_version")
                == "prose_run_remediation.v2"
                or run.get("required_initial_origin") is None
            ):
                raise ValueError("initial receipt adopted a rewrite")
            initial = RequiredInitialProseJournal.model_validate_json(
                _json(job["required_initial_prose_journal"])
            )
            await validate_produced_initial_prose(
                job,
                binding=binding,
                run=run,
            )
            if (
                initial.phase != "produced"
                or initial.run_id != receipt.source_run_id
                or initial.run_revision != receipt.source_run_revision
                or initial.content_digest != receipt.source_content_digest
                or initial.origin.request_digest
                != receipt.initial_request_digest
                or initial.origin.contract_digest
                != receipt.initial_contract_digest
            ):
                raise ValueError("initial receipt lineage changed")
            return

        from backend.db.required_prose_rewrite_journal import (
            _checked_journal,
            validate_produced_rewrite,
        )

        if remediation.get("schema_version") != "prose_run_remediation.v2":
            raise ValueError("rewrite receipt adopted an initial draft")
        rewrites = _checked_journal(job, binding)
        await validate_produced_rewrite(
            job,
            binding=binding,
            run=run,
        )
        matches = [
            entry
            for entry in rewrites.entries
            if entry.origin.rewrite_ordinal == receipt.rewrite_ordinal
        ]
        if len(matches) != 1:
            raise ValueError("rewrite receipt ordinal changed")
        entry = matches[0]
        if (
            entry.phase != "produced"
            or entry.origin.request.source_run_id
            != receipt.source_run_id
            or entry.origin.request.source_revision
            != receipt.previous_revision
            or entry.origin.request.source_content_digest
            != receipt.previous_content_digest
            or entry.result_revision != receipt.source_run_revision
            or entry.result_digest != receipt.source_content_digest
            or entry.origin.review_contract_digest
            != receipt.review_contract_digest
        ):
            raise ValueError("rewrite receipt lineage changed")
    except (KeyError, IndexError, StopIteration, TypeError, ValueError, ValidationError):
        raise RequiredAdherenceHandoffError("review_handoff_stale") from None


class JobRequiredReviewJournal:
    """Created only by the Job repository; satisfies RequiredReviewJournal.

    read() re-proves the supplied frozen candidate against its owned ProseRun,
    current outline and narrative revision. Temporary Novel/ProseRun fences
    protect a Job CAS; the started checkpoint and dedicated charge share one
    Job update. They are not a replacement for physical paid-attempt claims.
    """

    def __init__(
        self, binding: RequiredReviewJobBinding, *, candidate: RequiredReviewCandidate,
        plan: IndependentReviewPlan, read_job: Callable[..., Awaitable[dict[str, Any]]],
        write_job: Callable[..., Awaitable[Any]],
    ):
        self.binding = RequiredReviewJobBinding.model_validate_json(binding.model_dump_json())
        self._candidate = candidate
        self._candidate_digest = candidate.check_digest
        self._plan = plan
        self._read_job = read_job
        self._write_job = write_job

    @property
    def attempt_step_id(self) -> str:
        """Exact ledger namespace; a prefix match must never join reviews."""
        return f"{REVIEW_STEP_PREFIX}{self._candidate.snapshot.view_digest}"

    async def _job(self) -> tuple[dict[str, Any], _ReviewAuthorization, _ReviewLedger]:
        binding = self.binding
        lease = current_job_execution()
        if lease is None or lease.job_id != binding.job_id:
            raise RequiredAdherenceHandoffError("review_job_lease_required")
        job = await self._read_job(binding.job_id)
        if not _job_matches_binding(job, binding):
            raise RequiredAdherenceHandoffError("review_job_binding_stale")
        try:
            readiness = job["readiness"]
            from backend.services.generation.required_chapter_review_job import (
                required_review_authorization_from_planning,
                required_rewrite_authorization_from_planning,
            )

            planning = readiness["planning"]
            authorization = _ReviewAuthorization.model_validate(
                required_review_authorization_from_planning(planning)
            )
            if (
                readiness["digest"] != binding.readiness_digest
                or binding.chapter_id not in authorization.chapter_ids
                or authorization.capacity != RequiredAdherenceCapacity.from_plan(self._plan)
                or authorization.provider_alias != self._plan.generation.provider_alias
            ):
                raise ValueError("mismatched review authorization")
            raw = job.get("required_adherence_journal")
            try:
                writer_digest = required_rewrite_authorization_from_planning(
                    planning
                ).get("contract_digest")
            except (KeyError, TypeError, ValueError):
                writer_digest = None
            ledger = _ReviewLedger(
                schema_version="required_adherence_job_journal.v2" if writer_digest else "required_adherence_job_journal.v1",
                writer_contract_digest=writer_digest, binding=binding, authorization=authorization,
            ) if raw is None else _ReviewLedger.model_validate_json(_json(raw))
            if ledger.binding != binding or ledger.authorization != authorization or ledger.writer_contract_digest != writer_digest:
                raise ValueError("mismatched review ledger")
        except (KeyError, TypeError, ValueError, ValidationError):
            raise RequiredAdherenceHandoffError("review_job_contract_invalid") from None
        return job, authorization, ledger

    async def _source(
        self,
        receipt: RequiredAdherenceReceipt,
    ) -> RequiredReviewCandidate:
        expected = self._candidate
        run, chapter = await _read_owned_complete_source(
            self.binding, run_id=expected.snapshot.source_run_id,
            run_revision=expected.snapshot.source_run_revision,
        )
        job = await self._read_job(self.binding.job_id)
        await _validate_receipt_source(
            job,
            binding=self.binding,
            run=run,
            receipt=receipt,
        )
        if (
            run.get("assembled_text") != expected.snapshot.prose
            or _json(chapter.get("outline")) != expected.snapshot.outline_json
            or run.get("outline_revision") != prose_revision(chapter["outline"])
            or _json(run.get("plan")) != _json(expected.prose_plan.to_dict())
        ):
            raise RequiredAdherenceHandoffError("review_handoff_stale")
        completion = dict(run.get("completion") or {})
        current = RequiredReviewCandidate.create(
            source_run_id=expected.snapshot.source_run_id,
            source_run_revision=int(run["revision"]), source_content_digest=expected.snapshot.source_content_digest,
            prose=run["assembled_text"], outline=chapter["outline"],
            authorized_context=expected.snapshot.authorized_context,
            prose_plan=expected.prose_plan,
            completion=completion,
        )
        if current.check_digest != self._candidate_digest:
            raise RequiredAdherenceHandoffError("review_handoff_stale")
        return current

    async def prepare(self, receipt: RequiredAdherenceReceipt) -> RequiredAdherenceCheckpoint:
        checkpoint = RequiredAdherenceHandoff.produced(receipt, snapshot=self._candidate.snapshot, plan=self._plan)
        if not await self._change(None, checkpoint):
            raise RequiredAdherenceHandoffError("review_checkpoint_conflict")
        return (await self.read())[0]

    async def read(self) -> tuple[RequiredAdherenceCheckpoint, RequiredReviewCandidate]:
        _, _, ledger = await self._job()
        entry = next(
            (
                item
                for item in ledger.entries
                if item.checkpoint.receipt.view_digest
                == self._candidate.snapshot.view_digest
            ),
            None,
        )
        if entry is None:
            raise RequiredAdherenceHandoffError("review_checkpoint_missing")
        current = await self._source(entry.checkpoint.receipt)
        if entry.candidate_digest != current.check_digest:
            raise RequiredAdherenceHandoffError("review_handoff_stale")
        return entry.checkpoint, current

    async def read_attempts(self) -> tuple[AttemptUsage, ...]:
        job, _, _ = await self._job()
        return self._attempts(job)

    def _attempts(self, job: Mapping[str, Any]) -> tuple[AttemptUsage, ...]:
        return _project_attempts(
            job, binding=self.binding, step_id=self.attempt_step_id,
            provider_alias=self._plan.generation.provider_alias,
            capacity=RequiredAdherenceCapacity.from_plan(self._plan),
        )

    async def compare_and_swap(
        self, expected: RequiredAdherenceCheckpoint, replacement: RequiredAdherenceCheckpoint,
        *, expected_candidate_digest: str,
    ) -> bool:
        if expected_candidate_digest != self._candidate_digest:
            return False
        return await self._change(expected, replacement)

    async def _change(
        self, expected: RequiredAdherenceCheckpoint | None, replacement: RequiredAdherenceCheckpoint,
    ) -> bool:
        replacement = RequiredAdherenceCheckpoint.model_validate_json(replacement.model_dump_json())
        job, authorization, ledger = await self._job()
        lease = current_job_execution()
        if lease is None:
            raise RequiredAdherenceHandoffError("review_job_lease_required")
        dispatch_owner = _ReviewDispatchOwner(epoch=lease.epoch, worker_id=lease.worker_id)
        candidate = await self._source(replacement.receipt)
        RequiredAdherenceHandoff.produced(replacement.receipt, snapshot=self._candidate.snapshot, plan=self._plan)
        if replacement.phase in {"review_settled", "review_blocked"}:
            RequiredAdherenceHandoff.validate_terminal(
                replacement, candidate=candidate, plan=self._plan, attempts=self._attempts(job),
            )
        entries = list(ledger.checkpoints)
        index = next((i for i, item in enumerate(entries) if item.receipt.view_digest == replacement.receipt.view_digest), None)
        if index is not None:
            await self._source(entries[index].receipt)
            if ledger.entries[index].candidate_digest != candidate.check_digest:
                raise RequiredAdherenceHandoffError("review_handoff_stale")
        started = False
        if expected is None:
            if replacement.phase != "awaiting_review":
                return False
            if index is not None:
                return entries[index].receipt == replacement.receipt
            entries.append(replacement)
        else:
            if index is None or entries[index] != expected or expected.receipt != replacement.receipt:
                return False
            if (
                expected.phase == "review_started" and replacement.phase in {"review_settled", "review_blocked"}
                and ledger.entries[index].dispatch_owner != dispatch_owner
            ):
                return False
            started = expected.phase == "awaiting_review" and replacement.phase == "review_started"
            if not (started or expected == replacement or (
                expected.phase == "review_started" and replacement.phase in {"review_settled", "review_blocked"}
            )):
                return False
            entries[index] = replacement
        capacity = authorization.capacity
        if started:
            reservation = job.get("attempt_reservation")
            if (
                not isinstance(reservation, Mapping)
                or reservation.get("chapter_id") != self.binding.chapter_id
                or not _integer(reservation.get("reserved_slots"))
                or not _integer(reservation.get("claimed_slots"))
                or reservation["reserved_slots"] - reservation["claimed_slots"] < capacity.max_attempts_per_review
            ):
                return False
            counts = tuple(job.get(key) for key in (
                "usage_attempt_capacity", "usage_attempt_claimed", "token_budget", "tokens_used", "tokens_reserved",
            ))
            if any(not _integer(value) for value in counts):
                return False
            attempts, claimed, tokens, used, reserved = counts
            if (
                attempts - claimed < capacity.max_attempts_per_review
                or tokens - used - reserved < capacity.tokens_per_review
                or authorization.deadline_at < get_utc_now() + timedelta(seconds=capacity.seconds_per_review)
            ):
                return False
            if self._attempts(job) or any(
                slot.get("state") not in {"accounted", "released_pre_dispatch"}
                for slot in job["attempt_slots"]
            ):
                return False
        if started and (
            job.get("has_uncertain_attempts") is not False
            or ledger.reserved_attempts + 2 > capacity.max_review_attempts_per_chapter
            or ledger.reserved_tokens + capacity.tokens_per_review > capacity.max_review_tokens_per_chapter
            or ledger.reserved_seconds + capacity.seconds_per_review > capacity.max_review_seconds_per_chapter
        ):
            return False
        charged = [item for item in entries if item.phase != "awaiting_review"]
        updated = _ReviewLedger(
            schema_version=ledger.schema_version, binding=ledger.binding, authorization=ledger.authorization,
            writer_contract_digest=ledger.writer_contract_digest,
            entries=[_ReviewEntry(
                checkpoint=item,
                candidate_digest=ledger.entries[i].candidate_digest if i < len(ledger.entries) else candidate.check_digest,
                outline_revision=ledger.entries[i].outline_revision if i < len(ledger.entries) else prose_revision(json.loads(candidate.snapshot.outline_json)),
                dispatch_owner=(dispatch_owner if started and i == index else
                                ledger.entries[i].dispatch_owner if i < len(ledger.entries) else None),
            ) for i, item in enumerate(entries)],
            reserved_attempts=sum(item.review_capacity.max_attempts_per_review for item in charged),
            reserved_tokens=sum(item.review_capacity.tokens_per_review for item in charged),
            reserved_seconds=sum(item.review_capacity.seconds_per_review for item in charged),
        )
        async with _source_fences(self.binding, replacement.receipt) as expires:
            await self._source(replacement.receipt)
            query = _job_snapshot_query(job)
            time_checks = [
                {"$gt": [{"$literal": min(expires, authorization.deadline_at)}, "$$NOW"]},
                {"$gt": ["$execution_lease.expires_at", "$$NOW"]},
            ]
            if started:
                time_checks.append({"$gte": [
                    {"$literal": authorization.deadline_at}, {"$add": ["$$NOW", capacity.seconds_per_review * 1000]},
                ]})
            query["$expr"] = {"$and": time_checks}
            result = await self._write_job(query, {"$set": {
                "required_adherence_journal": updated.model_dump(mode="json"), "updated_at": get_utc_now(),
            }})
            return result.matched_count == 1


def _project_attempts(
    job: Mapping[str, Any], *, binding: RequiredReviewJobBinding, step_id: str,
    provider_alias: str, capacity: RequiredAdherenceCapacity,
) -> tuple[AttemptUsage, ...]:
    slots, _, _ = _checked_bookkeeping(job)
    selected = [slot for slot in slots if slot.get("step_id") == step_id]
    attempts = []
    for slot in selected:
        attempt_id, state = slot.get("attempt_id"), slot.get("state")
        bound = slot.get("conservative_tokens")
        if (
            slot.get("chapter_id") != binding.chapter_id
            or slot.get("provider_alias") != provider_alias
            or state not in {"accounted", "claimed", "uncertain"}
            or not _integer(bound) or not 0 < bound <= capacity.input_tokens_per_attempt + capacity.output_tokens_per_attempt
        ):
            raise RequiredAdherenceHandoffError("review_accounting_invalid")
        if state == "accounted":
            raw = slot.get("usage")
            if (
                not isinstance(raw, dict) or set(raw) != {"input_tokens", "output_tokens", "total_tokens"}
                or any(not _integer(value) for value in raw.values())
                or raw["input_tokens"] + raw["output_tokens"] != raw["total_tokens"]
                or raw["input_tokens"] > capacity.input_tokens_per_attempt
                or raw["output_tokens"] > capacity.output_tokens_per_attempt
                or slot["charged_tokens"] != raw["total_tokens"]
                or not 0 < raw["total_tokens"] <= bound
            ):
                raise RequiredAdherenceHandoffError("review_accounting_invalid")
            usage = TokenUsage(**raw)
        else:
            # Unknown usage is never represented as a zero-cost settlement.
            # _checked_bookkeeping proves the original full reservation exists.
            if slot.get("usage") is not None:
                raise RequiredAdherenceHandoffError("review_accounting_invalid")
            usage = TokenUsage()
        attempts.append(AttemptUsage(attempt_id, slot["provider_alias"], slot.get("phase"), usage, state))
    if tuple(item.phase for item in attempts) not in {(), ("primary",), ("primary", "repair")}:
        raise RequiredAdherenceHandoffError("review_accounting_invalid")
    return tuple(attempts)


def _checked_bookkeeping(
    job: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Check conservation without converting an in-flight call to settled."""
    slots, active, usage_ids = (job.get(key) for key in ("attempt_slots", "active_token_reservations", "usage_attempt_ids"))
    counters = (job.get(key) for key in (
        "usage_attempt_capacity", "usage_attempt_claimed", "tokens_used", "tokens_reserved", "token_budget",
    ))
    if (
        any(not _integer(value) for value in counters)
        or not isinstance(slots, list) or any(not isinstance(slot, dict) for slot in slots)
        or not isinstance(active, list) or any(not isinstance(row, dict) for row in active)
        or not isinstance(usage_ids, list) or any(not isinstance(value, str) for value in usage_ids)
        or len(set(usage_ids)) != len(usage_ids)
        or job["usage_attempt_claimed"] != len(slots) or len(slots) > job["usage_attempt_capacity"]
    ):
        raise RequiredAdherenceHandoffError("review_accounting_invalid")
    identities = {}
    accounted_ids = set()
    expected_active = set()
    charged_floor = 0
    keys = ("attempt_id", "chapter_id", "step_id", "phase", "provider_alias")
    for slot in slots:
        if (
            any(not isinstance(slot.get(key), str) or not 1 <= len(slot[key]) <= 128 for key in keys)
            or slot["attempt_id"] in identities
            or slot.get("state") not in PERSISTED_ATTEMPT_STATES
            or not _integer(slot.get("conservative_tokens"))
            or not 0 < slot["conservative_tokens"] <= MAX_PERSISTED_ATTEMPT_TOKENS
        ):
            raise RequiredAdherenceHandoffError("review_accounting_invalid")
        identities[slot["attempt_id"]] = slot
        if slot["state"] == "accounted":
            charged = slot.get("charged_tokens")
            if not _integer(charged) or not 0 < charged <= MAX_PERSISTED_ATTEMPT_TOKENS:
                raise RequiredAdherenceHandoffError("review_accounting_invalid")
            charged_floor += charged
            accounted_ids.add(slot["attempt_id"])
        elif slot["state"] != "released_pre_dispatch":
            expected_active.add(slot["attempt_id"])
    seen = set()
    reserved_total = 0
    for row in active:
        attempt_id = row.get("attempt_id")
        if not isinstance(attempt_id, str) or attempt_id not in expected_active or attempt_id in seen:
            raise RequiredAdherenceHandoffError("review_accounting_invalid")
        slot = identities[attempt_id]
        if any(row.get(key) != slot.get(key) for key in (*keys, "state", "conservative_tokens")):
            raise RequiredAdherenceHandoffError("review_accounting_invalid")
        reserved_total += slot["conservative_tokens"]
        seen.add(attempt_id)
    if (
        seen != expected_active or set(usage_ids) != accounted_ids
        or job["tokens_used"] < charged_floor or job["tokens_reserved"] != reserved_total
        or job["tokens_used"] + job["tokens_reserved"] > job["token_budget"]
        or (any(slot["state"] == "uncertain" for slot in slots) and job.get("has_uncertain_attempts") is not True)
    ):
        raise RequiredAdherenceHandoffError("review_accounting_invalid")
    return slots, active, usage_ids


def _job_matches_binding(job: Mapping[str, Any], binding: RequiredReviewJobBinding) -> bool:
    return (
        str(job.get("_id")) == binding.job_id
        and str(job.get("owner_id")) == binding.owner_id
        and str(job.get("novel_id")) == binding.novel_id
        and job.get("is_deleted") is False and job.get("status") == "running"
        and job.get("current_chapter_id") == binding.chapter_id
        and _integer(job.get("authorization_revision"))
        and job["authorization_revision"] == binding.authorization_revision
        and _integer(job.get("expected_narrative_revision"))
        and job["expected_narrative_revision"] == binding.narrative_revision
    )


async def _read_owned_complete_source(
    binding: RequiredReviewJobBinding, *, run_id: str, run_revision: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Re-read shared source authority; callers prove their own snapshot/digest."""
    run = await prose_run_repo.get_run(run_id, binding.owner_id)
    chapter = await chapter_repo.get_chapter_by_id(binding.chapter_id)
    novel = await novel_repo.get_novel_by_id(binding.novel_id)
    if (
        str(run.get("novel_id")) != binding.novel_id
        or str(run.get("chapter_id")) != binding.chapter_id
        or str(run.get("generation_job_id")) != binding.job_id
        or str(chapter.get("novel_id")) != binding.novel_id
        or str(novel.get("owner_id")) != binding.owner_id
        or not _integer(run.get("revision")) or run["revision"] != run_revision
        or run.get("status") != "complete" or run.get("lease") is not None
        or run.get("has_uncertain_attempt") is not False
        or not _integer(run.get("narrative_revision")) or run["narrative_revision"] != binding.narrative_revision
        or not _integer(novel.get("narrative_revision")) or novel["narrative_revision"] != binding.narrative_revision
    ):
        raise RequiredAdherenceHandoffError("review_handoff_stale")
    return run, chapter


def _job_snapshot_query(job: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = {key: job.get(key) for key in (
        "_id", "owner_id", "novel_id", "status", "is_deleted", "current_chapter_id",
        "authorization_revision", "expected_narrative_revision", "readiness",
        "has_uncertain_attempts", "attempt_slots", "required_adherence_journal",
        "required_initial_prose_journal",
        "required_prose_rewrite_journal",
        "usage_attempt_capacity", "usage_attempt_claimed", "token_budget", "tokens_used", "tokens_reserved",
        "attempt_reservation", "active_token_reservations", "usage_attempt_ids",
    )}
    integer_fields = (
        "authorization_revision", "expected_narrative_revision", "usage_attempt_capacity",
        "usage_attempt_claimed", "token_budget", "tokens_used", "tokens_reserved",
        "attempt_reservation.reserved_slots", "attempt_reservation.claimed_slots",
    )
    return {"$and": [snapshot, {"$expr": {"$and": [
        {"$in": [{"$type": f"${field}"}, ["int", "long"]]} for field in integer_fields
    ]}}]}


@asynccontextmanager
async def _source_fences(
    binding: RequiredReviewJobBinding, receipt: RequiredAdherenceReceipt,
) -> AsyncIterator[datetime]:
    async with candidate_write_fences(
        binding, run_id=receipt.source_run_id, run_revision=receipt.source_run_revision,
    ) as expires:
        yield expires


@asynccontextmanager
async def candidate_write_fences(
    binding: RequiredReviewJobBinding, *, run_id: str, run_revision: int,
) -> AsyncIterator[datetime]:
    """Shared Novel/ProseRun fence; each caller re-proves its own protocol."""
    token = f"required-review:{uuid4().hex}"
    try:
        expires = await narrative_revision_store.acquire_write_fence(
            binding.novel_id, expected_revision=binding.narrative_revision,
            fence_token=token, resource_kind="prose_run", resource_id=run_id,
        )
        await prose_run_repo.acquire_remediation_write_fence(
            run_id=run_id, owner_id=binding.owner_id, novel_id=binding.novel_id,
            expected_revision=run_revision,
            expected_narrative_revision=binding.narrative_revision,
            fence_token=token, expires_at=expires,
        )
        yield expires
    finally:
        try:
            await prose_run_repo.release_remediation_write_fence(
                run_id=run_id, owner_id=binding.owner_id, novel_id=binding.novel_id, fence_token=token,
            )
        finally:
            await narrative_revision_store.release_write_fence(binding.novel_id, fence_token=token)


class RequiredReviewDispatchRejected(RequiredAdherenceHandoffError):
    provider_request_not_dispatched = True


async def write_required_review_claim(
    job: Mapping[str, Any], *, chapter_id: str, step_id: str, phase: str,
    provider_alias: str, conservative_tokens: int, query: Mapping[str, Any],
    update: Mapping[str, Any], write_job: Callable[..., Awaitable[Any]],
) -> Any:
    """Fence the existing physical claim; never allocate another review pool.

    Only the new namespace can use the opt-in journal. The repository keeps
    legacy claims out of these Jobs until the other successor phases have
    their own separately implemented authority; no legacy Job is upgraded.
    """
    try:
        ledger = _ReviewLedger.model_validate_json(_json(job["required_adherence_journal"]))
        from backend.services.generation.required_chapter_review_job import (
            required_review_authorization_from_planning,
        )

        authorization = _ReviewAuthorization.model_validate(
            required_review_authorization_from_planning(
                job["readiness"]["planning"]
            )
        )
        binding = ledger.binding
        lease = current_job_execution()
        if (
            lease is None or lease.job_id != binding.job_id
            or not _job_matches_binding(job, binding)
            or ledger.authorization != authorization
            or job["readiness"]["digest"] != binding.readiness_digest
            or chapter_id != binding.chapter_id or chapter_id not in authorization.chapter_ids
            or provider_alias != authorization.provider_alias
            or job.get("has_uncertain_attempts") is not False
            or not ledger.entries
        ):
            raise ValueError("invalid review authority")
        entry = ledger.entries[-1]
        receipt, capacity = entry.checkpoint.receipt, authorization.capacity
        if (
            entry.checkpoint.phase != "review_started"
            or entry.dispatch_owner != _ReviewDispatchOwner(epoch=lease.epoch, worker_id=lease.worker_id)
            or step_id != f"{REVIEW_STEP_PREFIX}{receipt.view_digest}"
            or type(conservative_tokens) is not int
            or not 0 < conservative_tokens <= capacity.input_tokens_per_attempt + capacity.output_tokens_per_attempt
        ):
            raise ValueError("invalid review claim")
        attempts = _project_attempts(
            job, binding=binding, step_id=step_id, provider_alias=provider_alias, capacity=capacity,
        )
        if (
            len(attempts) >= capacity.max_attempts_per_review
            or any(attempt.state != "accounted" for attempt in attempts)
            or phase != ("primary" if not attempts else "repair")
            or any(slot.get("state") in {"claimed", "uncertain"} for slot in job["attempt_slots"])
            or job.get("active_token_reservations") != []
        ):
            raise ValueError("review attempt cannot be repeated")
    except (KeyError, TypeError, ValueError):
        raise RequiredReviewDispatchRejected("review_dispatch_rejected") from None

    async with _source_fences(binding, receipt) as expires:
        try:
            run, chapter = await _read_owned_complete_source(
                binding, run_id=receipt.source_run_id, run_revision=receipt.source_run_revision,
            )
            await _validate_receipt_source(
                job,
                binding=binding,
                run=run,
                receipt=receipt,
            )
            if (
                not isinstance(run.get("assembled_text"), str)
                or hashlib.sha256(run["assembled_text"].encode("utf-8")).hexdigest() != receipt.source_content_digest
                or prose_revision(chapter.get("outline")) != entry.outline_revision
                or run.get("outline_revision") != entry.outline_revision
                or RequiredReviewCandidate.proof_digest(receipt.view_digest, run.get("plan"), run.get("completion")) != entry.candidate_digest
            ):
                raise ValueError("review source changed")
        except (KeyError, TypeError, ValueError):
            raise RequiredReviewDispatchRejected("review_dispatch_rejected") from None
        remaining_seconds = (capacity.max_attempts_per_review - len(attempts)) * capacity.timeout_seconds_per_attempt
        guarded = {"$and": [dict(query), _job_snapshot_query(job), {"$expr": {"$and": [
            {"$gt": [{"$literal": expires}, "$$NOW"]},
            {"$gt": ["$execution_lease.expires_at", "$$NOW"]},
            {"$gte": [{"$literal": authorization.deadline_at}, {"$add": ["$$NOW", remaining_seconds * 1000]}]},
        ]}}]}
        return await write_job(guarded, update)
