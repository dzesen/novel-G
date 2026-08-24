"""恢复 standalone MongoDB 下未完成的领域 mutation。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Awaitable, Callable

from backend.db import collections
from backend.db.mongo import get_database
from backend.db.narrative_revision import narrative_revision_store
from backend.db.mutation import (
    MutationCommand,
    MutationConflictError,
    MutationEngine,
    MutationHandlerSpec,
    RecoveryScope,
)
from backend.db.utils import get_utc_now, to_object_id
from backend.services.novel.chapter_service import ChapterService
from backend.services.novel.chapter_state_service import ChapterStateService
from backend.services.novel.agent_revision import AgentRevisionProposalService
from backend.services.novel.character_state_service import CharacterStateService
from backend.services.novel.plot_thread_service import PlotThreadService
from backend.services.novel.novel_service import NovelService
from backend.services.novel.reference_card_service import ReferenceCardService
from backend.services.novel.reference_card_curation import ReferenceCardCurationService
from backend.services.novel.emergent_reference_card_candidates import (
    EmergentReferenceCardCandidateModule,
)
from backend.services.novel.volume_service import VolumeService
from backend.services.generation.prose_runs import ProseRunModule
from backend.services.generation.chapter_finalization import (
    ChapterFinalizationService,
    parse_chapter_finalization_authorization,
)
from backend.services.generation.candidate_repair_contracts import (
    JobMutationRecoveryBindingV1,
)
from backend.services.interop.card_import_proposal_service import (
    CardImportProposalService,
)


_RECOVERABLE_FINALIZATION_PROPOSAL_CONFLICTS = frozenset({
    "State proposal disappeared before acceptance",
    "State proposal disappeared before applied publication",
    "State proposal expired before claim",
})


def _is_recoverable_finalization_journal(journal: dict[str, Any]) -> bool:
    status = str(journal.get("status") or "")
    if status in {"intent", "running", "failed", "completed"}:
        return True
    if status != "conflict" or str(journal.get("phase") or "") not in {
        "primary_writes",
        "timeline_writes",
        "derived_data",
    }:
        return False
    recovery_error = journal.get("last_recovery_error")
    return (
        isinstance(recovery_error, dict)
        and recovery_error.get("error_type") == "MutationConflictError"
        and recovery_error.get("message")
        in _RECOVERABLE_FINALIZATION_PROPOSAL_CONFLICTS
    )


MutationExecutor = Callable[[Any, Any], Awaitable[Any]]


def _executors() -> dict[tuple[str, int], MutationHandlerSpec[Any]]:
    # 延迟构建目录，避免服务模块导入期间形成循环依赖。
    from backend.services.generation.reference_card_auto_creation import (
        AUTO_CREATE_MUTATION_NAME,
        AUTO_CREATE_MUTATION_VERSION,
        auto_reference_card_creation_service,
    )
    from backend.services.generation.reference_card_auto_creation_revert import (
        AUTO_CARD_REVERT_MUTATION_NAME,
        AUTO_CARD_REVERT_MUTATION_VERSION,
        auto_reference_card_revert_service,
    )
    from backend.services.generation.reference_card_dependency_repair import (
        REFERENCE_CARD_REPAIR_MUTATION_NAME,
        REFERENCE_CARD_REPAIR_MUTATION_VERSION,
        reference_card_dependency_repair_service,
    )

    callbacks: dict[tuple[str, int], MutationExecutor] = {
        ("accept_chapter_outline", 1): ChapterService._execute_accept_chapter_outline,
        ("accept_chapter_state", 1): ChapterStateService._execute_accept_chapter_state,
        ("accept_prose_run", 1): ProseRunModule._execute_accept,
        ("finalize_chapter_generation", 1): (
            ChapterFinalizationService._execute_finalize
        ),
        ("create_chapter", 1): ChapterService._execute_create_chapter,
        ("create_chapter", 2): ChapterService._execute_create_chapter,
        ("update_chapter", 1): ChapterService._execute_update_chapter,
        ("update_chapter", 2): ChapterService._execute_update_chapter,
        ("update_chapter_outline", 1): ChapterService._execute_update_chapter_outline,
        ("update_character_current_state", 1): CharacterStateService._execute_update_current_state,
        ("legacy_update_character_current_state", 1): CharacterStateService._execute_legacy_update_current_state,
        ("update_permanent_fact", 1): CharacterStateService._execute_update_fact,
        ("legacy_update_permanent_fact", 1): CharacterStateService._execute_legacy_update_fact,
        ("delete_permanent_fact", 1): CharacterStateService._execute_delete_fact,
        ("create_plot_thread", 1): PlotThreadService._execute_create,
        ("update_plot_thread", 1): PlotThreadService._execute_update,
        ("soft_delete_plot_thread", 1): PlotThreadService._execute_soft_delete,
        ("soft_delete_chapter", 1): ChapterService._execute_soft_delete_chapter,
        ("soft_delete_chapter", 2): ChapterService._execute_soft_delete_chapter,
        ("restore_chapter", 1): ChapterService._execute_restore_chapter,
        ("restore_chapter", 2): ChapterService._execute_restore_chapter,
        ("hard_delete_chapter", 1): ChapterService._execute_hard_delete_chapter,
        ("soft_delete_volume", 1): VolumeService._execute_soft_delete_volume,
        ("soft_delete_volume", 2): VolumeService._execute_soft_delete_volume,
        ("restore_volume", 1): VolumeService._execute_restore_volume,
        ("restore_volume", 2): VolumeService._execute_restore_volume,
        ("hard_delete_volume", 1): VolumeService._execute_hard_delete_volume,
        ("accept_volume_outline", 1): VolumeService._execute_accept_volume_outline,
        ("accept_volume_outline", 2): VolumeService._execute_accept_volume_outline,
        ("accept_volume_outline", 3): VolumeService._execute_accept_volume_outline,
        ("create_volume", 1): VolumeService._execute_create_volume,
        ("create_volume", 2): VolumeService._execute_create_volume,
        ("update_volume", 1): VolumeService._execute_update_volume,
        ("update_novel_context", 1): NovelService._execute_update_novel_info,
        ("update_novel_metadata", 1): NovelService._execute_update_novel_info,
        ("soft_delete_novel", 1): NovelService._execute_novel_lifecycle,
        ("restore_novel", 1): NovelService._execute_novel_lifecycle,
        ("create_reference_card", 1): ReferenceCardService._execute_mutation,
        ("update_reference_card_context", 1): ReferenceCardService._execute_mutation,
        ("update_reference_card_metadata", 1): ReferenceCardService._execute_mutation,
        ("soft_delete_reference_card", 1): ReferenceCardService._execute_mutation,
        ("restore_reference_card", 1): ReferenceCardService._execute_mutation,
        ("hard_delete_reference_card", 1): ReferenceCardService._execute_mutation,
        ("apply_reference_card_plan", 1): ReferenceCardCurationService._execute_apply,
        ("apply_emergent_reference_card_candidates", 1): (
            EmergentReferenceCardCandidateModule._execute_apply
        ),
        ("apply_card_import_proposal", 1): CardImportProposalService._execute_apply,
        ("apply_agent_revision_proposal", 1): AgentRevisionProposalService._execute_apply,
        (AUTO_CREATE_MUTATION_NAME, AUTO_CREATE_MUTATION_VERSION): (
            auto_reference_card_creation_service._execute_apply
        ),
        (AUTO_CARD_REVERT_MUTATION_NAME, AUTO_CARD_REVERT_MUTATION_VERSION): (
            auto_reference_card_revert_service._execute_revert
        ),
        (
            REFERENCE_CARD_REPAIR_MUTATION_NAME,
            REFERENCE_CARD_REPAIR_MUTATION_VERSION,
        ): reference_card_dependency_repair_service._execute_apply,
    }
    non_narrative_operations = {
        "update_novel_metadata",
        "update_reference_card_metadata",
        # This handler advances only after its stale check, inside the same
        # recoverable command, so a rejected old digest cannot bump revision.
        "apply_card_import_proposal",
    }
    persistent_fence_operations = {
        AUTO_CREATE_MUTATION_NAME,
        AUTO_CARD_REVERT_MUTATION_NAME,
        REFERENCE_CARD_REPAIR_MUTATION_NAME,
    }
    persistent_fence_handlers = {
        ("accept_volume_outline", 3),
    }
    return {
        key: MutationHandlerSpec(
            callback,
            advances_narrative_revision=key[0] not in non_narrative_operations,
            persistent_narrative_fence=(
                key[0] in persistent_fence_operations
                or key in persistent_fence_handlers
            ),
        )
        for key, callback in callbacks.items()
    }


async def _sync_quarantined_proposals(
    report: dict[str, list[dict[str, Any]]]
) -> None:
    journals = get_database()[collections.MUTATION_JOURNALS]
    for item in report.get("quarantined") or []:
        journal = await journals.find_one({"_id": to_object_id(item["journal_id"])})
        command = ((journal or {}).get("command") or {}).get("payload") or {}
        operation = (journal or {}).get("operation")
        if operation == "apply_reference_card_plan":
            proposals = get_database()[collections.REFERENCE_CARD_PROPOSALS]
            proposal_id = command.get("proposal_id")
            expected_status = "claimed"
            quarantined_status = "quarantined"
        elif operation == "apply_card_import_proposal":
            proposals = get_database()[collections.CARD_IMPORT_PROPOSALS]
            proposal_id = command.get("proposal_id")
            expected_status = "applying"
            quarantined_status = "quarantined"
        elif operation == "apply_agent_revision_proposal":
            proposals = get_database()[collections.AGENT_REVISION_PROPOSALS]
            proposal_id = command.get("proposal_id")
            expected_status = "applying"
            # Agent revision proposals expose a closed author-facing state
            # machine. A poisoned recovery is terminal and non-applicable, so
            # surface it as stale while retaining the operator audit below.
            quarantined_status = "stale"
        else:
            proposals = get_database()[collections.STATE_PREVIEWS]
            claim = command.get("proposal_claim") or (
                ((command.get("state_command") or {}).get("payload") or {}).get(
                    "proposal_claim"
                )
                or {}
            )
            proposal_id = claim.get("proposal_id")
            expected_status = "claimed"
            quarantined_status = "quarantined"
        if not proposal_id:
            continue
        update: dict[str, Any] = {
            "status": quarantined_status,
            "quarantine_error": dict(item),
            "quarantined_at": get_utc_now(),
            "updated_at": get_utc_now(),
        }
        if operation == "apply_agent_revision_proposal":
            update["stale_reason"] = (
                "修订提案恢复失败，已隔离且不会继续写入；请重新运行生成角色"
            )
        await proposals.update_one(
            {
                "_id": to_object_id(proposal_id),
                "status": expected_status,
            },
            {"$set": update},
        )


async def recover_pending_mutations(novel_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """按更新时间恢复已知 mutation；未知操作保留 journal 并显式报告。"""
    report = await MutationEngine(_executors()).recover(
        RecoveryScope(novel_id=novel_id)
    )
    await _sync_quarantined_proposals(report)
    return report


async def recover_bound_mutation_revision(
    binding: JobMutationRecoveryBindingV1,
) -> int | None:
    """Recover one exact Job-owned mutation and return its frozen revision receipt.

    A missing journal means the mutation has not begun.  Once an intent exists,
    the persisted command is authoritative: callers never rebuild it from an
    expired proposal token or today's repository state.
    """

    try:
        frozen = JobMutationRecoveryBindingV1.model_validate(
            binding.model_dump(mode="python")
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise MutationConflictError("The Job mutation recovery binding is invalid") from exc
    normalized_novel_id = frozen.novel_id
    normalized_key = frozen.idempotency_key
    normalized_operation = frozen.operation
    collection = get_database()[collections.MUTATION_JOURNALS]
    query = {
        "novel_id": to_object_id(normalized_novel_id),
        "idempotency_key": normalized_key,
        "is_deleted": False,
    }
    journal = await collection.find_one(query)
    if journal is None:
        if normalized_operation != "accept_chapter_state":
            return None
        from backend.services.novel.state_proposal import (
            FactAccountingPolicy,
            SelectAllPolicy,
            state_proposal_module,
        )

        proposal = await state_proposal_module.recover_job_bound_result(frozen)
        if proposal is None:
            return None
        await state_proposal_module.run_auto(
            chapter_id=frozen.chapter_id,
            proposal=proposal,
            policy=(
                FactAccountingPolicy()
                if isinstance(proposal.get("fact_evidence"), Mapping)
                else SelectAllPolicy()
            ),
            job_mutation_binding=frozen,
        )
        journal = await collection.find_one(query)
        if journal is None:
            raise MutationConflictError(
                "The recovered state result did not create a mutation intent"
            )
    if str(journal.get("operation") or "") != normalized_operation:
        raise MutationConflictError(
            "The idempotency key is bound to a different mutation operation"
        )
    try:
        command = MutationCommand.from_journal(journal)
    except (KeyError, TypeError, ValueError) as exc:
        raise MutationConflictError("The persisted mutation command is invalid") from exc
    if (
        command.novel_id != normalized_novel_id
        or command.idempotency_key != normalized_key
        or command.operation != normalized_operation
        or type(command.expected_narrative_revision) is not int
    ):
        raise MutationConflictError("The persisted mutation identity diverged")
    payload = command.payload
    if (
        not isinstance(payload, dict)
        or str(payload.get("chapter_id") or "") != frozen.chapter_id
    ):
        raise MutationConflictError("The persisted mutation chapter diverged")
    authoritative_binding = frozen
    if normalized_operation == "accept_chapter_outline":
        expected_key = (
            f"candidate-job-outline:{frozen.job_id}:{frozen.chapter_id}"
        )
        if normalized_key != expected_key:
            raise MutationConflictError(
                "The persisted outline mutation is not bound to this Job"
            )
        try:
            stored_binding = JobMutationRecoveryBindingV1.model_validate(
                payload.get("job_mutation_binding")
            )
        except (TypeError, ValueError) as exc:
            raise MutationConflictError(
                "The persisted outline mutation Job binding is invalid"
            ) from exc
        stable_fields = {"expected_narrative_revision"}
        if stored_binding.model_dump(exclude=stable_fields) != frozen.model_dump(
            exclude=stable_fields
        ):
            raise MutationConflictError(
                "The persisted outline mutation belongs to another Job authorization"
            )
        authoritative_binding = stored_binding
    elif normalized_operation == "accept_chapter_state":
        proposal_claim = payload.get("proposal_claim")
        raw_binding = (
            proposal_claim.get("job_mutation_binding")
            if isinstance(proposal_claim, dict)
            else None
        )
        try:
            stored_binding = JobMutationRecoveryBindingV1.model_validate(
                raw_binding
            )
        except (TypeError, ValueError) as exc:
            raise MutationConflictError(
                "The persisted state mutation Job binding is invalid"
            ) from exc
        if stored_binding != frozen:
            raise MutationConflictError(
                "The persisted state mutation belongs to another Job"
            )
    elif normalized_operation == "finalize_chapter_generation":
        raw_authorization = payload.get("authorization")
        if not isinstance(raw_authorization, dict) or set(
            raw_authorization
        ) != {
            "job_id",
            "readiness_digest",
            "authorization_revision",
            "snapshot",
        }:
            raise MutationConflictError(
                "The persisted finalization authorization is invalid"
            )
        try:
            snapshot = parse_chapter_finalization_authorization(
                raw_authorization.get("snapshot")
            )
        except ValueError as exc:
            raise MutationConflictError(
                "The persisted finalization authorization is invalid"
            ) from exc
        authorization_revision = raw_authorization.get(
            "authorization_revision"
        )
        if (
            not isinstance(raw_authorization.get("job_id"), str)
            or not isinstance(raw_authorization.get("readiness_digest"), str)
            or type(authorization_revision) is not int
            or authorization_revision
            != snapshot["authorization_revision"]
        ):
            raise MutationConflictError(
                "The persisted finalization authorization is invalid"
            )
        supplied = {
            "job_id": raw_authorization.get("job_id"),
            "readiness_digest": raw_authorization.get("readiness_digest"),
            "authorization_revision": raw_authorization.get(
                "authorization_revision"
            ),
        }
        expected = {
            "job_id": frozen.job_id,
            "readiness_digest": frozen.readiness_digest,
            "authorization_revision": frozen.authorization_revision,
        }
        if supplied != expected:
            raise MutationConflictError(
                "The persisted finalization belongs to another Job authorization"
            )
    if (
        command.expected_narrative_revision
        != authoritative_binding.expected_narrative_revision
    ):
        raise MutationConflictError("The persisted mutation identity diverged")
    stored_digest = journal.get("command_digest")
    if stored_digest is not None and (
        not isinstance(stored_digest, str)
        or stored_digest != command.digest()
    ):
        raise MutationConflictError("The persisted mutation command digest diverged")

    status = str(journal.get("status") or "")
    if status != "completed":
        recoverable_status = status in {"intent", "running", "failed"}
        if normalized_operation == "finalize_chapter_generation":
            recoverable_status = _is_recoverable_finalization_journal(journal)
        if not recoverable_status:
            raise MutationConflictError(
                f"The persisted mutation cannot be recovered from status {status or 'missing'}"
            )
        spec = _executors().get((command.operation, command.version))
        if spec is None:
            raise MutationConflictError("The persisted mutation handler is unavailable")
        await MutationEngine({
            (command.operation, command.version): spec,
        }).execute(command)
        journal = await collection.find_one(query)
        if journal is None or str(journal.get("status") or "") != "completed":
            raise MutationConflictError("The persisted mutation did not complete")

    receipts = journal.get("receipts")
    revision_receipt = (
        receipts.get("narrative_revision")
        if isinstance(receipts, dict)
        else None
    )
    revision = (
        revision_receipt.get("revision")
        if isinstance(revision_receipt, dict)
        else None
    )
    if type(revision) is not int or revision < 1:
        raise MutationConflictError(
            "The persisted mutation narrative revision receipt is invalid"
        )
    if revision != command.expected_narrative_revision + 1:
        raise MutationConflictError(
            "The persisted mutation narrative revision receipt diverged"
        )
    return revision


async def find_job_bound_finalization_recovery_binding(
    *,
    novel_id: str,
    job_id: str,
    chapter_id: str,
    readiness_digest: str,
    authorization_revision: int,
    expected_narrative_revision: int,
) -> JobMutationRecoveryBindingV1 | None:
    """Find the one recoverable finalization journal owned by this exact Job.

    Candidate finalization persists its stable idempotency key inside the journal,
    while the GenerationJob keeps the terminal checkpoint prefix.  This lookup is
    deliberately closed over the Job/readiness/chapter/revision authority and is
    read-only.  The Job must first reacquire the global execution slot; its normal
    candidate runner then resumes the frozen journal through
    ``recover_bound_mutation_revision`` without dispatching another Provider call.
    """

    if (
        not isinstance(novel_id, str)
        or not novel_id
        or not isinstance(job_id, str)
        or not job_id
        or not isinstance(chapter_id, str)
        or not chapter_id
        or not isinstance(readiness_digest, str)
        or not readiness_digest
        or type(authorization_revision) is not int
        or authorization_revision < 1
        or type(expected_narrative_revision) is not int
        or expected_narrative_revision < 0
    ):
        raise MutationConflictError(
            "The Job finalization recovery authority is invalid"
        )

    collection = get_database()[collections.MUTATION_JOURNALS]
    matches = await collection.find(
        {
            "novel_id": to_object_id(novel_id),
            "operation": "finalize_chapter_generation",
            "is_deleted": False,
            "command.expected_narrative_revision": expected_narrative_revision,
            "command.payload.chapter_id": chapter_id,
            "command.payload.authorization.job_id": job_id,
            "command.payload.authorization.readiness_digest": readiness_digest,
            "command.payload.authorization.authorization_revision": (
                authorization_revision
            ),
            "command.payload.authorization.snapshot.authorization_revision": (
                authorization_revision
            ),
        },
        projection={
            "idempotency_key": 1,
            "status": 1,
            "phase": 1,
            "last_recovery_error": 1,
            "receipts.narrative_revision": 1,
        },
    ).limit(2).to_list(length=2)
    if not matches:
        return None
    if len(matches) != 1:
        raise MutationConflictError(
            "Multiple finalization journals match the same Job authority"
        )
    journal = matches[0]
    status = str(journal.get("status") or "")
    if not _is_recoverable_finalization_journal(journal):
        raise MutationConflictError(
            "The persisted finalization journal is not recoverable"
        )
    idempotency_key = journal.get("idempotency_key")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise MutationConflictError(
            "The persisted finalization idempotency key is invalid"
        )
    receipts = journal.get("receipts")
    revision_receipt = (
        receipts.get("narrative_revision")
        if isinstance(receipts, dict)
        else None
    )
    receipt_revision = (
        revision_receipt.get("revision")
        if isinstance(revision_receipt, dict)
        else None
    )
    if receipt_revision is not None and (
        type(receipt_revision) is not int
        or receipt_revision != expected_narrative_revision + 1
    ):
        raise MutationConflictError(
            "The persisted finalization narrative revision receipt diverged"
        )
    if status == "completed" and receipt_revision is None:
        raise MutationConflictError(
            "The completed finalization is missing its narrative revision receipt"
        )

    current_revision = await narrative_revision_store.current(novel_id)
    owned_revision = (
        receipt_revision
        if type(receipt_revision) is int
        else expected_narrative_revision
    )
    if current_revision != owned_revision:
        raise MutationConflictError(
            "The narrative changed outside the recoverable finalization journal"
        )

    return JobMutationRecoveryBindingV1(
        schema_version="job_mutation_recovery_binding.v1",
        novel_id=novel_id,
        job_id=job_id,
        chapter_id=chapter_id,
        readiness_digest=readiness_digest,
        authorization_revision=authorization_revision,
        expected_narrative_revision=expected_narrative_revision,
        operation="finalize_chapter_generation",
        idempotency_key=idempotency_key,
    )
