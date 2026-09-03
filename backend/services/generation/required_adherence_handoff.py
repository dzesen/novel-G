"""Versioned repaired-candidate handoff, not a completion or write authority.

ADR-0008 keeps this opt-in contract separate from the old, already-verified
remediation receipt. The candidate pipeline owns persistence and publication;
the handoff never stores another copy of the prose or unlocks formal content.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.services.generation.independent_outline_review import (
    IndependentOutlineReviewer,
    IndependentReviewFailureCode,
    IndependentReviewPlan,
    OutlineReviewSnapshot,
)
from backend.services.generation.outline_adherence import (
    OutlineAdherenceValidationError,
    revalidate_current_outline_adherence_evidence,
)
from backend.services.generation.prose_completion import (
    ProseExecutionPlan,
    prose_completion_module,
)
from backend.services.generation.prose_scene_repair import (
    SceneContractValidationProof,
    validate_v2_scene_contract_proof,
)
from backend.services.generation.required_adherence_capacity import RequiredAdherenceCapacity
from backend.services.llm.generation_runtime import AttemptUsage


class RequiredAdherenceHandoffError(ValueError):
    """A closed, local handoff failure; messages never contain source values."""


class _HandoffContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class AwaitingAdherenceReceipt(_HandoffContract):
    schema_version: Literal["prose_candidate_awaiting_adherence.v1"]
    source_run_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    source_run_revision: int = Field(ge=1, le=2**63 - 1)
    source_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_revision: int = Field(ge=1, le=2**63 - 1)
    previous_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    rewrite_ordinal: int = Field(ge=1, le=2)
    review_contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    view_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def require_new_revision(self) -> "AwaitingAdherenceReceipt":
        if (
            self.source_run_revision <= self.previous_revision
            or self.source_content_digest == self.previous_content_digest
        ):
            raise ValueError("repair_no_progress")
        return self


class InitialAwaitingAdherenceReceipt(_HandoffContract):
    """A complete initial draft, distinct from every repaired revision."""

    schema_version: Literal["initial_prose_candidate_awaiting_adherence.v1"]
    source_run_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    source_run_revision: int = Field(ge=2, le=2**63 - 1)
    source_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    initial_request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    initial_contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    view_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


RequiredAdherenceReceipt = (
    InitialAwaitingAdherenceReceipt | AwaitingAdherenceReceipt
)


def required_review_ordinal(receipt: RequiredAdherenceReceipt) -> int:
    """Initial review is ordinal zero; real rewrites retain ordinals one/two."""

    return 0 if isinstance(receipt, InitialAwaitingAdherenceReceipt) else receipt.rewrite_ordinal


class RequiredAdherenceCheckpoint(_HandoffContract):
    schema_version: Literal["required_adherence_checkpoint.v1"]
    receipt: RequiredAdherenceReceipt
    review_capacity: RequiredAdherenceCapacity
    phase: Literal["awaiting_review", "review_started", "review_settled", "review_blocked"]
    evidence: dict[str, Any] | None = None
    failure_code: IndependentReviewFailureCode | None = None
    attempt_ids: tuple[str, ...] = Field(default=(), max_length=2)
    ledger_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    can_write_formal_prose: Literal[False] = False
    can_generate_state: Literal[False] = False

    @model_validator(mode="after")
    def validate_terminal_shape(self) -> "RequiredAdherenceCheckpoint":
        if self.review_capacity.review_contract_digest != self.receipt.review_contract_digest:
            raise ValueError("review_capacity_plan_mismatch")
        if self.phase == "review_settled":
            if (
                self.evidence is None or self.failure_code is not None
                or not self.attempt_ids or self.ledger_digest is None
            ):
                raise ValueError("review_checkpoint_evidence_missing")
        elif self.phase == "review_blocked":
            if self.evidence is not None or self.failure_code is None or self.ledger_digest is None:
                raise ValueError("review_checkpoint_failure_invalid")
        elif (
            self.evidence is not None or self.failure_code is not None
            or self.attempt_ids or self.ledger_digest is not None
        ):
            raise ValueError("review_checkpoint_unfinished_has_result")
        return self


@dataclass(frozen=True)
class RequiredReviewCandidate:
    """Live source, never serialized into the metadata checkpoint."""

    snapshot: OutlineReviewSnapshot
    prose_plan: ProseExecutionPlan
    completion: Mapping[str, Any]

    @classmethod
    def create(
        cls,
        *,
        source_run_id: str,
        source_run_revision: int,
        source_content_digest: str,
        prose: str,
        outline: Mapping[str, Any],
        authorized_context: str,
        prose_plan: ProseExecutionPlan,
        completion: Mapping[str, Any],
    ) -> "RequiredReviewCandidate":
        """Build one review candidate from its deterministic completion proof."""

        try:
            proof = validate_v2_scene_contract_proof(
                text=prose,
                outline=outline,
                plan=prose_plan,
                completion=completion,
            )
            if proof is None:
                raise ValueError("review_requires_v2_proof")
            snapshot = OutlineReviewSnapshot.create(
                source_run_id=source_run_id,
                source_run_revision=source_run_revision,
                source_content_digest=source_content_digest,
                prose=prose,
                outline=outline,
                authorized_context=authorized_context,
                scene_ranges=tuple({
                    "scene_id": scene.scene_id,
                    "start": scene.start,
                    "end": scene.end,
                } for scene in proof.scenes),
            )
            candidate = cls(snapshot, prose_plan, dict(completion))
            candidate._require_complete_with_proof(
                outline=json.loads(snapshot.outline_json),
                proof=proof,
            )
            return candidate
        except RequiredAdherenceHandoffError:
            raise
        except (ValueError, TypeError, KeyError):
            raise RequiredAdherenceHandoffError(
                "review_candidate_incomplete"
            ) from None

    @property
    def check_digest(self) -> str:
        """Bind the exact live completion proof inspected before an await."""
        return self.proof_digest(self.snapshot.view_digest, self.prose_plan.to_dict(), self.completion)

    @staticmethod
    def proof_digest(view_digest: str, prose_plan: Mapping[str, Any], completion: Mapping[str, Any]) -> str:
        """The storage adapter can recheck a proof without storing the context."""
        try:
            value = json.dumps({
                "view_digest": view_digest,
                "prose_plan": dict(prose_plan),
                "completion": dict(completion),
            }, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (ValueError, TypeError):
            raise RequiredAdherenceHandoffError("review_candidate_incomplete") from None
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def require_complete(self) -> None:
        outline = json.loads(self.snapshot.outline_json)
        try:
            proof = validate_v2_scene_contract_proof(
                text=self.snapshot.prose, outline=outline,
                plan=self.prose_plan, completion=self.completion,
            )
            if proof is None:
                raise ValueError("review_requires_v2_proof")
        except (ValueError, TypeError, KeyError):
            raise RequiredAdherenceHandoffError("review_candidate_incomplete") from None
        self._require_complete_with_proof(outline=outline, proof=proof)

    def _require_complete_with_proof(
        self,
        *,
        outline: Mapping[str, Any],
        proof: SceneContractValidationProof,
    ) -> None:
        if (
            self.completion.get("status") != "complete"
            or self.completion.get("finish_reason") != "stop"
            or self.completion.get("can_write_formal_prose") is not False
            or self.completion.get("source_run_id") != self.snapshot.source_run_id
            or self.completion.get("source_run_revision") != self.snapshot.source_run_revision
            or self.completion.get("source_content_digest") != self.snapshot.source_content_digest
            or self.prose_plan.requested_word_count != outline.get("target_word_count")
            or self.prose_plan.minimum_completion_ratio != 0.8
        ):
            raise RequiredAdherenceHandoffError("review_candidate_incomplete")
        try:
            proof_ranges = tuple(
                (scene.scene_id, scene.start, scene.end, scene.content_digest)
                for scene in proof.scenes
            )
            snapshot_ranges = tuple(
                (scene.scene_id, scene.start, scene.end, scene.content_digest)
                for scene in self.snapshot.scene_ranges
            )
            if snapshot_ranges != proof_ranges:
                raise ValueError("review_scene_range_mismatch")
            completion = prose_completion_module.inspect(
                text=self.snapshot.prose, plan=self.prose_plan,
                finish_reason=self.completion["finish_reason"],
                completed_scene_indexes=range(len(proof.scenes)),
                outline_revision=self.snapshot.view_digest,
                expected_outline_revision=self.snapshot.view_digest,
                effective_word_count=self.completion.get("effective_word_count"),
            )
        except (ValueError, TypeError, KeyError):
            raise RequiredAdherenceHandoffError("review_candidate_incomplete") from None
        if completion.status != "complete" or completion.finish_reason != "stop":
            raise RequiredAdherenceHandoffError("review_candidate_incomplete")


class RequiredReviewJournal(Protocol):
    """The candidate pipeline's owner/Job/fence-scoped persistence interface.

    compare_and_swap must atomically reject a replaced checkpoint, stale live
    source, lost lease, or changed authorization. It must durably acknowledge
    the started checkpoint before dispatch AND reserve that checkpoint's full
    per-review attempt/token/serial-time capacity from the dedicated allowance.
    A denied reservation must leave both counters and checkpoint unchanged;
    a completed or lost dispatch never refunds the logical review opportunity.
    Returning False is a safe refusal, not permission to use another pool.
    Replaying a terminal checkpoint uses an identical expected/replacement pair
    as a live fence/source check; it must not reserve capacity a second time.
    This is not a paid attempt scope:
    the reviewer's existing scope still reserves and accounts every request.
    Old Job adapters cannot implement this by a blind append or an upsert.
    """

    async def read(self) -> tuple[RequiredAdherenceCheckpoint, RequiredReviewCandidate]: ...

    async def read_attempts(self) -> tuple[AttemptUsage, ...]:
        """Read all attempts, including claimed/uncertain, for this review."""
        ...

    async def compare_and_swap(
        self, expected: RequiredAdherenceCheckpoint, replacement: RequiredAdherenceCheckpoint,
        *, expected_candidate_digest: str,
    ) -> bool: ...


class RequiredAdherenceHandoff:
    def __init__(self, journal: RequiredReviewJournal, reviewer: IndependentOutlineReviewer):
        self._journal = journal
        self._reviewer = reviewer

    @staticmethod
    def produced(
        receipt: RequiredAdherenceReceipt,
        *,
        snapshot: OutlineReviewSnapshot,
        plan: IndependentReviewPlan,
    ) -> RequiredAdherenceCheckpoint:
        if not isinstance(
            receipt,
            (InitialAwaitingAdherenceReceipt, AwaitingAdherenceReceipt),
        ):
            raise RequiredAdherenceHandoffError("review_receipt_version_invalid")
        if (
            receipt.source_run_id != snapshot.source_run_id
            or receipt.source_run_revision != snapshot.source_run_revision
            or receipt.source_content_digest != snapshot.source_content_digest
            or receipt.view_digest != snapshot.view_digest
            or receipt.review_contract_digest != plan.contract_digest
        ):
            raise RequiredAdherenceHandoffError("review_handoff_stale")
        return RequiredAdherenceCheckpoint(
            schema_version="required_adherence_checkpoint.v1",
            receipt=receipt,
            review_capacity=RequiredAdherenceCapacity.from_plan(plan),
            phase="awaiting_review",
        )

    async def resume(self, plan: IndependentReviewPlan) -> RequiredAdherenceCheckpoint:
        self._require_plan(plan)
        checkpoint, candidate = await self._journal.read()
        self.produced(checkpoint.receipt, snapshot=candidate.snapshot, plan=plan)
        if checkpoint.review_capacity != RequiredAdherenceCapacity.from_plan(plan):
            raise RequiredAdherenceHandoffError("review_capacity_plan_mismatch")
        candidate.require_complete()
        candidate_digest = candidate.check_digest
        if checkpoint.phase == "review_started":
            # A completed paid call without its published result is not a new
            # review opportunity. Recovery must resolve the existing ledger.
            raise RequiredAdherenceHandoffError("review_dispatch_in_doubt")
        if checkpoint.phase in {"review_settled", "review_blocked"}:
            await self._require_ledger(checkpoint, plan)
            if checkpoint.phase == "review_settled":
                self._revalidate(checkpoint, candidate)
            self._require_plan(plan)
            if not await self._journal.compare_and_swap(
                checkpoint, checkpoint, expected_candidate_digest=candidate_digest,
            ):
                raise RequiredAdherenceHandoffError("review_checkpoint_conflict")
            return checkpoint
        if await self._journal.read_attempts():
            raise RequiredAdherenceHandoffError("review_dispatch_in_doubt")
        started = checkpoint.model_copy(update={"phase": "review_started"})
        if not await self._journal.compare_and_swap(
            checkpoint, started, expected_candidate_digest=candidate_digest,
        ):
            raise RequiredAdherenceHandoffError("review_checkpoint_conflict")
        current, candidate = await self._journal.read()
        if current != started:
            raise RequiredAdherenceHandoffError("review_checkpoint_conflict")
        self.produced(started.receipt, snapshot=candidate.snapshot, plan=plan)
        candidate.require_complete()
        candidate_digest = candidate.check_digest
        if await self._journal.read_attempts():
            raise RequiredAdherenceHandoffError("review_dispatch_in_doubt")
        result = await self._reviewer.review(candidate.snapshot, plan)
        attempts = await self._journal.read_attempts()
        # A persistent scope may also report an unknown call with conservative
        # usage; that is a reservation, not observed/settled usage. Compare the
        # accounted rows exactly and bind any reported unknown identity to the
        # authoritative journal. The journal keeps its reservation unchanged.
        reported_accounted = tuple(attempt for attempt in result.attempts if attempt.state == "accounted")
        if (
            tuple(attempt for attempt in attempts if attempt.state == "accounted") != reported_accounted
            or any(
                reported.state not in {"claimed", "uncertain"}
                or not any(
                    (stored.attempt_id, stored.provider_alias, stored.phase, stored.state)
                    == (reported.attempt_id, reported.provider_alias, reported.phase, reported.state)
                    for stored in attempts
                )
                for reported in result.attempts if reported.state != "accounted"
            )
        ):
            raise RequiredAdherenceHandoffError("review_accounting_invalid")
        settled = RequiredAdherenceCheckpoint.model_validate({
            **started.model_dump(),
            "phase": "review_blocked" if result.failure_code else "review_settled",
            "evidence": result.evidence, "failure_code": result.failure_code,
            "attempt_ids": tuple(attempt.attempt_id for attempt in attempts),
            "ledger_digest": self._ledger_digest(attempts),
        })
        current, candidate = await self._journal.read()
        if current != started:
            raise RequiredAdherenceHandoffError("review_checkpoint_conflict")
        self.produced(settled.receipt, snapshot=candidate.snapshot, plan=plan)
        candidate.require_complete()
        if candidate.check_digest != candidate_digest:
            raise RequiredAdherenceHandoffError("review_handoff_stale")
        await self._require_ledger(settled, plan)
        if settled.phase == "review_settled":
            self._revalidate(settled, candidate)
        self._require_plan(plan)
        if not await self._journal.compare_and_swap(
            started, settled, expected_candidate_digest=candidate_digest,
        ):
            raise RequiredAdherenceHandoffError("review_checkpoint_conflict")
        return settled

    def _require_plan(self, plan: IndependentReviewPlan) -> None:
        if not self._reviewer.matches_plan(plan):
            raise RequiredAdherenceHandoffError("review_plan_stale")

    async def _require_ledger(
        self, checkpoint: RequiredAdherenceCheckpoint, plan: IndependentReviewPlan,
    ) -> None:
        attempts = await self._journal.read_attempts()
        self._validate_ledger(checkpoint, plan, attempts)

    @staticmethod
    def validate_terminal(
        checkpoint: RequiredAdherenceCheckpoint, *, candidate: RequiredReviewCandidate,
        plan: IndependentReviewPlan, attempts: tuple[AttemptUsage, ...],
    ) -> None:
        """Shared gate for the scheduler and its authoritative storage adapter."""
        checkpoint = RequiredAdherenceCheckpoint.model_validate_json(checkpoint.model_dump_json())
        if checkpoint.phase not in {"review_settled", "review_blocked"}:
            raise RequiredAdherenceHandoffError("review_checkpoint_unfinished")
        RequiredAdherenceHandoff.produced(checkpoint.receipt, snapshot=candidate.snapshot, plan=plan)
        if checkpoint.review_capacity != RequiredAdherenceCapacity.from_plan(plan):
            raise RequiredAdherenceHandoffError("review_capacity_plan_mismatch")
        candidate.require_complete()
        RequiredAdherenceHandoff._validate_ledger(checkpoint, plan, attempts)
        if checkpoint.phase == "review_settled":
            RequiredAdherenceHandoff._revalidate(checkpoint, candidate)

    @staticmethod
    def _validate_ledger(
        checkpoint: RequiredAdherenceCheckpoint, plan: IndependentReviewPlan,
        attempts: tuple[AttemptUsage, ...],
    ) -> None:
        uncertain = checkpoint.failure_code == "review_uncertain"
        if (
            tuple(attempt.attempt_id for attempt in attempts) != checkpoint.attempt_ids
            or len(set(checkpoint.attempt_ids)) != len(checkpoint.attempt_ids)
            or any(
                attempt.state not in ({"accounted", "claimed", "uncertain"} if uncertain else {"accounted"})
                or attempt.provider_alias != plan.generation.provider_alias
                for attempt in attempts
            )
            or tuple(attempt.phase for attempt in attempts) not in {
                (), ("primary",), ("primary", "repair"),
            }
            or uncertain != any(attempt.state in {"claimed", "uncertain"} for attempt in attempts)
            or checkpoint.ledger_digest != RequiredAdherenceHandoff._ledger_digest(attempts)
        ):
            raise RequiredAdherenceHandoffError("review_accounting_invalid")
        for attempt in attempts:
            usage = attempt.usage
            if (
                any(type(value) is not int or value < 0 or value > 2**63 - 1 for value in (
                    usage.input_tokens, usage.output_tokens, usage.total_tokens,
                ))
                or usage.total_tokens != usage.input_tokens + usage.output_tokens
                or usage.input_tokens > plan.input_token_bound
                or usage.output_tokens > plan.generation.max_output_tokens
            ):
                raise RequiredAdherenceHandoffError("review_accounting_invalid")

    @staticmethod
    def _ledger_digest(attempts: tuple[AttemptUsage, ...]) -> str:
        values = [{
            "attempt_id": attempt.attempt_id,
            "provider_alias": attempt.provider_alias,
            "phase": attempt.phase,
            "state": attempt.state,
            "usage": attempt.usage.model_dump(),
        } for attempt in attempts]
        return hashlib.sha256(json.dumps(
            values, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    @staticmethod
    def _revalidate(checkpoint: RequiredAdherenceCheckpoint, candidate: RequiredReviewCandidate) -> None:
        snapshot = candidate.snapshot
        try:
            revalidate_current_outline_adherence_evidence(
                checkpoint.evidence,
                outline=json.loads(snapshot.outline_json), prose=snapshot.prose,
                source_prose_run_id=snapshot.source_run_id,
                source_prose_run_revision=snapshot.source_run_revision,
                source_content_digest=snapshot.source_content_digest,
            )
        except OutlineAdherenceValidationError:
            raise RequiredAdherenceHandoffError("review_evidence_invalid") from None
