"""Candidate-only rewrite reservations on the original, leased Job ledger."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
import json
from typing import Any, Literal

from pydantic import Field

from backend.db.required_adherence_journal import (
    RequiredReviewJobBinding, _ReviewAuthorization, _ReviewLedger,
    _checked_bookkeeping, _integer, _job_matches_binding, _job_snapshot_query,
    candidate_write_fences,
)
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.prose_run_repository import prose_run_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db.utils import get_utc_now
from backend.services.generation.job_execution import current_job_execution
from backend.services.agent_runtime.contracts import RuntimeCallUsage, RuntimeToolResult
from backend.services.generation.prose_runs import prose_revision
from backend.services.generation.required_prose_rewrite_contracts import (
    ClosedRewriteModel, RequiredProseRewriteRequest, RequiredRewriteOrigin,
    RequiredRewriteAuthorization, REQUIRED_REWRITE_STEP_PREFIX, contract_digest,
    RequiredRewriteCandidateOrigin, REQUIRED_REWRITE_SCOPE, REQUIRED_REWRITE_TOOL,
    REQUIRED_REWRITE_FINISH, REQUIRED_REWRITE_RESULT,
)
from backend.services.generation.required_adherence_handoff import (
    InitialAwaitingAdherenceReceipt,
    required_review_ordinal,
)
from backend.services.generation.required_initial_prose_contracts import (
    ReviewedInitialProseSource,
)


class RequiredProseRewriteError(ValueError):
    pass


class RequiredRewriteDispatchRejected(RequiredProseRewriteError):
    provider_request_not_dispatched = True


class RewriteEntry(ClosedRewriteModel):
    origin: RequiredRewriteOrigin
    outline_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_proof_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    worker_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_epoch: int = Field(ge=1)
    readiness_id: str | None = None
    readiness_digest: str | None = None
    phase: Literal["reserved", "produced", "incomplete", "blocked"] = "reserved"
    result_revision: int | None = None
    result_digest: str | None = None
    agent_run_id: str | None = None
    prior_review_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @property
    def start_request_id(self) -> str:
        return "required-rewrite-" + self.origin.request_digest

    def step_id(self, kind: str) -> str:
        if kind not in {"planner", "rewrite"}:
            raise ValueError("unknown required rewrite step")
        return f"{REQUIRED_REWRITE_STEP_PREFIX}{self.origin.request_digest}:{kind}"


class RewriteJournal(ClosedRewriteModel):
    schema_version: Literal["required_prose_rewrite_job_journal.v1"] = "required_prose_rewrite_job_journal.v1"
    binding: RequiredReviewJobBinding
    authorization: RequiredRewriteAuthorization
    review_authorization: _ReviewAuthorization
    entries: list[RewriteEntry] = Field(default_factory=list, max_length=2)


def _source_digest(run: Mapping) -> str:
    return contract_digest({key: run.get(key) for key in (
        "assembled_text", "revision", "outline_revision", "narrative_revision", "status", "plan", "completion", "remediation",
    )})


def _checked_journal(job: Mapping, binding: RequiredReviewJobBinding, authorization=None):
    lease = current_job_execution()
    try:
        from backend.services.generation.required_chapter_review_job import (
            required_review_authorization_from_planning,
            required_rewrite_authorization_from_planning,
        )

        planning = job["readiness"]["planning"]
        auth = RequiredRewriteAuthorization.model_validate(
            required_rewrite_authorization_from_planning(planning)
        )
        review = _ReviewAuthorization.model_validate(
            required_review_authorization_from_planning(planning)
        )
        if (
            lease is None or lease.job_id != binding.job_id
            or not _job_matches_binding(job, binding)
            or job["readiness"]["digest"] != binding.readiness_digest
            or authorization is not None and auth != authorization
            or review.capacity != auth.review_capacity or binding.chapter_id not in review.chapter_ids
            or job.get("has_uncertain_attempts") is not False
        ):
            raise ValueError("rewrite binding changed")
        raw = job.get("required_prose_rewrite_journal")
        journal = RewriteJournal(binding=binding, authorization=auth, review_authorization=review) if raw is None else (
            RewriteJournal.model_validate_json(json.dumps(raw))
        )
        if journal.binding != binding or journal.authorization != auth or journal.review_authorization != review:
            raise ValueError("rewrite authorization changed")
        for index, entry in enumerate(journal.entries):
            expected = contract_digest({"binding": binding.model_dump(mode="json"),
                                        "contract": auth.contract_digest, "request": entry.origin.request.model_dump(mode="json")})
            if (
                entry.origin.rewrite_ordinal != index + 1
                or entry.origin.request_digest != expected
                or entry.origin.job_id != binding.job_id
                or entry.origin.readiness_digest != binding.readiness_digest
                or entry.origin.authorization_revision != binding.authorization_revision
                or entry.origin.contract_digest != auth.contract_digest
                or entry.origin.review_contract_digest != auth.review_capacity.review_contract_digest
            ):
                raise ValueError("rewrite lineage invalid")
        _checked_bookkeeping(job)
        known_steps = {entry.step_id(kind) for entry in journal.entries for kind in ("planner", "rewrite")}
        raw_initial = job.get("required_initial_prose_journal")
        initial = None
        if raw_initial is not None:
            from backend.db.required_initial_prose_journal import (
                RequiredInitialProseJournal,
            )
            from backend.services.generation.required_initial_prose_contracts import (
                REQUIRED_INITIAL_PROSE_STEP_PREFIX,
            )

            initial = RequiredInitialProseJournal.model_validate_json(
                json.dumps(raw_initial)
            )
            if initial.phase not in {"produced", "incomplete"}:
                raise ValueError("initial prose is not settled")
            known_steps.add(
                f"{REQUIRED_INITIAL_PROSE_STEP_PREFIX}{initial.origin.request_digest}"
            )
        raw_reviews = job.get("required_adherence_journal")
        reviews = None
        if raw_reviews is not None:
            reviews = _ReviewLedger.model_validate_json(json.dumps(raw_reviews))
            if reviews.binding != binding or reviews.writer_contract_digest != auth.contract_digest:
                raise ValueError("old review journal cannot be migrated")
            known_steps.update("required-adherence:" + entry.checkpoint.receipt.view_digest for entry in reviews.entries)
        for index, entry in enumerate(journal.entries):
            if index == 0:
                if entry.prior_review_digest is None:
                    if initial is not None and (
                        initial.phase != "incomplete"
                        or entry.origin.request.source_run_id != initial.run_id
                        or entry.origin.request.source_revision
                        != initial.run_revision
                        or entry.origin.request.source_content_digest
                        != initial.content_digest
                    ):
                        raise ValueError(
                            "initial incomplete source changed"
                        )
                    continue
                checkpoints = [] if reviews is None else [
                    item.checkpoint
                    for item in reviews.entries
                    if required_review_ordinal(item.checkpoint.receipt) == 0
                ]
                if (
                    initial is None
                    or len(checkpoints) != 1
                    or not isinstance(
                        checkpoints[0].receipt,
                        InitialAwaitingAdherenceReceipt,
                    )
                    or contract_digest(
                        checkpoints[0].model_dump(mode="json")
                    ) != entry.prior_review_digest
                    or checkpoints[0].phase != "review_settled"
                    or (checkpoints[0].evidence or {}).get("decision")
                    != "repair"
                    or entry.origin.request.source_run_id
                    != checkpoints[0].receipt.source_run_id
                    or entry.origin.request.source_revision
                    != checkpoints[0].receipt.source_run_revision
                    or entry.origin.request.source_content_digest
                    != checkpoints[0].receipt.source_content_digest
                ):
                    raise ValueError("initial settled repair decision changed")
                continue
            previous = journal.entries[index - 1]
            if (
                previous.phase not in {"produced", "incomplete"}
                or entry.origin.request.source_run_id != previous.origin.request.source_run_id
                or entry.origin.request.source_revision != previous.result_revision
                or entry.origin.request.source_content_digest != previous.result_digest
            ):
                raise ValueError("rewrite source lineage changed")
            if previous.phase == "incomplete":
                if entry.prior_review_digest is not None:
                    raise ValueError("partial progress cannot adopt a review")
                continue
            checkpoints = [] if reviews is None else [
                item.checkpoint for item in reviews.entries
                if required_review_ordinal(item.checkpoint.receipt)
                == previous.origin.rewrite_ordinal
            ]
            if (
                len(checkpoints) != 1 or entry.prior_review_digest is None
                or contract_digest(checkpoints[0].model_dump(mode="json")) != entry.prior_review_digest
                or checkpoints[0].phase != "review_settled"
                or (checkpoints[0].evidence or {}).get("decision") != "repair"
            ):
                raise ValueError("previous settled repair decision changed")
        if any(slot.get("step_id") not in known_steps or slot.get("chapter_id") != binding.chapter_id for slot in job["attempt_slots"]):
            raise ValueError("old or orphaned attempts cannot enter the rewrite contract")
        for entry in journal.entries:
            rewrite_accounting(job, entry, journal.authorization)
        return journal
    except RequiredProseRewriteError:
        raise
    except (KeyError, TypeError, ValueError):
        raise RequiredProseRewriteError(
            "rewrite_job_contract_invalid"
        ) from None


async def _source(binding, request):
    run = await prose_run_repo.get_run(request.source_run_id, binding.owner_id)
    chapter = await chapter_repo.get_chapter_by_id(binding.chapter_id)
    novel = await novel_repo.get_novel_by_id(binding.novel_id)
    if (
        str(run.get("generation_job_id")) != binding.job_id
        or str(run.get("novel_id")) != binding.novel_id or str(run.get("chapter_id")) != binding.chapter_id
        or str(chapter.get("novel_id")) != binding.novel_id or str(novel.get("owner_id")) != binding.owner_id
        or run.get("has_uncertain_attempt") is not False or run.get("lease") is not None
        or not _integer(run.get("revision"))
        or not _integer(run.get("narrative_revision")) or run["narrative_revision"] != binding.narrative_revision
        or not _integer(novel.get("narrative_revision")) or novel["narrative_revision"] != binding.narrative_revision
        or run.get("outline_revision") != prose_revision(chapter.get("outline"))
        or not isinstance(run.get("assembled_text"), str)
    ):
        raise RequiredProseRewriteError("rewrite_source_stale")
    return run, chapter


async def reviewed_initial_source_authority(
    job,
    *,
    binding,
    ledger: RewriteJournal,
    entry: RewriteEntry,
    source,
) -> ReviewedInitialProseSource | None:
    """Re-prove the only locked-complete source allowed into rewrite one."""

    if entry.origin.rewrite_ordinal != 1:
        return None
    if entry.prior_review_digest is None:
        return None
    try:
        if ledger.entries[0] != entry:
            raise ValueError("not the first reviewed source")
        from backend.db.required_initial_prose_journal import (
            RequiredInitialProseJournal,
            validate_produced_initial_prose,
        )

        initial = RequiredInitialProseJournal.model_validate_json(
            json.dumps(job["required_initial_prose_journal"])
        )
        await validate_produced_initial_prose(job, binding=binding, run=source)
        return ReviewedInitialProseSource(
            origin=initial.origin,
            run_id=str(source["_id"]),
            run_revision=int(source["revision"]),
            content_digest=entry.origin.request.source_content_digest,
            review_checkpoint_digest=entry.prior_review_digest,
        )
    except (KeyError, IndexError, TypeError, ValueError):
        raise RequiredProseRewriteError(
            "rewrite_reviewed_initial_source_stale"
        ) from None


def _require_candidate_attempt_identity(job, ledger, source):
    """Check the candidate CAS receipt before any possible continuation call.

    The ProseRun pointer is the original durable result even if the independent
    receipt acknowledgement or Agent step archival was interrupted. This is a
    pre-dispatch identity check, not a substitute for full receipt adoption.
    """
    remediation = source.get("remediation") or {}
    if remediation.get("schema_version") != "prose_run_remediation.v2":
        return
    try:
        origin = RequiredRewriteCandidateOrigin.model_validate_json(json.dumps(remediation.get("required_adherence_origin")))
        entry = next(item for item in ledger.entries if item.origin.request_digest == origin.request_digest)
        pointer = remediation["latest_receipt"]
        result = RuntimeToolResult.model_validate(pointer["result_projection"])
        if (
            origin != RequiredRewriteCandidateOrigin(**entry.origin.model_dump(), agent_run_id=origin.agent_run_id)
            or source["revision"] != entry.origin.request.source_revision + 1
            or pointer.get("source_revision") != entry.origin.request.source_revision
            or result.code not in {REQUIRED_REWRITE_RESULT, "prose_candidate_checkpointed"}
            or result.audit_view.get("required_rewrite_attempt_ids") != [
                slot["attempt_id"] for slot in job["attempt_slots"] if slot["step_id"] == entry.step_id("rewrite")
            ]
        ):
            raise ValueError("writer original attempt identity changed")
    except (KeyError, StopIteration, TypeError, ValueError):
        raise RequiredProseRewriteError("rewrite_original_receipt_invalid") from None


def _capacity(job, authorization, review, *, writer_attempts, writer_tokens, writer_seconds):
    reservation = job.get("attempt_reservation")
    capacity = authorization.review_capacity
    if (
        not isinstance(reservation, dict) or reservation.get("chapter_id") != job.get("current_chapter_id")
        or not _integer(reservation.get("reserved_slots")) or not _integer(reservation.get("claimed_slots"))
        or any(slot.get("state") not in {"accounted", "released_pre_dispatch"} for slot in job["attempt_slots"])
        or job.get("active_token_reservations") != []
    ):
        raise RequiredProseRewriteError("rewrite_accounting_incomplete")
    attempts = writer_attempts + capacity.max_attempts_per_review
    if (
        job["usage_attempt_capacity"] - job["usage_attempt_claimed"] < attempts
        or reservation["reserved_slots"] - reservation["claimed_slots"] < attempts
        or job["token_budget"] - job["tokens_used"] - job["tokens_reserved"] < writer_tokens + capacity.tokens_per_review
        or review.deadline_at < get_utc_now() + timedelta(seconds=writer_seconds + capacity.seconds_per_review)
    ):
        raise RequiredProseRewriteError("rewrite_required_review_capacity")


class JobRequiredProseRewriteJournal:
    def __init__(self, binding, *, authorization, review_plan, read_job, write_job):
        self.binding = binding
        self.authorization = RequiredRewriteAuthorization.model_validate_json(json.dumps(authorization))
        self._read_job, self._write_job = read_job, write_job
        self._review_plan = review_plan

    async def read(self):
        job = await self._read_job(self.binding.job_id)
        ledger = _checked_journal(job, self.binding, self.authorization)
        if ledger.review_authorization.provider_alias != self._review_plan.generation.provider_alias:
            raise RequiredProseRewriteError("rewrite_review_plan_mismatch")
        if ledger.entries:
            source, _ = await _source(self.binding, ledger.entries[-1].origin.request)
            _require_candidate_attempt_identity(job, ledger, source)
        return job, ledger

    async def begin(self, request: RequiredProseRewriteRequest) -> RewriteEntry:
        request.tool_arguments()
        job, ledger = await self.read()
        key = contract_digest({"binding": self.binding.model_dump(mode="json"),
                               "contract": self.authorization.contract_digest, "request": request.model_dump(mode="json")})
        for entry in ledger.entries:
            if entry.origin.request_digest == key:
                return await self._adopt_reserved(job, ledger, entry)
        if len(ledger.entries) >= 2:
            raise RequiredProseRewriteError("rewrite_dispatch_limit")
        source, _ = await _source(self.binding, request)
        prior_review_digest = None
        import hashlib
        if source["revision"] != request.source_revision or hashlib.sha256(source["assembled_text"].encode()).hexdigest() != request.source_content_digest:
            raise RequiredProseRewriteError("rewrite_source_stale")
        if not ledger.entries:
            if (
                source.get("status") == "complete"
                and source.get("completion", {}).get("status") == "complete"
                and source.get("completion", {}).get("can_write_formal_prose")
                is False
            ):
                prior_review_digest = await self._require_source_review(
                    job,
                    request,
                    expected_review_ordinal=0,
                )
            elif source.get("remediation") or source.get("status") != "incomplete" or source.get("completion", {}).get("status") != "incomplete":
                raise RequiredProseRewriteError("rewrite_initial_failure_required")
            else:
                raw_initial = job.get("required_initial_prose_journal")
                if raw_initial is not None:
                    from backend.db.required_initial_prose_journal import (
                        validate_incomplete_initial_prose,
                    )

                    await validate_incomplete_initial_prose(
                        job,
                        binding=self.binding,
                        run=source,
                    )
        else:
            previous = ledger.entries[-1]
            if (
                previous.phase not in {"produced", "incomplete"} or previous.result_revision != request.source_revision
                or previous.result_digest != request.source_content_digest
                or previous.origin.request.source_run_id != request.source_run_id
            ):
                raise RequiredProseRewriteError("rewrite_previous_result_required")
            if previous.phase == "incomplete":
                from backend.services.generation.prose_remediation_runtime import ResumableProseCandidateCheckpoint

                marker = ResumableProseCandidateCheckpoint.model_validate(source.get("completion", {}).get("resumable_scene_repair"))
                origin = RequiredRewriteCandidateOrigin.model_validate_json(json.dumps(source.get("remediation", {}).get("required_adherence_origin")))
                if (
                    source.get("status") != "incomplete" or source.get("completion", {}).get("can_write_formal_prose") is not False
                    or origin != RequiredRewriteCandidateOrigin(**previous.origin.model_dump(), agent_run_id=previous.agent_run_id)
                    or marker.candidate_revision != request.source_revision or marker.content_digest != request.source_content_digest
                    or marker.agent_run_id != previous.agent_run_id
                    or marker.remaining_scene_indexes != request.scene_indexes or marker.issue_categories != request.issue_categories
                ):
                    raise RequiredProseRewriteError("rewrite_partial_scope_invalid")
                rewrite_accounting(job, previous, ledger.authorization)
            else:
                prior_review_digest = await self._require_source_review(
                    job,
                    request,
                    expected_review_ordinal=previous.origin.rewrite_ordinal,
                )
        auth = ledger.authorization
        _capacity(job, auth, ledger.review_authorization, writer_attempts=auth.attempts, writer_tokens=auth.tokens, writer_seconds=auth.seconds)
        lease = current_job_execution()
        entry = RewriteEntry(
            origin=RequiredRewriteOrigin(
                job_id=self.binding.job_id, readiness_digest=self.binding.readiness_digest,
                authorization_revision=self.binding.authorization_revision,
                contract_digest=auth.contract_digest, request_digest=key,
                review_contract_digest=auth.review_capacity.review_contract_digest,
                rewrite_ordinal=len(ledger.entries) + 1, request=request,
            ), outline_revision=source["outline_revision"], source_proof_digest=_source_digest(source),
            worker_id=lease.worker_id, execution_epoch=lease.epoch,
            prior_review_digest=prior_review_digest,
        )
        updated = ledger.model_copy(update={"entries": [*ledger.entries, entry]})
        async with candidate_write_fences(self.binding, run_id=request.source_run_id, run_revision=request.source_revision) as expires:
            current, _ = await _source(self.binding, request)
            if _source_digest(current) != entry.source_proof_digest:
                raise RequiredProseRewriteError("rewrite_source_stale")
            if not await self._save(job, updated, expires=expires, seconds=auth.seconds + auth.review_capacity.seconds_per_review):
                raise RequiredProseRewriteError("rewrite_checkpoint_conflict")
        return entry

    async def _adopt_reserved(self, job, ledger, entry):
        """Transfer only execution ownership under the already-acquired Job lease.

        The original readiness, Agent start identity, receipts and charges do
        not change. AgentRuntime must still recover its own original attempt;
        this cannot make a dispatched/unknown writer retryable.
        """
        lease = current_job_execution()
        if entry.phase != "reserved" or (entry.worker_id, entry.execution_epoch) == (lease.worker_id, lease.epoch):
            return entry
        if lease.epoch <= entry.execution_epoch:
            raise RequiredProseRewriteError("rewrite_execution_owner_invalid")
        # read() has already required complete original accounting and the
        # current Job lease. The same snapshot and live lease fence this CAS.
        adopted = entry.model_copy(update={"worker_id": lease.worker_id, "execution_epoch": lease.epoch})
        entries = [adopted if item == entry else item for item in ledger.entries]
        if not await self._save(job, ledger.model_copy(update={"entries": entries})):
            raise RequiredProseRewriteError("rewrite_checkpoint_conflict")
        return adopted

    async def _require_source_review(
        self,
        job,
        request,
        *,
        expected_review_ordinal,
    ):
        from backend.db.repositories.generation_job_repository import generation_job_repo
        from backend.services.generation.independent_outline_review import OutlineReviewSnapshot
        from backend.services.generation.prose_remediation_runtime import _execution_plan
        from backend.services.generation.required_adherence_handoff import RequiredReviewCandidate, RequiredAdherenceHandoff
        from backend.services.llm.context_builder import fetch_context_inputs, assemble_context

        try:
            source, chapter = await _source(self.binding, request)
            context = assemble_context(await fetch_context_inputs(self.binding.novel_id, self.binding.chapter_id))
            snapshot = OutlineReviewSnapshot.create(
                source_run_id=request.source_run_id, source_run_revision=request.source_revision,
                source_content_digest=request.source_content_digest, prose=source["assembled_text"],
                outline=chapter["outline"], authorized_context=context.to_prompt_text(),
            )
            candidate = RequiredReviewCandidate(snapshot, _execution_plan(source), source["completion"])
            journal = generation_job_repo.required_review_journal(self.binding, candidate=candidate, plan=self._review_plan)
            checkpoint, current = await journal.read()
            RequiredAdherenceHandoff.validate_terminal(
                checkpoint, candidate=current, plan=self._review_plan, attempts=await journal.read_attempts(),
            )
            if (
                checkpoint.phase != "review_settled" or checkpoint.evidence.get("decision") != "repair"
                or checkpoint.receipt.source_run_revision != request.source_revision
                or checkpoint.receipt.source_content_digest != request.source_content_digest
                or required_review_ordinal(checkpoint.receipt)
                != expected_review_ordinal
            ):
                raise ValueError("previous independent review missing")
            return contract_digest(checkpoint.model_dump(mode="json"))
        except (KeyError, IndexError, TypeError, ValueError):
            raise RequiredProseRewriteError("rewrite_previous_review_required") from None

    async def replace(self, expected: RewriteEntry, replacement: RewriteEntry) -> RewriteEntry:
        job, ledger = await self.read()
        index = next((i for i, item in enumerate(ledger.entries) if item.origin.request_digest == expected.origin.request_digest), None)
        if index is None:
            raise RequiredProseRewriteError("rewrite_checkpoint_missing")
        if ledger.entries[index] != expected:
            if ledger.entries[index] == replacement:
                return replacement
            raise RequiredProseRewriteError("rewrite_checkpoint_conflict")
        entries = list(ledger.entries)
        entries[index] = replacement
        if not await self._save(job, ledger.model_copy(update={"entries": entries})):
            raise RequiredProseRewriteError("rewrite_checkpoint_conflict")
        return replacement

    async def _save(self, job, updated, *, expires=None, seconds=0):
        query = _job_snapshot_query(job)
        if expires is not None:
            query["$and"].append({"$expr": {"$and": [
                {"$gt": [{"$literal": expires}, "$$NOW"]},
                {"$gte": [{"$literal": updated.review_authorization.deadline_at}, {"$add": ["$$NOW", seconds * 1000]}]},
            ]}})
        result = await self._write_job(query, {"$set": {"required_prose_rewrite_journal": updated.model_dump(mode="json"), "updated_at": get_utc_now()}})
        return result.matched_count == 1


async def write_required_rewrite_claim(job, *, chapter_id, step_id, phase, provider_alias, conservative_tokens, query, update, write_job):
    try:
        raw = RewriteJournal.model_validate_json(json.dumps(job["required_prose_rewrite_journal"]))
        ledger = _checked_journal(job, raw.binding)
        entry = ledger.entries[-1]
        lease = current_job_execution()
        kind = next((kind for kind in ("planner", "rewrite") if step_id == entry.step_id(kind)), None)
        if (
            entry.phase != "reserved" or kind is None or entry.readiness_id is None
            or chapter_id != raw.binding.chapter_id or entry.worker_id != lease.worker_id or entry.execution_epoch != lease.epoch
        ):
            raise ValueError("rewrite dispatch authority missing")
        bound = ledger.authorization.planner if kind == "planner" else ledger.authorization.rewrite
        own = [slot for slot in job["attempt_slots"] if slot.get("step_id") == step_id]
        all_own = [slot for slot in job["attempt_slots"] if slot.get("step_id") in {entry.step_id("planner"), entry.step_id("rewrite")}]
        max_calls = 2 if kind == "planner" else 1
        if (
            provider_alias != bound.provider_alias or phase not in {"primary", "repair"}
            or len(own) >= max_calls * bound.max_attempts
            or phase == "repair" and (not own or own[-1]["phase"] != "primary")
            or sum(slot["phase"] == "primary" for slot in own) >= max_calls and phase == "primary"
            or type(conservative_tokens) is not int or not 0 < conservative_tokens <= bound.input_tokens + bound.output_tokens
        ):
            raise ValueError("rewrite claim outside frozen contract")
        remaining_tokens = ledger.authorization.tokens - sum(slot["conservative_tokens"] for slot in all_own)
        remaining_attempts = ledger.authorization.attempts - len(all_own)
        remaining_seconds = ledger.authorization.seconds - sum(
            ledger.authorization.planner.timeout_seconds if slot["step_id"] == entry.step_id("planner") else ledger.authorization.rewrite.timeout_seconds
            for slot in all_own
        )
        _capacity(job, ledger.authorization, ledger.review_authorization,
                  writer_attempts=remaining_attempts, writer_tokens=remaining_tokens, writer_seconds=remaining_seconds)
        source, _ = await _source(raw.binding, entry.origin.request)
        _require_candidate_attempt_identity(job, ledger, source)
        if source["revision"] == entry.origin.request.source_revision:
            if _source_digest(source) != entry.source_proof_digest:
                raise ValueError("rewrite source changed")
        elif not (
            kind == "planner" and source["revision"] == entry.origin.request.source_revision + 1
            and (source.get("remediation") or {}).get("required_adherence_origin", {}).get("request_digest") == entry.origin.request_digest
        ):
            raise ValueError("rewrite source changed")
    except RequiredProseRewriteError as error:
        raise RequiredRewriteDispatchRejected(str(error)) from None
    except KeyError:
        raise RequiredRewriteDispatchRejected(
            "rewrite_dispatch_field_missing"
        ) from None
    except IndexError:
        raise RequiredRewriteDispatchRejected(
            "rewrite_dispatch_entry_missing"
        ) from None
    except TypeError:
        raise RequiredRewriteDispatchRejected(
            "rewrite_dispatch_type_invalid"
        ) from None
    except ValueError as error:
        reason_codes = {
            "rewrite dispatch authority missing": "rewrite_dispatch_authority_missing",
            "rewrite claim outside frozen contract": "rewrite_dispatch_claim_outside_contract",
            "rewrite source changed": "rewrite_dispatch_source_changed",
        }
        raise RequiredRewriteDispatchRejected(
            reason_codes.get(str(error), "rewrite_dispatch_value_invalid")
        ) from None
    async with candidate_write_fences(raw.binding, run_id=entry.origin.request.source_run_id, run_revision=int(source["revision"])) as expires:
        current, _ = await _source(raw.binding, entry.origin.request)
        if _source_digest(current) != _source_digest(source):
            raise RequiredRewriteDispatchRejected("rewrite_dispatch_rejected")
        guarded = {"$and": [dict(query), _job_snapshot_query(job), {"$expr": {"$and": [
            {"$gt": [{"$literal": expires}, "$$NOW"]},
            {"$gte": [{"$literal": ledger.review_authorization.deadline_at}, {"$add": ["$$NOW", (remaining_seconds + ledger.authorization.review_capacity.seconds_per_review) * 1000]}]},
        ]}}]}
        return await write_job(guarded, update)


async def validate_required_rewrite_origin(origin, *, agent_run_id: str, owner_id: str):
    """Re-prove the parent authority immediately before a candidate mutation."""
    from backend.db.repositories.agent_runtime_repository import agent_runtime_repository
    from backend.db.repositories.generation_job_repository import generation_job_repo

    job = await generation_job_repo.get_job(origin.job_id)
    try:
        raw = RewriteJournal.model_validate_json(json.dumps(job["required_prose_rewrite_journal"]))
        ledger = _checked_journal(job, raw.binding)
        entry = ledger.entries[-1]
        lease = current_job_execution()
        stored_lease = job["execution_lease"]
        if (
            entry.origin != origin or entry.phase != "reserved"
            or raw.binding.owner_id != owner_id
            or entry.worker_id != lease.worker_id or entry.execution_epoch != lease.epoch
            or stored_lease["worker_id"] != lease.worker_id or stored_lease["epoch"] != lease.epoch
            or stored_lease["expires_at"] <= get_utc_now()
        ):
            raise ValueError("writer execution owner changed")
        source, _ = await _source(raw.binding, entry.origin.request)
        initial_authority = await reviewed_initial_source_authority(
            job,
            binding=raw.binding,
            ledger=ledger,
            entry=entry,
            source=source,
        )
        agent = await agent_runtime_repository.get_run_owned(run_id=agent_run_id, owner_id=owner_id)
        if (
            agent.get("status") != "running" or agent.get("start_request_id") != entry.start_request_id
            or agent.get("authorization_digest") != entry.readiness_digest
            or contract_digest(agent.get("authorization")) != entry.readiness_digest
            or str(agent.get("novel_id")) != raw.binding.novel_id
        ):
            raise ValueError("writer Agent does not own the logical repair")
        return initial_authority
    except (KeyError, IndexError, TypeError, ValueError):
        raise RequiredProseRewriteError("rewrite_job_authority_stale") from None


def rewrite_accounting(job, entry, authorization):
    """Project exact original Provider settlements, not just matching totals."""
    _checked_bookkeeping(job)
    usage = {}
    for kind, bound, max_calls in (
        ("planner", authorization.planner, 2), ("rewrite", authorization.rewrite, 1),
    ):
        own = [slot for slot in job["attempt_slots"] if slot.get("step_id") == entry.step_id(kind)]
        calls, in_call, previous = 0, 0, None
        input_tokens = output_tokens = 0
        for slot in own:
            raw = slot.get("usage")
            phase = slot.get("phase")
            if phase == "primary":
                calls += 1
                in_call = 1
            elif phase == "repair" and previous == "primary":
                in_call += 1
            else:
                raise RequiredProseRewriteError("rewrite_accounting_invalid")
            if (
                slot.get("provider_alias") != bound.provider_alias
                or slot.get("chapter_id") != job["current_chapter_id"]
                or slot.get("state") != "accounted" or calls > max_calls or in_call > bound.max_attempts
                or not isinstance(raw, dict) or set(raw) != {"input_tokens", "output_tokens", "total_tokens"}
                or any(not _integer(value) for value in raw.values())
                or raw["input_tokens"] + raw["output_tokens"] != raw["total_tokens"]
                or raw["input_tokens"] > bound.input_tokens or raw["output_tokens"] > bound.output_tokens
                or slot.get("charged_tokens") != raw["total_tokens"]
                or not 0 < raw["total_tokens"] <= slot["conservative_tokens"] <= bound.input_tokens + bound.output_tokens
            ):
                raise RequiredProseRewriteError("rewrite_accounting_invalid")
            input_tokens += raw["input_tokens"]
            output_tokens += raw["output_tokens"]
            previous = phase
        usage[kind] = RuntimeCallUsage(
            paid_attempts=len(own), input_tokens=input_tokens, output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        )
    return usage


async def read_original_rewrite_result(
    *, job: Mapping[str, Any], binding: RequiredReviewJobBinding, entry: RewriteEntry,
    agent_run_id: str, step: Mapping[str, Any], writer_usage: RuntimeCallUsage,
) -> tuple[dict[str, Any], RuntimeToolResult]:
    """One receipt proof shared by first materialization and later adoption."""
    from backend.services.agent_runtime.contracts import RuntimeToolContext, AgentScope
    from backend.services.generation.prose_remediation_runtime import (
        RewriteProseCandidateInput, _rewrite_request_digest, _validated_rewrite_receipt_result,
    )

    try:
        invocation = step.get("tool_invocation") or {}
        attempts = [item for item in step.get("attempt_ledger", []) if item.get("kind") == "tool"]
        expected_scope = {"kind": REQUIRED_REWRITE_SCOPE, "object_id": entry.origin.request.source_run_id}
        if (
            len(attempts) != 1 or attempts[0].get("state") != "settled"
            or step.get("status") != "completed" or not isinstance(step.get("observation"), Mapping)
            or invocation.get("tool") != REQUIRED_REWRITE_TOOL.model_dump(mode="json")
            or invocation.get("scope") != expected_scope
            or invocation.get("arguments") != entry.origin.request.tool_arguments()
        ):
            raise ValueError("writer step identity changed")
        context = RuntimeToolContext(
            owner_id=binding.owner_id, novel_id=binding.novel_id, run_id=agent_run_id,
            step_id=str(step["step_id"]), scope=AgentScope(**expected_scope), authorization_digest=entry.readiness_digest,
        )
        payload = RewriteProseCandidateInput.model_validate(invocation["arguments"])
        found = await prose_run_repo.find_remediation_receipt(
            run_id=entry.origin.request.source_run_id, owner_id=binding.owner_id, novel_id=binding.novel_id,
            idempotency_key=f"{invocation['idempotency_key']}:{attempts[0]['call_key']}",
            request_digest=_rewrite_request_digest(context=context, payload=payload),
        )
        if found is None or found[1].get("state") != "completed":
            raise ValueError("writer original receipt missing")
        result = _validated_rewrite_receipt_result(document=found[0], receipt=found[1], context=context, payload=payload)
        observation = RuntimeToolResult.model_validate({
            key: value for key, value in step["observation"].items()
            if key in RuntimeToolResult.model_fields and key != "schema_version"
        })
        if (
            result != observation or result.status != "ok"
            or result.code not in {REQUIRED_REWRITE_RESULT, "prose_candidate_checkpointed"}
            or result.usage != writer_usage or attempts[0].get("usage") != result.usage.model_dump(mode="json")
            or result.audit_view.get("required_rewrite_attempt_ids") != [
                slot["attempt_id"] for slot in job["attempt_slots"] if slot["step_id"] == entry.step_id("rewrite")
            ]
        ):
            raise ValueError("writer original result changed")
        return found[0], result
    except (KeyError, IndexError, TypeError, ValueError):
        raise RequiredProseRewriteError("rewrite_original_receipt_invalid") from None


async def validate_produced_rewrite(job, *, binding, run):
    """A new review cannot adopt a self-consistent but unproven writer receipt."""
    from backend.db.repositories.agent_runtime_repository import agent_runtime_repository

    try:
        ledger = _checked_journal(job, binding)
        origin = RequiredRewriteCandidateOrigin.model_validate_json(json.dumps(run["remediation"]["required_adherence_origin"]))
        entry = next(item for item in ledger.entries if item.origin.request_digest == origin.request_digest)
        if (
            entry.phase != "produced" or entry.agent_run_id != origin.agent_run_id
            or origin != RequiredRewriteCandidateOrigin(**entry.origin.model_dump(), agent_run_id=entry.agent_run_id)
            or str(run["_id"]) != entry.origin.request.source_run_id
            or run["revision"] != entry.result_revision or run["revision"] != entry.origin.request.source_revision + 1
            or run["remediation"].get("verification") is not None
            or run["remediation"].get("schema_version") != "prose_run_remediation.v2"
            or run["remediation"].get("latest_content_digest") != entry.result_digest
        ):
            raise ValueError("writer candidate identity changed")
        settled = rewrite_accounting(job, entry, ledger.authorization)
        agent = await agent_runtime_repository.get_run_owned(run_id=entry.agent_run_id, owner_id=binding.owner_id)
        authorization = agent.get("authorization") or {}
        if (
            agent.get("status") != "completed" or agent.get("termination", {}).get("reason_code") != "goal_satisfied"
            or agent.get("authorization_digest") != entry.readiness_digest or contract_digest(authorization) != entry.readiness_digest
            or authorization.get("allowed_tools") != [REQUIRED_REWRITE_TOOL.model_dump(mode="json")]
            or authorization.get("scope") != {"kind": REQUIRED_REWRITE_SCOPE, "object_id": entry.origin.request.source_run_id}
            or authorization.get("limits") != ledger.authorization.runtime_limits().model_dump(mode="json")
            or str(agent.get("novel_id")) != binding.novel_id or agent.get("start_request_id") != entry.start_request_id
            or agent.get("has_uncertain_attempts") is not False or agent.get("attempts") != []
            or agent.get("tokens_reserved") != 0 or agent.get("paid_attempts_reserved") != 0
        ):
            raise ValueError("writer Agent evidence changed")
        steps = await agent_runtime_repository.list_steps_owned(run_id=entry.agent_run_id, owner_id=binding.owner_id)
        if len(steps) != 2 or any(step.get("status") != "completed" for step in steps):
            raise ValueError("writer steps missing")
        invocation = steps[0].get("tool_invocation") or {}
        expected_scope = {"kind": REQUIRED_REWRITE_SCOPE, "object_id": entry.origin.request.source_run_id}
        if (
            steps[0].get("ordinal") != 0 or steps[1].get("ordinal") != 1
            or invocation.get("tool") != REQUIRED_REWRITE_TOOL.model_dump(mode="json")
            or invocation.get("scope") != expected_scope or invocation.get("arguments") != entry.origin.request.tool_arguments()
            or (steps[1].get("planner_decision") or {}).get("finish_code") != REQUIRED_REWRITE_FINISH
            or (steps[1].get("observation") or {}).get("code") != REQUIRED_REWRITE_FINISH
        ):
            raise ValueError("writer goal evidence changed")
        attempts = [row for step in steps for row in step.get("attempt_ledger", [])]
        if [row.get("kind") for row in attempts] != ["planner", "tool", "planner"] or any(row.get("state") != "settled" for row in attempts):
            raise ValueError("writer attempt projections incomplete")
        aggregate = {key: sum(item.model_dump()[key] for item in settled.values()) for key in RuntimeCallUsage.model_fields}
        if any(agent["usage"].get(key) != value for key, value in aggregate.items()):
            raise ValueError("writer aggregate accounting changed")
        for kind, source_kind in (("planner", "planner"), ("tool", "rewrite")):
            expected_usage = settled[source_kind].model_dump()
            rows = [row for row in attempts if row["kind"] == kind]
            for key, value in expected_usage.items():
                actual_values = [row.get("usage", {}).get(key) for row in rows]
                if any(not _integer(item) for item in actual_values) or sum(actual_values) != value:
                    raise ValueError("writer attempt accounting changed")
        _, result = await read_original_rewrite_result(
            job=job, binding=binding, entry=entry, agent_run_id=entry.agent_run_id,
            step=steps[0], writer_usage=settled["rewrite"],
        )
        if result.code != REQUIRED_REWRITE_RESULT:
            raise ValueError("writer did not produce a complete candidate")
    except (KeyError, IndexError, StopIteration, TypeError, ValueError):
        raise RequiredProseRewriteError("rewrite_handoff_evidence_invalid") from None


async def validate_incomplete_rewrite(
    job,
    *,
    binding,
    run,
    entry: RewriteEntry,
    continuation_request: RequiredProseRewriteRequest | None = None,
):
    """Re-prove the exact partial candidate retained by a later attempt.

    A blocked next rewrite does not create a new ProseRun revision. Recovery
    must therefore validate the preceding incomplete entry instead of trying
    to reinterpret that partial source as a produced complete rewrite.
    """

    from backend.services.generation.prose_remediation_runtime import (
        ResumableProseCandidateCheckpoint,
        _execution_plan,
    )
    from backend.services.generation.prose_scene_repair import (
        build_v2_scene_repair_plan,
        incomplete_scene_indexes,
    )
    from backend.services.novel.state_completion import (
        chapter_content_digest,
    )

    try:
        ledger = _checked_journal(job, binding)
        stored = [
            item
            for item in ledger.entries
            if item.origin.request_digest == entry.origin.request_digest
        ]
        marker = ResumableProseCandidateCheckpoint.model_validate(
            (run.get("completion") or {}).get("resumable_scene_repair")
        )
        remediation = run.get("remediation") or {}
        origin = RequiredRewriteCandidateOrigin.model_validate_json(
            json.dumps(remediation.get("required_adherence_origin"))
        )
        chapter = await chapter_repo.get_chapter_by_id(binding.chapter_id)
        outline = chapter.get("outline") or {}
        current_text = str(run.get("assembled_text") or "")
        source_failures = build_v2_scene_repair_plan(
            run=run,
            current_text=current_text,
            outline=outline,
            plan=_execution_plan(run),
            target_scene_indexes=marker.remaining_scene_indexes,
            max_source_failure_targets=1,
        ).source_failing_scene_indexes
        progress_failures = incomplete_scene_indexes(
            completion=run.get("completion") or {},
            scene_count=len(outline.get("scenes") or []),
        )
        if (
            len(stored) != 1
            or stored[0] != entry
            or entry.phase != "incomplete"
            or entry.agent_run_id is None
            or str(run.get("_id"))
            != entry.origin.request.source_run_id
            or run.get("status") != "incomplete"
            or (run.get("completion") or {}).get("status")
            != "incomplete"
            or (run.get("completion") or {}).get(
                "can_write_formal_prose"
            )
            is not False
            or int(run.get("revision") or 0) != entry.result_revision
            or entry.result_revision
            != entry.origin.request.source_revision + 1
            or chapter_content_digest(
                str(run.get("assembled_text") or "")
            )
            != entry.result_digest
            or remediation.get("schema_version")
            != "prose_run_remediation.v2"
            or remediation.get("verification") is not None
            or remediation.get("latest_content_digest")
            != entry.result_digest
            or origin
            != RequiredRewriteCandidateOrigin(
                **entry.origin.model_dump(),
                agent_run_id=entry.agent_run_id,
            )
            or marker.candidate_revision != entry.result_revision
            or marker.content_digest != entry.result_digest
            or marker.agent_run_id != entry.agent_run_id
            or marker.source_revision
            != entry.origin.request.source_revision
            or marker.source_content_digest
            != entry.origin.request.source_content_digest
            or marker.issue_categories
            != entry.origin.request.issue_categories
            or marker.target_scene_indexes
            != entry.origin.request.scene_indexes
            or source_failures != marker.remaining_scene_indexes
            or progress_failures != marker.remaining_scene_indexes
        ):
            raise ValueError("partial rewrite identity changed")
        if continuation_request is not None and (
            continuation_request.source_run_id
            != entry.origin.request.source_run_id
            or continuation_request.source_revision
            != entry.result_revision
            or continuation_request.source_content_digest
            != entry.result_digest
            or marker.remaining_scene_indexes
            != continuation_request.scene_indexes
            or marker.issue_categories
            != continuation_request.issue_categories
        ):
            raise ValueError("partial continuation changed")
        rewrite_accounting(job, entry, ledger.authorization)
        _require_candidate_attempt_identity(job, ledger, run)
        return marker
    except (KeyError, IndexError, StopIteration, TypeError, ValueError):
        raise RequiredProseRewriteError(
            "rewrite_partial_evidence_invalid"
        ) from None
