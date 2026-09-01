"""Opt-in candidate producer; the Agent never owns the independent review.

run() resumes the original identity. It cannot create a Job, choose a model,
expand a budget, run a Judge, write a chapter, or unlock a state proposal.
"""
from __future__ import annotations

import json
from typing import Literal
from pydantic import Field

from backend.db.required_adherence_journal import candidate_write_fences
from backend.db.required_prose_rewrite_journal import (
    RequiredProseRewriteError, _source, reviewed_initial_source_authority,
    rewrite_accounting, read_original_rewrite_result,
)
from backend.db.repositories.agent_runtime_repository import agent_runtime_repository
from backend.db.repositories.generation_job_repository import generation_job_repo
from backend.db.repositories.prose_run_repository import prose_run_repo
from backend.db.utils import get_utc_now
from backend.services.agent_runtime.contracts import (
    AgentReadinessRequest, AgentScope, CompletionDecision,
)
from backend.services.agent_runtime.runtime import AgentRuntime
from backend.services.generation.attempt_scope import JobAttemptScope
from backend.services.generation.independent_outline_review import OutlineReviewSnapshot
from backend.services.generation.prose_remediation_runtime import (
    FrozenStructuredCall, ProseRemediationPlanner, ProseRemediationToolApplication,
    ProseRemediationToolRegistry, _execution_plan, _validate_candidate_snapshot,
    read_prose_remediation_revision,
    ResumableProseCandidateCheckpoint,
)
from backend.services.generation.required_adherence_handoff import (
    AwaitingAdherenceReceipt, RequiredReviewCandidate,
)
from backend.services.generation.required_prose_rewrite_contracts import (
    RequiredProseRewritePlan, RequiredProseRewriteRequest, RequiredRewriteCandidateOrigin,
    REQUIRED_REWRITE_FINISH, REQUIRED_REWRITE_RESULT, REQUIRED_REWRITE_SCOPE, REQUIRED_REWRITE_TOOL,
    ClosedRewriteModel, contract_digest,
)
from backend.services.llm.context_builder import fetch_context_inputs, assemble_context
from backend.services.llm.generation_runtime import GenerationRuntime, create_generation_runtime
from backend.services.novel.state_completion import chapter_content_digest


class IncompleteRewriteReceipt(ClosedRewriteModel):
    schema_version: Literal["required_prose_rewrite_incomplete.v1"] = "required_prose_rewrite_incomplete.v1"
    phase: Literal["incomplete"] = "incomplete"
    source_run_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    candidate_revision: int = Field(ge=2)
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    rewrite_ordinal: int = Field(ge=1, le=2)
    remaining_scene_indexes: tuple[int, ...]
    can_write_formal_prose: Literal[False] = False
    can_generate_state: Literal[False] = False


async def _candidate(binding, entry, agent_run_id: str) -> RequiredReviewCandidate:
    run, chapter = await _source(binding, entry.origin.request)
    origin = RequiredRewriteCandidateOrigin.model_validate_json(json.dumps(
        (run.get("remediation") or {}).get("required_adherence_origin"),
    ))
    expected = RequiredRewriteCandidateOrigin(**entry.origin.model_dump(), agent_run_id=agent_run_id)
    if (
        origin != expected or run["revision"] != entry.origin.request.source_revision + 1
        or run.get("status") != "complete" or run.get("outline_revision") != entry.outline_revision
    ):
        raise RequiredProseRewriteError("rewrite_result_stale")
    text = _validate_candidate_snapshot(
        run=run, chapter=chapter, novel_id=binding.novel_id,
        expected_revision=entry.origin.request.source_revision + 1,
        allow_unverified_remediation=True, required_origin=entry.origin,
    )
    context = assemble_context(await fetch_context_inputs(binding.novel_id, binding.chapter_id))
    snapshot = OutlineReviewSnapshot.create(
        source_run_id=entry.origin.request.source_run_id, source_run_revision=int(run["revision"]),
        source_content_digest=chapter_content_digest(text), prose=text, outline=chapter["outline"],
        authorized_context=context.to_prompt_text(),
    )
    result = RequiredReviewCandidate(snapshot, _execution_plan(run), dict(run["completion"]))
    result.require_complete()
    return result


class _CandidateProducedPolicy:
    revision = "required-rewrite-candidate-produced-r1"

    def __init__(self, binding, entry):
        self.binding, self.entry = binding, entry

    async def evaluate(self, *, run, observations, proposal):
        satisfied = False
        try:
            latest = observations[-1] if observations else {}
            authorization = run["authorization"]
            if (
                proposal.get("kind") != "propose_finish" or proposal.get("finish_code") != REQUIRED_REWRITE_FINISH
                or len(observations) != 1 or latest.get("observation_kind") != REQUIRED_REWRITE_RESULT
                or authorization["scope"] != {"kind": REQUIRED_REWRITE_SCOPE, "object_id": self.entry.origin.request.source_run_id}
                or authorization["allowed_tools"] != [REQUIRED_REWRITE_TOOL.model_dump(mode="json")]
                or str(run["owner_id"]) != self.binding.owner_id or str(run["novel_id"]) != self.binding.novel_id
            ):
                raise ValueError("candidate-only goal not satisfied")
            candidate = await _candidate(self.binding, self.entry, str(run["_id"]))
            satisfied = (
                latest.get("candidate_revision") == candidate.snapshot.source_run_revision
                and latest.get("content_digest") == candidate.snapshot.source_content_digest
            )
        except (KeyError, TypeError, ValueError):
            satisfied = False
        return CompletionDecision(
            satisfied=satisfied,
            reason_code="candidate_awaiting_adherence" if satisfied else "candidate_not_produced",
            planner_view={"satisfied": satisfied, "can_write_formal_prose": False, "can_generate_state": False},
        )


class RequiredProseRewriteProducer:
    """One bounded logical rewrite and its durable, still-locked handoff."""

    def __init__(self, *, binding, plan: RequiredProseRewritePlan, config_supplier=None, adapter_factory=None):
        if (config_supplier is None) != (adapter_factory is None):
            raise ValueError("external generation dependencies must be supplied together")
        self.binding, self.plan = binding, plan
        self._config_supplier, self._adapter_factory = config_supplier, adapter_factory
        self._journal = generation_job_repo.required_rewrite_journal(binding, authorization=plan.authorization(), review_plan=plan.review)

    def _call(self, entry, kind):
        scope = JobAttemptScope(self.binding.job_id, self.binding.chapter_id, entry.step_id(kind), repo=generation_job_repo)
        runtime = (
            create_generation_runtime(attempt_scope=scope, max_provider_retries=0)
            if self._config_supplier is None else GenerationRuntime(
                config_supplier=self._config_supplier, adapter_factory=self._adapter_factory, attempt_scope=scope,
            )
        )
        return FrozenStructuredCall(runtime=runtime, plan=self.plan.planner if kind == "planner" else self.plan.rewrite)

    def _runtime(self, entry):
        planner_call, rewrite_call = self._call(entry, "planner"), self._call(entry, "rewrite")
        planner = ProseRemediationPlanner(planner_call, required_origin=entry.origin)
        application = ProseRemediationToolApplication(
            rewrite_call=rewrite_call, adherence_call=None, required_origin=entry.origin,
        )
        tools = ProseRemediationToolRegistry(application=application, rewrite_call=rewrite_call, adherence_call=None)

        async def validate_scope(*, owner_id, novel_id, scope):
            job, ledger = await self._journal.read()
            if (
                owner_id != self.binding.owner_id or novel_id != self.binding.novel_id
                or scope != AgentScope(kind=REQUIRED_REWRITE_SCOPE, object_id=entry.origin.request.source_run_id)
            ):
                raise RequiredProseRewriteError("rewrite_scope_invalid")
            source, chapter = await _source(self.binding, entry.origin.request)
            initial_authority = await reviewed_initial_source_authority(
                job,
                binding=self.binding,
                ledger=ledger,
                entry=entry,
                source=source,
            )
            _validate_candidate_snapshot(
                run=source, chapter=chapter, novel_id=novel_id,
                allow_unverified_remediation=True, required_origin=entry.origin,
                reviewed_initial_source=initial_authority,
            )

        return AgentRuntime(
            planner=planner, tools=tools, completion_policy=_CandidateProducedPolicy(self.binding, entry),
            revision_reader=read_prose_remediation_revision, scope_validator=validate_scope,
            repository=agent_runtime_repository, clock=get_utc_now,
        )

    async def run(self, request: RequiredProseRewriteRequest):
        """Start once or recover the same receipt; never retry an old run."""
        request = RequiredProseRewriteRequest.model_validate_json(request.model_dump_json())
        entry = await self._journal.begin(request)
        runtime = self._runtime(entry)
        if entry.readiness_id is None:
            readiness = await runtime.inspect_readiness(
                owner_id=self.binding.owner_id,
                request=AgentReadinessRequest(
                    novel_id=self.binding.novel_id,
                    goal="只完成一次冻结改稿并交给独立审查；不能复检或正式提交。 " + json.dumps(request.tool_arguments(), ensure_ascii=False),
                    scope=AgentScope(kind=REQUIRED_REWRITE_SCOPE, object_id=request.source_run_id),
                    allowed_tools=(REQUIRED_REWRITE_TOOL,), allowed_effects=("proposal_only",),
                    allowed_change_classes=("temporary_candidate",),
                    allowed_external_data_categories=("chapter_prose_candidate_metadata", "outline_adherence_evidence", "chapter_prose_candidate", "chapter_outline", "narrative_context"),
                    limits=self._journal.authorization.runtime_limits(),
                ),
            )
            entry = await self._journal.replace(entry, entry.model_copy(update={
                "readiness_id": readiness.readiness_id, "readiness_digest": readiness.digest,
            }))
            # The runtime's scope validator and completion policy close over
            # the durable entry.  Rebuild them after publishing readiness so
            # no pre-readiness snapshot can authorize or reject dispatch.
            runtime = self._runtime(entry)
        original = await agent_runtime_repository.find_run_by_start_request_id_owned(
            owner_id=self.binding.owner_id, start_request_id=entry.start_request_id,
        )
        if original is None:
            if entry.phase != "reserved":
                raise RequiredProseRewriteError("rewrite_original_agent_missing")
            view = await runtime.start(
                owner_id=self.binding.owner_id, readiness_id=entry.readiness_id,
                digest=entry.readiness_digest, start_request_id=entry.start_request_id,
            )
        else:
            if (
                original.get("authorization_digest") != entry.readiness_digest
                or contract_digest(original.get("authorization")) != entry.readiness_digest
            ):
                raise RequiredProseRewriteError("rewrite_original_authorization_mismatch")
            view = await runtime.resume(owner_id=self.binding.owner_id, run_id=str(original["_id"]))
        return await self._materialize(entry, view)

    async def open_review(self, request: RequiredProseRewriteRequest):
        """Read the saved handoff; never start/resume an Agent or a Provider."""
        job, ledger = await self._journal.read()
        entry = next((item for item in ledger.entries if item.origin.request == request), None)
        if entry is None or entry.phase != "produced" or entry.agent_run_id is None:
            raise RequiredProseRewriteError("rewrite_handoff_not_produced")
        rewrite_accounting(job, entry, ledger.authorization)
        candidate = await _candidate(self.binding, entry, entry.agent_run_id)
        journal = generation_job_repo.required_review_journal(self.binding, candidate=candidate, plan=self.plan.review)
        await journal.read()
        return journal

    async def _materialize(self, entry, view):
        job, _ = await self._journal.read()
        settled = rewrite_accounting(job, entry, self._journal.authorization)
        slots = [slot for slot in job["attempt_slots"] if slot["step_id"] in {entry.step_id("planner"), entry.step_id("rewrite")}]
        if (
            not slots or any(slot["state"] != "accounted" for slot in slots)
            or view.has_uncertain_attempts or view.authorization_digest != entry.readiness_digest
            or view.usage.paid_attempts != len(slots)
            or view.usage.total_tokens != sum(slot["charged_tokens"] for slot in slots)
            or view.usage.input_tokens != sum(item.input_tokens for item in settled.values())
            or view.usage.output_tokens != sum(item.output_tokens for item in settled.values())
        ):
            raise RequiredProseRewriteError("rewrite_accounting_incomplete")
        has_partial_receipt = any(
            step.status == "completed" and (step.observation or {}).get("status") == "ok"
            and (step.observation or {}).get("code") == "prose_candidate_checkpointed"
            for step in view.steps
        )
        if view.status in {"failed", "cancelled"} and not has_partial_receipt:
            await self._journal.replace(entry, entry.model_copy(update={"phase": "blocked", "agent_run_id": view.run_id}))
            raise RequiredProseRewriteError("rewrite_agent_not_completed")
        tool_steps = [step for step in view.steps if step.tool_invocation is not None]
        if len(tool_steps) != 1:
            raise RequiredProseRewriteError("rewrite_original_receipt_invalid")
        step = tool_steps[0]
        original_step = await agent_runtime_repository.get_step_owned(
            run_id=view.run_id, owner_id=self.binding.owner_id, step_id=step.step_id,
        )
        original_source, result = await read_original_rewrite_result(
            job=job, binding=self.binding, entry=entry, agent_run_id=view.run_id,
            step=original_step, writer_usage=settled["rewrite"],
        )
        if result.code == "prose_candidate_checkpointed":
            return await self._incomplete(entry, view, original_source)
        if view.status != "completed" or view.termination is None or view.termination.reason_code != "goal_satisfied":
            raise RequiredProseRewriteError("rewrite_agent_not_completed")
        candidate = await _candidate(self.binding, entry, view.run_id)
        receipt = AwaitingAdherenceReceipt(
            schema_version="prose_candidate_awaiting_adherence.v1",
            source_run_id=candidate.snapshot.source_run_id,
            source_run_revision=candidate.snapshot.source_run_revision,
            source_content_digest=candidate.snapshot.source_content_digest,
            previous_revision=entry.origin.request.source_revision,
            previous_content_digest=entry.origin.request.source_content_digest,
            rewrite_ordinal=entry.origin.rewrite_ordinal,
            review_contract_digest=self.plan.review.contract_digest, view_digest=candidate.snapshot.view_digest,
        )
        published = entry.model_copy(update={
            "phase": "produced", "result_revision": receipt.source_run_revision,
            "result_digest": receipt.source_content_digest, "agent_run_id": view.run_id,
        })
        async with candidate_write_fences(self.binding, run_id=receipt.source_run_id, run_revision=receipt.source_run_revision):
            fresh = await _candidate(self.binding, entry, view.run_id)
            if fresh.check_digest != candidate.check_digest:
                raise RequiredProseRewriteError("rewrite_result_stale")
            await self._journal.replace(entry, published)
        journal = generation_job_repo.required_review_journal(self.binding, candidate=candidate, plan=self.plan.review)
        return await journal.prepare(receipt)

    async def _incomplete(self, entry, view, run):
        """Keep ADR-0006 progress, without converting a failed Agent to success."""
        from backend.services.generation.prose_scene_repair import build_v2_scene_repair_plan

        current, chapter = await _source(self.binding, entry.origin.request)
        origin = RequiredRewriteCandidateOrigin.model_validate_json(json.dumps(
            current.get("remediation", {}).get("required_adherence_origin"),
        ))
        marker = ResumableProseCandidateCheckpoint.model_validate(current.get("completion", {}).get("resumable_scene_repair"))
        if (
            current != run or current.get("status") != "incomplete"
            or origin != RequiredRewriteCandidateOrigin(**entry.origin.model_dump(), agent_run_id=view.run_id)
            or current.get("revision") != entry.origin.request.source_revision + 1
            or marker.content_digest != chapter_content_digest(current.get("assembled_text"))
        ):
            raise RequiredProseRewriteError("rewrite_partial_evidence_invalid")
        text = _validate_candidate_snapshot(
            run=current, chapter=chapter, novel_id=self.binding.novel_id,
            expected_revision=marker.candidate_revision, expected_content_digest=marker.content_digest,
            allow_unverified_remediation=True, required_origin=entry.origin,
        )
        proof = build_v2_scene_repair_plan(
            run=current, current_text=text, outline=chapter["outline"], plan=_execution_plan(current),
            target_scene_indexes=marker.remaining_scene_indexes, max_source_failure_targets=1,
        )
        if proof.source_failing_scene_indexes != marker.remaining_scene_indexes:
            raise RequiredProseRewriteError("rewrite_partial_evidence_invalid")
        receipt = IncompleteRewriteReceipt(
            source_run_id=entry.origin.request.source_run_id, candidate_revision=marker.candidate_revision,
            candidate_digest=marker.content_digest, rewrite_ordinal=entry.origin.rewrite_ordinal,
            remaining_scene_indexes=marker.remaining_scene_indexes,
        )
        replacement = entry.model_copy(update={
            "phase": "incomplete", "result_revision": marker.candidate_revision,
            "result_digest": marker.content_digest, "agent_run_id": view.run_id,
        })
        async with candidate_write_fences(self.binding, run_id=receipt.source_run_id, run_revision=receipt.candidate_revision):
            fresh, _ = await _source(self.binding, entry.origin.request)
            fields = ("assembled_text", "completion", "remediation", "plan", "revision")
            if any(fresh.get(key) != current.get(key) for key in fields):
                raise RequiredProseRewriteError("rewrite_partial_evidence_invalid")
            await self._journal.replace(entry, replacement)
        return receipt
