"""Lease-fenced Job journal for one ADR-0008 initial prose source."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal

from bson.int64 import Int64
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from backend.db.repositories.prose_run_repository import prose_run_repo
from backend.db.utils import get_utc_now
from backend.services.generation.job_execution import current_job_execution
from backend.services.generation.prose_completion import ProseExecutionPlan
from backend.services.generation.prose_generation import (
    planned_base_call_contracts,
)
from backend.services.generation.prose_runs import (
    prose_revision,
    prose_run_draft_text,
)
from backend.services.generation.required_adherence_handoff import (
    RequiredAdherenceHandoffError,
)
from backend.services.generation.required_initial_prose_contracts import (
    REQUIRED_INITIAL_PROSE_STEP_PREFIX,
    RequiredInitialProseAuthorization,
    RequiredInitialProseOrigin,
    stable_digest,
)


class RequiredInitialProseError(ValueError):
    """Closed initial-prose failure; source values are never included."""


class RequiredInitialProseDispatchRejected(RequiredInitialProseError):
    provider_request_not_dispatched = True


class _Closed(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class RequiredInitialProseJournal(_Closed):
    schema_version: Literal["required_initial_prose_journal.v1"]
    origin: RequiredInitialProseOrigin
    authorization: RequiredInitialProseAuthorization
    phase: Literal["reserved", "produced", "incomplete", "blocked"]
    worker_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_epoch: int = Field(ge=1, le=2**63 - 1)
    run_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{24}$")
    run_revision: int | None = Field(default=None, ge=2, le=2**63 - 1)
    content_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    attempt_ids: tuple[str, ...] = Field(default=(), max_length=100)
    failure_code: str | None = Field(default=None, min_length=1, max_length=80)
    can_write_formal_prose: Literal[False] = False
    can_generate_state: Literal[False] = False

    @model_validator(mode="after")
    def validate_shape(self) -> "RequiredInitialProseJournal":
        terminal_values = (
            self.run_id,
            self.run_revision,
            self.content_digest,
        )
        if self.authorization.contract_digest != self.origin.contract_digest:
            raise ValueError("initial_prose_contract_mismatch")
        if self.phase in {"produced", "incomplete"}:
            if any(value is None for value in terminal_values) or not self.attempt_ids:
                raise ValueError("initial_prose_result_incomplete")
            if self.failure_code is not None:
                raise ValueError("initial_prose_result_invalid")
        elif self.phase == "blocked":
            if any(value is not None for value in terminal_values) or self.failure_code is None:
                raise ValueError("initial_prose_failure_invalid")
        elif any(value is not None for value in terminal_values) or self.attempt_ids or self.failure_code is not None:
            raise ValueError("initial_prose_reserved_has_result")
        return self


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def _stored_integer(value: Any) -> bool:
    return type(value) in {int, Int64} and 0 <= value <= 2**63 - 1


def _validated_initial_attempts(
    job: Mapping[str, Any],
    journal: RequiredInitialProseJournal,
    *,
    require_recorded_identity: bool,
    run: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Re-prove the exact paid calls owned by one frozen initial request."""

    from backend.db.required_adherence_journal import (
        _checked_bookkeeping,
        _integer,
    )

    try:
        slots, active, _ = _checked_bookkeeping(job)
    except ValueError:
        raise RequiredInitialProseError(
            "initial_prose_accounting_invalid"
        ) from None
    selected = tuple(
        slot for slot in slots
        if slot.get("step_id")
        == f"{REQUIRED_INITIAL_PROSE_STEP_PREFIX}{journal.origin.request_digest}"
    )
    if not 1 <= len(selected) <= journal.authorization.max_calls:
        raise RequiredInitialProseError(
            "initial_prose_accounting_calls_missing"
            if not selected
            else "initial_prose_accounting_calls_exceeded"
        )
    if require_recorded_identity and tuple(
        slot.get("attempt_id") for slot in selected
    ) != journal.attempt_ids:
        raise RequiredInitialProseError(
            "initial_prose_accounting_identity_invalid"
        )
    maximum_tokens = (
        journal.authorization.input_tokens_per_call
        + journal.authorization.output_tokens_per_call
    )
    for slot in selected:
        usage = slot.get("usage")
        if (
            slot.get("chapter_id") != journal.origin.chapter_id
            or slot.get("provider_alias")
            != journal.authorization.provider_alias
            or slot.get("phase") != "text"
            or slot.get("state") != "accounted"
            or not _integer(slot.get("conservative_tokens"))
            or not 0 < slot["conservative_tokens"] <= maximum_tokens
            or not isinstance(usage, dict)
            or set(usage) != {
                "input_tokens",
                "output_tokens",
                "total_tokens",
            }
            or any(not _integer(value) for value in usage.values())
            or usage["input_tokens"] + usage["output_tokens"]
            != usage["total_tokens"]
            or usage["input_tokens"]
            > journal.authorization.input_tokens_per_call
            or usage["output_tokens"]
            > journal.authorization.output_tokens_per_call
            or slot.get("charged_tokens") != usage["total_tokens"]
            or not 0 < usage["total_tokens"] <= slot["conservative_tokens"]
        ):
            raise RequiredInitialProseError(
                "initial_prose_accounting_identity_invalid"
            )
    if (
        active
        or job.get("has_uncertain_attempts") is not False
        or any(
            slot.get("state")
            not in {"accounted", "released_pre_dispatch"}
            for slot in slots
        )
    ):
        raise RequiredInitialProseError(
            "initial_prose_accounting_unsettled"
        )
    if run is not None:
        raw_segments = run.get("segments")
        segment_header_checks = {
            "shape": isinstance(raw_segments, list) and bool(raw_segments),
            "count": isinstance(raw_segments, list)
            and len(raw_segments) == len(selected),
            "bound": isinstance(raw_segments, list)
            and len(raw_segments) <= journal.authorization.max_calls,
            "text": run.get("status") != "complete"
            or (
                isinstance(raw_segments, list)
                and prose_run_draft_text({
                    **dict(run),
                    "assembled_text": "",
                })
                == run.get("assembled_text")
            ),
        }
        failed_header = next(
            (
                name
                for name, valid in segment_header_checks.items()
                if not valid
            ),
            None,
        )
        if failed_header is not None:
            raise RequiredInitialProseError(
                f"initial_prose_segment_{failed_header}_invalid"
            )
        try:
            raw_plan = run["plan"]
            execution_plan = ProseExecutionPlan(
                requested_word_count=raw_plan["requested_word_count"],
                scene_count=raw_plan["scene_count"],
                mode=raw_plan["mode"],
                provider_output_limit=raw_plan["provider_output_limit"],
                safe_output_budget=raw_plan["safe_output_budget"],
                minimum_completion_ratio=raw_plan[
                    "minimum_completion_ratio"
                ],
                segment_budgets=tuple(raw_plan["segment_budgets"]),
                segment_minimums=tuple(raw_plan["segment_minimums"]),
                segment_maximums=tuple(raw_plan["segment_maximums"]),
                reason_codes=tuple(raw_plan["reason_codes"]),
                protocol_revision=raw_plan["protocol_revision"],
            )
            expected_contracts = planned_base_call_contracts(
                execution_plan
            )
            if (
                len(expected_contracts)
                != journal.authorization.max_calls
            ):
                raise ValueError("initial plan call count changed")
        except (KeyError, TypeError, ValueError):
            raise RequiredInitialProseError(
                "initial_prose_segment_plan_invalid"
            ) from None
        expected_by_sequence = {
            item["sequence_index"]: item
            for item in expected_contracts
        }
        sequences: set[int] = set()
        ordered_sequences: list[int] = []
        actual_by_scene: dict[int, list[int]] = {}
        for index, (segment, slot) in enumerate(
            zip(raw_segments, selected, strict=True)
        ):
            if not isinstance(segment, dict):
                raise RequiredInitialProseError(
                    "initial_prose_accounting_identity_invalid"
                )
            sequence = segment.get("sequence_index")
            text = segment.get("text")
            status = segment.get("status")
            expected = (
                expected_by_sequence.get(sequence)
                if type(sequence) is int
                else None
            )
            scene_index = segment.get("scene_index")
            scene_key = scene_index if type(scene_index) is int else -1
            scene_calls = actual_by_scene.setdefault(
                scene_key,
                [],
            )
            segment_checks = {
                "sequence_type": type(sequence) is int,
                "sequence_bound": type(sequence) is int
                and sequence in expected_by_sequence,
                "sequence_unique": sequence not in sequences,
                "sequence_order": not ordered_sequences
                or sequence > ordered_sequences[-1],
                "call_kind": segment.get("call_kind") == "base",
                "prompt_mode": segment.get("prompt_mode") == "base",
                "scene": expected is not None
                and scene_index == expected["scene_index"],
                "part": expected is not None
                and segment.get("part_index") == expected["part_index"]
                and segment.get("part_count") == expected["part_count"],
                "target": expected is not None
                and segment.get("target_word_count")
                == expected["target_word_count"],
                "scene_call": segment.get("scene_call_index")
                == len(scene_calls),
                "text": isinstance(text, str),
                "digest": isinstance(text, str)
                and segment.get("text_digest")
                == hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "usage": segment.get("usage") == slot.get("usage"),
                "status": status == "completed"
                if run.get("status") == "complete"
                else status in {"completed", "incomplete"},
            }
            failed_segment = next(
                (
                    name
                    for name, valid in segment_checks.items()
                    if not valid
                ),
                None,
            )
            if failed_segment is not None:
                raise RequiredInitialProseError(
                    f"initial_prose_segment_{failed_segment}_invalid"
                )
            sequences.add(sequence)
            ordered_sequences.append(sequence)
            scene_calls.append(sequence)
        if execution_plan.mode == "scene_segments":
            for scene_index in range(execution_plan.scene_count):
                planned = [
                    item["sequence_index"]
                    for item in expected_contracts
                    if item["scene_index"] == scene_index
                ]
                actual = actual_by_scene.get(scene_index, [])
                if actual != planned[:len(actual)] or (
                    run.get("status") == "complete" and not actual
                ):
                    raise RequiredInitialProseError(
                        "initial_prose_segment_scene_prefix_invalid"
                    )
    return selected


class JobRequiredInitialProseJournal:
    """Own one initial request before its first Provider dispatch."""

    def __init__(
        self,
        binding,
        *,
        origin: RequiredInitialProseOrigin,
        authorization: RequiredInitialProseAuthorization,
        read_job: Callable[..., Awaitable[dict[str, Any]]],
        write_job: Callable[..., Awaitable[Any]],
    ):
        self.binding = binding
        self.origin = RequiredInitialProseOrigin.model_validate_json(
            origin.model_dump_json()
        )
        self.authorization = RequiredInitialProseAuthorization.model_validate_json(
            authorization.model_dump_json()
        )
        self._read_job = read_job
        self._write_job = write_job

    @property
    def attempt_step_id(self) -> str:
        return f"{REQUIRED_INITIAL_PROSE_STEP_PREFIX}{self.origin.request_digest}"

    async def _job(self) -> tuple[dict[str, Any], RequiredInitialProseJournal | None]:
        from backend.db.required_adherence_journal import _job_matches_binding

        lease = current_job_execution()
        job = await self._read_job(self.binding.job_id)
        try:
            from backend.services.generation.required_chapter_review_job import (
                required_initial_authorization_from_planning,
            )

            stored_authorization = required_initial_authorization_from_planning(
                job["readiness"]["planning"],
                chapter_id=self.binding.chapter_id,
            )
            raw = job.get("required_initial_prose_journal")
            journal = (
                None
                if raw is None
                else RequiredInitialProseJournal.model_validate_json(_json(raw))
            )
            if (
                lease is None
                or lease.job_id != self.binding.job_id
                or not _job_matches_binding(job, self.binding)
                or job["readiness"]["digest"] != self.binding.readiness_digest
                or stored_authorization != self.authorization
                or self.origin.job_id != self.binding.job_id
                or self.origin.readiness_digest != self.binding.readiness_digest
                or self.origin.authorization_revision
                != self.binding.authorization_revision
                or self.origin.narrative_revision != self.binding.narrative_revision
                or self.origin.chapter_id != self.binding.chapter_id
                or self.origin.contract_digest != self.authorization.contract_digest
                or self.origin.generation_plan_digest
                != self.authorization.generation_plan_digest
                or self.origin.execution_plan_digest
                != self.authorization.execution_plan_digest
                or (
                    journal is not None
                    and (
                        journal.origin != self.origin
                        or journal.authorization != self.authorization
                    )
                )
            ):
                raise ValueError("initial authority changed")
        except (KeyError, TypeError, ValueError, ValidationError):
            raise RequiredInitialProseError("initial_prose_job_contract_invalid") from None
        return job, journal

    async def begin(self) -> RequiredInitialProseJournal:
        from backend.db.required_adherence_journal import _job_snapshot_query

        job, existing = await self._job()
        if existing is not None:
            if existing.phase == "reserved":
                return await self._adopt_reserved(job, existing)
            return existing
        lease = current_job_execution()
        if lease is None:
            raise RequiredInitialProseError("initial_prose_job_lease_required")
        current = await prose_run_repo.find_active(
            chapter_id=self.binding.chapter_id,
            owner_id=self.binding.owner_id,
        )
        if current is not None:
            raw_origin = current.get("required_initial_origin")
            if raw_origin is None or RequiredInitialProseOrigin.model_validate(
                raw_origin
            ) != self.origin:
                raise RequiredInitialProseError("initial_prose_source_conflict")
        journal = RequiredInitialProseJournal(
            schema_version="required_initial_prose_journal.v1",
            origin=self.origin,
            authorization=self.authorization,
            phase="reserved",
            worker_id=lease.worker_id,
            execution_epoch=lease.epoch,
        )
        result = await self._write_job(
            _job_snapshot_query(job),
            {"$set": {
                "required_initial_prose_journal": journal.model_dump(mode="json"),
                "updated_at": get_utc_now(),
            }},
        )
        if result.matched_count != 1:
            _, recovered = await self._job()
            if recovered is None:
                raise RequiredInitialProseError("initial_prose_checkpoint_conflict")
            return recovered
        return journal

    async def _adopt_reserved(
        self,
        job: Mapping[str, Any],
        journal: RequiredInitialProseJournal,
    ) -> RequiredInitialProseJournal:
        """Move an undispatched reservation to the current durable Job lease.

        Only execution ownership changes.  The frozen source, authorization,
        accounting, and any settled physical attempt identities remain intact.
        An in-flight or uncertain request is never made retryable by takeover.
        """

        from backend.db.required_adherence_journal import (
            _checked_bookkeeping,
            _job_snapshot_query,
        )
        from backend.services.generation.job_execution import (
            JobExecutionLeaseV1,
        )

        lease = current_job_execution()
        if journal.phase != "reserved":
            raise RequiredInitialProseError(
                "initial_prose_execution_owner_invalid"
            )
        if lease is None:
            raise RequiredInitialProseError(
                "initial_prose_job_lease_required"
            )
        if (journal.worker_id, journal.execution_epoch) == (
            lease.worker_id,
            lease.epoch,
        ):
            return journal
        try:
            stored_lease = JobExecutionLeaseV1.model_validate(
                job["execution_lease"]
            )
            if (
                not _stored_integer(job.get("execution_epoch"))
                or job["execution_epoch"] != lease.epoch
                or stored_lease.job_id != lease.job_id
                or stored_lease.worker_id != lease.worker_id
                or stored_lease.epoch != lease.epoch
                or stored_lease.expires_at <= get_utc_now()
                or lease.epoch <= journal.execution_epoch
            ):
                raise ValueError("stale execution owner")
            slots, active, _ = _checked_bookkeeping(job)
            if (
                active
                or job.get("has_uncertain_attempts") is not False
                or any(
                    slot.get("state")
                    not in {"accounted", "released_pre_dispatch"}
                    for slot in slots
                )
            ):
                raise RequiredInitialProseError(
                    "initial_prose_accounting_unsettled"
                )
        except RequiredInitialProseError:
            raise
        except (KeyError, TypeError, ValueError, ValidationError):
            raise RequiredInitialProseError(
                "initial_prose_execution_owner_invalid"
            ) from None

        adopted = journal.model_copy(update={
            "worker_id": lease.worker_id,
            "execution_epoch": lease.epoch,
        })
        result = await self._write_job(
            _job_snapshot_query(job),
            {"$set": {
                "required_initial_prose_journal": adopted.model_dump(
                    mode="json"
                ),
                "updated_at": get_utc_now(),
            }},
        )
        if result.matched_count == 1:
            return adopted
        _, recovered = await self._job()
        if recovered == adopted:
            return recovered
        raise RequiredInitialProseError(
            "initial_prose_checkpoint_conflict"
        )

    async def read(self) -> RequiredInitialProseJournal:
        _, journal = await self._job()
        if journal is None:
            raise RequiredInitialProseError("initial_prose_checkpoint_missing")
        return journal

    async def produced(self, run: Mapping[str, Any]) -> RequiredInitialProseJournal:
        return await self._settle(run, phase="produced")

    async def incomplete(
        self,
        run: Mapping[str, Any],
    ) -> RequiredInitialProseJournal:
        return await self._settle(run, phase="incomplete")

    async def _settle(
        self,
        run: Mapping[str, Any],
        *,
        phase: Literal["produced", "incomplete"],
    ) -> RequiredInitialProseJournal:
        from backend.db.required_adherence_journal import (
            _job_snapshot_query,
            candidate_write_fences,
        )

        job, current = await self._job()
        if current is None:
            raise RequiredInitialProseError("initial_prose_checkpoint_missing")
        if current.phase == phase:
            await _validate_initial_run(
                job,
                binding=self.binding,
                journal=current,
                run=run,
                require_journal_result=True,
                expected_status=(
                    "complete" if phase == "produced" else "incomplete"
                ),
            )
            return current
        if current.phase != "reserved":
            raise RequiredInitialProseError("initial_prose_checkpoint_blocked")
        await _validate_initial_run(
            job,
            binding=self.binding,
            journal=current,
            run=run,
            require_journal_result=False,
            expected_status=(
                "complete" if phase == "produced" else "incomplete"
            ),
        )
        selected = _validated_initial_attempts(
            job,
            current,
            require_recorded_identity=False,
            run=run,
        )
        text = run.get("assembled_text")
        if not isinstance(text, str) or not text:
            raise RequiredInitialProseError("initial_prose_result_invalid")
        replacement = current.model_copy(update={
            "phase": phase,
            "run_id": str(run["_id"]),
            "run_revision": int(run["revision"]),
            "content_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "attempt_ids": tuple(slot["attempt_id"] for slot in selected),
        })
        _validated_initial_attempts(
            job,
            replacement,
            require_recorded_identity=True,
            run=run,
        )
        async with candidate_write_fences(
            self.binding,
            run_id=str(run["_id"]),
            run_revision=int(run["revision"]),
        ):
            guarded_job, guarded_current = await self._job()
            if guarded_current != current:
                raise RequiredInitialProseError("initial_prose_checkpoint_conflict")
            await _validate_initial_run(
                guarded_job,
                binding=self.binding,
                journal=current,
                run=await prose_run_repo.get_run(
                    str(run["_id"]),
                    self.binding.owner_id,
                ),
                require_journal_result=False,
                expected_status=(
                    "complete" if phase == "produced" else "incomplete"
                ),
            )
            result = await self._write_job(
                _job_snapshot_query(guarded_job),
                {"$set": {
                    "required_initial_prose_journal": replacement.model_dump(mode="json"),
                    "updated_at": get_utc_now(),
                }},
            )
        if result.matched_count != 1:
            raise RequiredInitialProseError("initial_prose_checkpoint_conflict")
        return replacement


async def _validate_initial_run(
    job: Mapping[str, Any],
    *,
    binding,
    journal: RequiredInitialProseJournal,
    run: Mapping[str, Any],
    require_journal_result: bool,
    expected_status: Literal["complete", "incomplete"],
) -> None:
    if require_journal_result:
        _validated_initial_attempts(
            job,
            journal,
            require_recorded_identity=True,
            run=run,
        )
    origin = RequiredInitialProseOrigin.model_validate(
        run.get("required_initial_origin")
    )
    completion = run.get("completion")
    text = run.get("assembled_text")
    execution_plan = run.get("plan")
    provider_plan = run.get("provider_plan")
    digest = (
        hashlib.sha256(text.encode("utf-8")).hexdigest()
        if isinstance(text, str)
        else ""
    )
    if (
        origin != journal.origin
        or (
            require_journal_result
            and str(run.get("_id")) != journal.run_id
        )
        or str(run.get("owner_id")) != binding.owner_id
        or str(run.get("novel_id")) != binding.novel_id
        or str(run.get("chapter_id")) != binding.chapter_id
        or str(run.get("generation_job_id")) != binding.job_id
        or run.get("status") != expected_status
        or run.get("lease") is not None
        or run.get("has_uncertain_attempt") is not False
        or run.get("outline_revision") != origin.outline_revision
        or not isinstance(execution_plan, Mapping)
        or stable_digest(dict(execution_plan))
        != journal.authorization.execution_plan_digest
        or not isinstance(provider_plan, Mapping)
        or provider_plan.get("provider_alias")
        != journal.authorization.provider_alias
        or provider_plan.get("provider_model")
        != journal.authorization.provider_model
        or not _stored_integer(run.get("narrative_revision"))
        or run.get("narrative_revision") != binding.narrative_revision
        or not isinstance(completion, Mapping)
        or completion.get("status") != expected_status
        or (
            expected_status == "complete"
            and completion.get("finish_reason") != "stop"
        )
        or completion.get("can_write_formal_prose") is not False
        or digest == ""
        or (
            require_journal_result
            and (
                not _stored_integer(run.get("revision"))
                or run.get("revision") != journal.run_revision
                or digest != journal.content_digest
            )
        )
    ):
        raise RequiredInitialProseError("initial_prose_result_stale")


async def validate_produced_initial_prose(job, *, binding, run) -> None:
    try:
        journal = RequiredInitialProseJournal.model_validate_json(
            _json(job["required_initial_prose_journal"])
        )
        if journal.phase != "produced":
            raise ValueError("not produced")
        await _validate_initial_run(
            job,
            binding=binding,
            journal=journal,
            run=run,
            require_journal_result=True,
            expected_status="complete",
        )
    except (KeyError, TypeError, ValueError, ValidationError):
        raise RequiredAdherenceHandoffError("review_handoff_stale") from None


async def validate_incomplete_initial_prose(job, *, binding, run) -> None:
    try:
        journal = RequiredInitialProseJournal.model_validate_json(
            _json(job["required_initial_prose_journal"])
        )
        if journal.phase != "incomplete":
            raise ValueError("not incomplete")
        await _validate_initial_run(
            job,
            binding=binding,
            journal=journal,
            run=run,
            require_journal_result=True,
            expected_status="incomplete",
        )
    except (KeyError, TypeError, ValueError, ValidationError):
        raise RequiredInitialProseError(
            "initial_prose_result_stale"
        ) from None


async def write_required_initial_prose_claim(
    job: Mapping[str, Any],
    *,
    chapter_id: str,
    step_id: str,
    phase: str,
    provider_alias: str,
    conservative_tokens: int,
    query: Mapping[str, Any],
    update: Mapping[str, Any],
    write_job: Callable[..., Awaitable[Any]],
) -> Any:
    """Validate one physical base-scene claim against the original journal."""

    from backend.db.required_adherence_journal import (
        _checked_bookkeeping,
        _job_matches_binding,
        _job_snapshot_query,
        RequiredReviewJobBinding,
    )

    try:
        journal = RequiredInitialProseJournal.model_validate_json(
            _json(job["required_initial_prose_journal"])
        )
        origin, authorization = journal.origin, journal.authorization
        binding = RequiredReviewJobBinding(
            job_id=origin.job_id,
            owner_id=str(job["owner_id"]),
            novel_id=str(job["novel_id"]),
            chapter_id=origin.chapter_id,
            readiness_digest=origin.readiness_digest,
            authorization_revision=origin.authorization_revision,
            narrative_revision=origin.narrative_revision,
        )
        lease = current_job_execution()
        slots, active, _ = _checked_bookkeeping(job)
        selected = [
            slot for slot in slots
            if slot.get("step_id") == step_id
        ]
        checks = {
            "phase": journal.phase == "reserved",
            "lease": lease is not None and lease.job_id == binding.job_id,
            "binding": _job_matches_binding(job, binding),
            "worker": lease is not None and journal.worker_id == lease.worker_id
            and journal.execution_epoch == lease.epoch,
            "chapter": chapter_id == binding.chapter_id,
            "step": step_id
            == f"{REQUIRED_INITIAL_PROSE_STEP_PREFIX}{origin.request_digest}",
            "attempt_phase": phase == "text",
            "provider": provider_alias == authorization.provider_alias,
            "token_bound": type(conservative_tokens) is int
            and 0 < conservative_tokens <= (
                authorization.input_tokens_per_call
                + authorization.output_tokens_per_call
            ),
            "call_count": len(selected) < authorization.max_calls,
            "settlement": not any(
                slot.get("state") != "accounted" for slot in selected
            ),
            "no_active_attempt": not active,
            "certainty": job.get("has_uncertain_attempts") is False,
        }
        failed = next((name for name, valid in checks.items() if not valid), None)
        if failed is not None:
            raise ValueError(f"initial_claim_{failed}_invalid")
        run = await prose_run_repo.find_required_initial(
            owner_id=binding.owner_id,
            chapter_id=binding.chapter_id,
            job_id=binding.job_id,
            request_digest=origin.request_digest,
        )
        if (
            run is None
            or RequiredInitialProseOrigin.model_validate(
                run.get("required_initial_origin")
            ) != origin
            or run.get("status") not in {"active", "incomplete"}
            or run.get("lease") is None
        ):
            raise ValueError("initial source unavailable")
    except KeyError:
        code = "initial_claim_field_missing"
    except TypeError:
        code = "initial_claim_type_invalid"
    except ValidationError:
        code = "initial_claim_contract_invalid"
    except ValueError as exc:
        code = (
            str(exc)
            if str(exc).startswith("initial_claim_")
            else "initial_claim_source_unavailable"
            if str(exc) == "initial source unavailable"
            else "initial_claim_value_invalid"
        )
    if 'code' in locals():
        raise RequiredInitialProseDispatchRejected(
            code
        ) from None
    guarded = {"$and": [dict(query), _job_snapshot_query(job)]}
    return await write_job(guarded, update)
