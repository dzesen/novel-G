"""One recoverable initial/repaired-candidate review loop for ADR-0008.

This interface intentionally stops before state generation, certificates, or
formal prose acceptance.  It is not a second chapter Job engine.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Literal

from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.generation_job_repository import generation_job_repo
from backend.db.repositories.prose_run_repository import prose_run_repo
from backend.services.generation.attempt_scope import JobAttemptScope
from backend.services.generation.chapter_generation_application import (
    AcceptanceAuthority,
    AcceptanceTiming,
    ChapterGenerationApplicationService,
    ProseGenerationCommand,
)
from backend.services.generation.independent_outline_review import (
    IndependentOutlineReviewer,
)
from backend.services.generation.chapter_repair_policy import (
    ChapterRepairPolicy,
    RepairBudgetLimitsV1,
    RepairBudgetExhausted,
    RepairComponent,
    RepairIssueV1,
)
from backend.services.generation.outline_adherence import (
    OUTLINE_ISSUE_CATEGORIES,
)
from backend.services.generation.prose_continuation import ProseContinuationPolicy
from backend.services.generation.prose_runs import prose_revision
from backend.services.generation.required_adherence_capacity import (
    RequiredAdherenceCapacity,
)
from backend.services.generation.required_adherence_handoff import (
    InitialAwaitingAdherenceReceipt,
    RequiredAdherenceCheckpoint,
    RequiredAdherenceHandoff,
    RequiredReviewCandidate,
)
from backend.services.generation.required_initial_prose_contracts import (
    RequiredInitialProseAuthorization,
    build_required_initial_prose_authorization,
    build_required_initial_prose_origin,
    required_initial_execution_plan,
    stable_digest,
)
from backend.services.generation.required_prose_rewrite import (
    IncompleteRewriteReceipt,
    RequiredProseRewriteError,
    RequiredProseRewriteProducer,
)
from backend.services.generation.required_prose_rewrite_contracts import (
    RequiredProseRewriteRequest,
    RequiredProseRewritePlan,
)
from backend.services.llm.generation_runtime import (
    GenerationPlan,
    GenerationRuntime,
)
from backend.services.novel.state_completion import chapter_content_digest


@dataclass(frozen=True)
class RequiredChapterReviewPlan:
    initial_generation: GenerationPlan
    rewrite: RequiredProseRewritePlan

    def __post_init__(self) -> None:
        if (
            not isinstance(self.initial_generation.provider_model, str)
            or not self.initial_generation.provider_model.strip()
            or self.initial_generation.provider_model.strip()
            != self.review.writer_model.strip()
        ):
            raise ValueError("initial_writer_model_mismatch")

    @property
    def review(self):
        return self.rewrite.review

    @property
    def review_capacity(self) -> RequiredAdherenceCapacity:
        return RequiredAdherenceCapacity.from_plan(self.review)

    def initial_authorization(
        self,
        outline: dict[str, Any],
    ) -> dict[str, Any]:
        return build_required_initial_prose_authorization(
            self.initial_generation,
            outline,
        ).model_dump(mode="json")


@dataclass(frozen=True)
class RequiredChapterReviewOutcome:
    phase: Literal["reviewed", "incomplete", "blocked"]
    candidate: RequiredReviewCandidate | None
    review: RequiredAdherenceCheckpoint | None
    repair_count: int
    reason_code: str | None = None
    convergence: tuple[Any, ...] = ()
    can_write_formal_prose: Literal[False] = False
    can_generate_state: Literal[False] = False


class RequiredChapterReviewLoop:
    """Generate/recover one initial source and route it through one reviewer."""

    def __init__(
        self,
        *,
        binding,
        plan: RequiredChapterReviewPlan,
        application: ChapterGenerationApplicationService | None = None,
        config_supplier=None,
        adapter_factory=None,
        review_records=None,
    ):
        if (config_supplier is None) != (adapter_factory is None):
            raise ValueError("external generation dependencies must be supplied together")
        self.binding = binding
        self.plan = plan
        self.application = application or ChapterGenerationApplicationService()
        self._config_supplier = config_supplier
        self._adapter_factory = adapter_factory
        from backend.services.generation.judge_review_records import judge_review_records

        self._review_records = review_records if review_records is not None else (judge_review_records if config_supplier is None else None)

    def _runtime(self, scope):
        if self._config_supplier is None:
            from backend.services.llm.generation_runtime import (
                create_generation_runtime,
            )

            return create_generation_runtime(
                attempt_scope=scope,
                max_provider_retries=0,
            )
        return GenerationRuntime(
            config_supplier=self._config_supplier,
            adapter_factory=self._adapter_factory,
            attempt_scope=scope,
        )

    @staticmethod
    def _hard_issues(review: RequiredAdherenceCheckpoint) -> tuple[RepairIssueV1, ...]:
        evidence = review.evidence or {}
        return tuple(
            RepairIssueV1(
                issue_signature=str(item["issue_signature"]),
                severity=str(item["severity"]),
            )
            for item in evidence.get("local_issues") or []
            if isinstance(item, dict)
            and item.get("severity") in {"blocker", "major"}
        )

    @staticmethod
    def _rewrite_request(
        candidate: RequiredReviewCandidate,
        review: RequiredAdherenceCheckpoint,
    ) -> RequiredProseRewriteRequest:
        evidence = review.evidence or {}
        outline = json.loads(candidate.snapshot.outline_json)
        scene_index_by_id = {
            str(scene.get("scene_id") or ""): index
            for index, scene in enumerate(outline.get("scenes") or [], start=1)
        }
        categories: set[str] = set()
        indexes: set[int] = set()
        has_unscoped = False
        allowed = {item.value for item in OUTLINE_ISSUE_CATEGORIES}
        for item in evidence.get("local_issues") or []:
            if not isinstance(item, dict) or item.get("severity") not in {
                "blocker",
                "major",
            }:
                continue
            category = str(item.get("category") or "")
            if category in allowed:
                categories.add(category)
            scene_id = str(item.get("scene_id") or "")
            if scene_id in scene_index_by_id:
                indexes.add(scene_index_by_id[scene_id])
            else:
                has_unscoped = True
        if has_unscoped:
            indexes.update(scene_index_by_id.values())
        if not categories or not indexes:
            raise ValueError("review_repair_target_invalid")
        return RequiredProseRewriteRequest(
            source_run_id=candidate.snapshot.source_run_id,
            source_revision=candidate.snapshot.source_run_revision,
            source_content_digest=candidate.snapshot.source_content_digest,
            issue_categories=tuple(sorted(categories)),
            scene_indexes=tuple(sorted(indexes)),
        )

    @staticmethod
    def _repair_limits() -> RepairBudgetLimitsV1:
        return RepairBudgetLimitsV1(
            provider_technical_retry=0,
            adherence_judge_retry=0,
            state_reextraction=0,
            local_prose_repair=0,
            scene_regeneration=2,
            outline_rollback=0,
        )

    @staticmethod
    def _incomplete_request(
        run: dict[str, Any],
        outline: dict[str, Any],
        *,
        issue_categories: tuple[str, ...] = ("scene_coverage",),
        scene_indexes: tuple[int, ...] | None = None,
    ) -> RequiredProseRewriteRequest:
        text = str(run.get("assembled_text") or "")
        if scene_indexes is None:
            progress = list((run.get("completion") or {}).get("scene_progress") or [])
            scene_indexes = tuple(
                sorted({
                    int(item["scene_index"]) + 1
                    for item in progress
                    if isinstance(item, dict)
                    and type(item.get("scene_index")) is int
                    and item.get("status") != "complete"
                })
            )
        if not scene_indexes:
            scene_indexes = tuple(range(1, len(outline.get("scenes") or []) + 1))
        return RequiredProseRewriteRequest(
            source_run_id=str(run["_id"]),
            source_revision=int(run["revision"]),
            source_content_digest=chapter_content_digest(text),
            issue_categories=issue_categories,
            scene_indexes=scene_indexes,
        )

    def _producer(self) -> RequiredProseRewriteProducer:
        return RequiredProseRewriteProducer(
            binding=self.binding,
            plan=self.plan.rewrite,
            config_supplier=self._config_supplier,
            adapter_factory=self._adapter_factory,
        )

    async def _drive_rewrite_to_review(
        self,
        request: RequiredProseRewriteRequest,
        outline: dict[str, Any],
    ) -> tuple[
        RequiredAdherenceCheckpoint | None,
        RequiredReviewCandidate | None,
        str | None,
    ]:
        """Resume one exact rewrite, continuing only its persisted partial scope."""

        from backend.db.required_prose_rewrite_journal import (
            _checked_journal,
        )

        producer = self._producer()
        current_request = request
        while True:
            produced = await producer.run(current_request)
            if not isinstance(produced, IncompleteRewriteReceipt):
                journal = await producer.open_review(current_request)
                review, candidate = await self._resume_review(journal)
                return review, candidate, None

            job = await generation_job_repo.get_job(self.binding.job_id)
            ledger = _checked_journal(job, self.binding)
            if len(ledger.entries) >= self._repair_limits().scene_regeneration:
                return None, None, "rewrite_candidate_incomplete"
            run = await prose_run_repo.get_run(
                produced.source_run_id,
                self.binding.owner_id,
            )
            current_request = self._incomplete_request(
                run,
                outline,
                issue_categories=current_request.issue_categories,
                scene_indexes=produced.remaining_scene_indexes,
            )

    async def _replay_repair_policy(
        self,
    ) -> tuple[ChapterRepairPolicy, int, str | None]:
        """Rebuild bounded repair state from the durable ordered journals."""

        from backend.db.required_adherence_journal import _ReviewLedger
        from backend.db.required_prose_rewrite_journal import (
            RewriteJournal,
            _checked_journal,
        )
        from backend.services.generation.required_adherence_handoff import (
            required_review_ordinal,
        )

        job = await generation_job_repo.get_job(self.binding.job_id)
        policy = ChapterRepairPolicy(self._repair_limits())
        raw_rewrites = job.get("required_prose_rewrite_journal")
        rewrites = (
            None
            if raw_rewrites is None
            else _checked_journal(job, self.binding)
        )
        raw_reviews = job.get("required_adherence_journal")
        if raw_reviews is None:
            if rewrites is not None:
                for _entry in rewrites.entries:
                    policy.authorize(RepairComponent.SCENE_REGENERATION)
            return policy, 0 if rewrites is None else len(rewrites.entries), None
        reviews = _ReviewLedger.model_validate_json(json.dumps(raw_reviews))
        if reviews.binding != self.binding:
            raise ValueError("review_repair_history_invalid")

        terminal_reason = None
        checkpoints = {
            required_review_ordinal(checkpoint.receipt): checkpoint
            for checkpoint in reviews.checkpoints
            if checkpoint.phase == "review_settled"
        }
        initial_checkpoint = checkpoints.get(0)
        if initial_checkpoint is not None:
            policy.observe_adherence(
                self._hard_issues(initial_checkpoint),
                prose_run_revision=(
                    initial_checkpoint.receipt.source_run_revision
                ),
                content_digest=(
                    initial_checkpoint.receipt.source_content_digest
                ),
            )
        if rewrites is not None:
            assert isinstance(rewrites, RewriteJournal)
        for entry in () if rewrites is None else rewrites.entries:
            ordinal = entry.origin.rewrite_ordinal
            charge = policy.authorize(RepairComponent.SCENE_REGENERATION)
            checkpoint = checkpoints.get(ordinal)
            if checkpoint is None:
                continue
            hard_issues = self._hard_issues(checkpoint)
            if policy.observation_count == 0:
                policy.observe_adherence(
                    hard_issues,
                    prose_run_revision=checkpoint.receipt.source_run_revision,
                    content_digest=checkpoint.receipt.source_content_digest,
                )
                continue
            convergence = policy.observe_adherence(
                hard_issues,
                prose_run_revision=checkpoint.receipt.source_run_revision,
                content_digest=checkpoint.receipt.source_content_digest,
                charge=charge,
            )
            if convergence.decision == "not_converged":
                terminal_reason = convergence.reason_codes[0]
                break
        return (
            policy,
            0 if rewrites is None else len(rewrites.entries),
            terminal_reason,
        )

    async def _resume_review(
        self,
        journal,
    ) -> tuple[RequiredAdherenceCheckpoint, RequiredReviewCandidate]:
        review_scope = JobAttemptScope(
            self.binding.job_id,
            self.binding.chapter_id,
            journal.attempt_step_id,
            repo=generation_job_repo,
        )
        recording = self._review_records.recording(
            owner_id=self.binding.owner_id, novel_id=self.binding.novel_id,
            chapter_id=self.binding.chapter_id, job_id=self.binding.job_id,
            step_id=journal.attempt_step_id,
        ) if self._review_records is not None else None
        reviewer = IndependentOutlineReviewer(self._runtime(review_scope), recording=recording)
        checkpoint = await RequiredAdherenceHandoff(
            journal,
            reviewer,
        ).resume(self.plan.review)
        _, candidate = await journal.read()
        return checkpoint, candidate

    async def _rewrite_failure_outcome(
        self,
        error: RequiredProseRewriteError,
        *,
        candidate: RequiredReviewCandidate | None = None,
        review: RequiredAdherenceCheckpoint | None = None,
    ) -> RequiredChapterReviewOutcome:
        """Report the durable repair charge from the same failed attempt."""

        policy, repair_count, replayed_reason = (
            await self._replay_repair_policy()
        )
        return RequiredChapterReviewOutcome(
            phase="blocked",
            candidate=candidate,
            review=review,
            repair_count=repair_count,
            reason_code=replayed_reason or str(error),
            convergence=policy.transitions,
        )

    async def run(self) -> RequiredChapterReviewOutcome:
        chapter = await chapter_repo.get_chapter_by_id(self.binding.chapter_id)
        outline = dict(chapter.get("outline") or {})
        authorization = RequiredInitialProseAuthorization.model_validate(
            self.plan.initial_authorization(outline)
        )
        execution = required_initial_execution_plan(
            self.plan.initial_generation,
            outline,
        )
        if authorization.execution_plan_digest != stable_digest(
            execution.to_dict()
        ):
            raise ValueError("initial_prose_execution_plan_changed")
        origin = build_required_initial_prose_origin(
            job_id=self.binding.job_id,
            readiness_digest=self.binding.readiness_digest,
            authorization_revision=self.binding.authorization_revision,
            narrative_revision=self.binding.narrative_revision,
            chapter_id=self.binding.chapter_id,
            outline_revision=prose_revision(outline),
            authorization=authorization,
        )
        initial_journal = generation_job_repo.required_initial_prose_journal(
            self.binding,
            origin=origin,
            authorization=authorization,
        )
        initial = await initial_journal.begin()
        run = await prose_run_repo.find_required_initial(
            owner_id=self.binding.owner_id,
            chapter_id=self.binding.chapter_id,
            job_id=self.binding.job_id,
            request_digest=origin.request_digest,
        )
        if initial.phase == "reserved":
            if run is None or run.get("status") not in {"complete", "incomplete"}:
                scope = JobAttemptScope(
                    self.binding.job_id,
                    self.binding.chapter_id,
                    initial_journal.attempt_step_id,
                    repo=generation_job_repo,
                )
                command = ProseGenerationCommand(
                    novel_id=self.binding.novel_id,
                    chapter_id=self.binding.chapter_id,
                    authority=AcceptanceAuthority.SYSTEM,
                    acceptance_timing=AcceptanceTiming.DEFERRED,
                    owner_id=self.binding.owner_id,
                    attempt_scope=scope,
                    generation_plan=self.plan.initial_generation,
                    generation_job_id=self.binding.job_id,
                    required_initial_origin=origin,
                    continuation_policy=ProseContinuationPolicy(
                        automatic_continuations_per_scene=0,
                    ),
                )
                result = await self.application.collect(command)
                status = result.completion.get("status")
                if status not in {"complete", "incomplete"} or (
                    result.completion.get("can_write_formal_prose") is not False
                ):
                    raise ValueError("initial_prose_result_invalid")
                run = await prose_run_repo.get_run(
                    str(result.completion["source_run_id"]),
                    self.binding.owner_id,
                )
            initial = await (
                initial_journal.produced(run)
                if run.get("status") == "complete"
                else initial_journal.incomplete(run)
            )
        if initial.phase == "blocked":
            return RequiredChapterReviewOutcome(
                phase="blocked",
                candidate=None,
                review=None,
                repair_count=0,
                reason_code=initial.failure_code or "initial_prose_blocked",
            )
        if initial.run_id is None:
            raise ValueError("initial_prose_result_invalid")
        # The same run may already contain a persisted rewrite on recovery.
        run = await prose_run_repo.get_run(
            str(initial.run_id),
            self.binding.owner_id,
        )
        source_job = await generation_job_repo.get_job(self.binding.job_id)
        review = None
        candidate = None
        drive_reason = None
        job = source_job
        raw_rewrites = job.get("required_prose_rewrite_journal")
        if raw_rewrites is None:
            if initial.phase == "produced":
                from backend.db.required_initial_prose_journal import (
                    validate_produced_initial_prose,
                )

                await validate_produced_initial_prose(
                    source_job,
                    binding=self.binding,
                    run=run,
                )
            else:
                from backend.db.required_initial_prose_journal import (
                    validate_incomplete_initial_prose,
                )

                await validate_incomplete_initial_prose(
                    source_job,
                    binding=self.binding,
                    run=run,
                )
        if raw_rewrites is not None:
            from backend.db.required_prose_rewrite_journal import (
                _checked_journal,
            )
            rewrites = _checked_journal(job, self.binding)
            latest = rewrites.entries[-1]
            if latest.phase == "blocked":
                if (
                    int(run.get("revision") or 0)
                    != latest.origin.request.source_revision
                    or chapter_content_digest(
                        str(run.get("assembled_text") or "")
                    )
                    != latest.origin.request.source_content_digest
                ):
                    raise ValueError("rewrite_blocked_source_stale")
                remediation = run.get("remediation") or {}
                previous = (
                    rewrites.entries[-2]
                    if len(rewrites.entries) > 1
                    else None
                )
                if previous is not None and previous.phase == "incomplete":
                    from backend.db.required_prose_rewrite_journal import (
                        validate_incomplete_rewrite,
                    )

                    await validate_incomplete_rewrite(
                        job,
                        binding=self.binding,
                        run=run,
                        entry=previous,
                        continuation_request=latest.origin.request,
                    )
                elif (
                    remediation.get("schema_version")
                    == "prose_run_remediation.v2"
                ):
                    from backend.db.required_prose_rewrite_journal import (
                        validate_produced_rewrite,
                    )

                    await validate_produced_rewrite(
                        job,
                        binding=self.binding,
                        run=run,
                    )
                elif initial.phase == "produced":
                    from backend.db.required_initial_prose_journal import (
                        validate_produced_initial_prose,
                    )

                    await validate_produced_initial_prose(
                        job,
                        binding=self.binding,
                        run=run,
                    )
                else:
                    from backend.db.required_initial_prose_journal import (
                        validate_incomplete_initial_prose,
                    )

                    await validate_incomplete_initial_prose(
                        job,
                        binding=self.binding,
                        run=run,
                    )
                policy, repair_count, prior_reason = (
                    await self._replay_repair_policy()
                )
                return RequiredChapterReviewOutcome(
                    phase="blocked",
                    candidate=None,
                    review=None,
                    repair_count=repair_count,
                    reason_code=prior_reason or "rewrite_agent_not_completed",
                    convergence=policy.transitions,
                )
            if latest.phase == "incomplete":
                from backend.db.required_prose_rewrite_journal import (
                    validate_incomplete_rewrite,
                )

                marker = await validate_incomplete_rewrite(
                    job,
                    binding=self.binding,
                    run=run,
                    entry=latest,
                )
                if len(rewrites.entries) >= (
                    self._repair_limits().scene_regeneration
                ):
                    policy, repair_count, prior_reason = (
                        await self._replay_repair_policy()
                    )
                    return RequiredChapterReviewOutcome(
                        phase="incomplete",
                        candidate=None,
                        review=None,
                        repair_count=repair_count,
                        reason_code=(
                            prior_reason or "rewrite_candidate_incomplete"
                        ),
                        convergence=policy.transitions,
                    )
                request = self._incomplete_request(
                    run,
                    outline,
                    issue_categories=latest.origin.request.issue_categories,
                    scene_indexes=marker.remaining_scene_indexes,
                )
            else:
                request = latest.origin.request
            try:
                review, candidate, drive_reason = (
                    await self._drive_rewrite_to_review(request, outline)
                )
            except RequiredProseRewriteError as error:
                return await self._rewrite_failure_outcome(error)
        elif initial.phase == "incomplete":
            request = self._incomplete_request(run, outline)
            try:
                review, candidate, drive_reason = (
                    await self._drive_rewrite_to_review(request, outline)
                )
            except RequiredProseRewriteError as error:
                return await self._rewrite_failure_outcome(error)
        else:
            # Reassemble the authorized initial context through the same seam.
            scope = JobAttemptScope(
                self.binding.job_id,
                self.binding.chapter_id,
                initial_journal.attempt_step_id,
                repo=generation_job_repo,
            )
            prepared = await self.application.prepare(ProseGenerationCommand(
                novel_id=self.binding.novel_id,
                chapter_id=self.binding.chapter_id,
                authority=AcceptanceAuthority.SYSTEM,
                acceptance_timing=AcceptanceTiming.DEFERRED,
                owner_id=self.binding.owner_id,
                attempt_scope=scope,
                generation_plan=self.plan.initial_generation,
                generation_job_id=self.binding.job_id,
                required_initial_origin=origin,
                continuation_policy=ProseContinuationPolicy(
                    automatic_continuations_per_scene=0,
                ),
            ))
            text = str(run.get("assembled_text") or "")
            digest = chapter_content_digest(text)
            candidate = RequiredReviewCandidate.create(
                source_run_id=str(run["_id"]),
                source_run_revision=int(run["revision"]),
                source_content_digest=digest,
                prose=text,
                outline=outline,
                authorized_context=prepared.context.to_prompt_text(),
                prose_plan=execution,
                completion=dict(run.get("completion") or {}),
            )
            snapshot = candidate.snapshot
            receipt = InitialAwaitingAdherenceReceipt(
                schema_version="initial_prose_candidate_awaiting_adherence.v1",
                source_run_id=snapshot.source_run_id,
                source_run_revision=snapshot.source_run_revision,
                source_content_digest=snapshot.source_content_digest,
                initial_request_digest=origin.request_digest,
                initial_contract_digest=origin.contract_digest,
                review_contract_digest=self.plan.review.contract_digest,
                view_digest=snapshot.view_digest,
            )
            review_journal = generation_job_repo.required_review_journal(
                self.binding,
                candidate=candidate,
                plan=self.plan.review,
            )
            try:
                await review_journal.read()
            except Exception as error:
                from backend.services.generation.required_adherence_handoff import (
                    RequiredAdherenceHandoffError,
                )

                if (
                    not isinstance(error, RequiredAdherenceHandoffError)
                    or str(error) != "review_checkpoint_missing"
                ):
                    raise
                await review_journal.prepare(receipt)
            review, candidate = await self._resume_review(review_journal)

        policy, repair_count, reason_code = await self._replay_repair_policy()
        if drive_reason is not None:
            return RequiredChapterReviewOutcome(
                phase="incomplete",
                candidate=None,
                review=None,
                repair_count=repair_count,
                reason_code=drive_reason,
                convergence=policy.transitions,
            )
        if review is None or candidate is None:
            raise ValueError("review_loop_result_invalid")
        if reason_code is not None:
            return RequiredChapterReviewOutcome(
                phase="blocked",
                candidate=candidate,
                review=review,
                repair_count=repair_count,
                reason_code=reason_code,
                convergence=policy.transitions,
            )
        while (
            review.phase == "review_settled"
            and (review.evidence or {}).get("decision") == "repair"
        ):
            try:
                request = self._rewrite_request(candidate, review)
                policy.authorize(RepairComponent.SCENE_REGENERATION)
                review, candidate, drive_reason = (
                    await self._drive_rewrite_to_review(request, outline)
                )
                policy, repair_count, reason_code = (
                    await self._replay_repair_policy()
                )
                if drive_reason is not None:
                    return RequiredChapterReviewOutcome(
                        phase="incomplete",
                        candidate=None,
                        review=None,
                        repair_count=repair_count,
                        reason_code=drive_reason,
                        convergence=policy.transitions,
                    )
                if review is None or candidate is None:
                    raise ValueError("review_loop_result_invalid")
                if reason_code is not None:
                    break
            except RepairBudgetExhausted as error:
                reason_code = error.code
                break
            except RequiredProseRewriteError as error:
                return await self._rewrite_failure_outcome(
                    error,
                    candidate=candidate,
                    review=review,
                )
        phase: Literal["reviewed", "blocked"] = (
            "reviewed"
            if review.phase == "review_settled"
            and (review.evidence or {}).get("decision") == "pass"
            and reason_code is None
            else "blocked"
        )
        return RequiredChapterReviewOutcome(
            phase=phase,
            candidate=candidate,
            review=review,
            repair_count=repair_count,
            reason_code=(
                None
                if phase == "reviewed"
                else reason_code
                or review.failure_code
                or str((review.evidence or {}).get("decision") or "review_blocked")
            ),
            convergence=policy.transitions,
        )
