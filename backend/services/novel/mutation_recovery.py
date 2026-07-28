"""恢复 standalone MongoDB 下未完成的领域 mutation。"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from backend.db import collections
from backend.db.mongo import get_database
from backend.db.mutation import MutationEngine, MutationHandlerSpec, RecoveryScope
from backend.db.utils import get_utc_now, to_object_id
from backend.services.novel.chapter_service import ChapterService
from backend.services.novel.chapter_state_service import ChapterStateService
from backend.services.novel.agent_revision import AgentRevisionProposalService
from backend.services.novel.character_state_service import CharacterStateService
from backend.services.novel.plot_thread_service import PlotThreadService
from backend.services.novel.novel_service import NovelService
from backend.services.novel.reference_card_service import ReferenceCardService
from backend.services.novel.reference_card_curation import ReferenceCardCurationService
from backend.services.novel.volume_service import VolumeService
from backend.services.generation.prose_runs import ProseRunModule
from backend.services.interop.card_import_proposal_service import (
    CardImportProposalService,
)


MutationExecutor = Callable[[Any, Any], Awaitable[Any]]


def _executors() -> dict[tuple[str, int], MutationHandlerSpec[Any]]:
    # 延迟构建目录，避免服务模块导入期间形成循环依赖。
    callbacks: dict[tuple[str, int], MutationExecutor] = {
        ("accept_chapter_outline", 1): ChapterService._execute_accept_chapter_outline,
        ("accept_chapter_state", 1): ChapterStateService._execute_accept_chapter_state,
        ("accept_prose_run", 1): ProseRunModule._execute_accept,
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
        ("apply_card_import_proposal", 1): CardImportProposalService._execute_apply,
        ("apply_agent_revision_proposal", 1): AgentRevisionProposalService._execute_apply,
    }
    non_narrative_operations = {
        "update_novel_metadata",
        "update_reference_card_metadata",
        # This handler advances only after its stale check, inside the same
        # recoverable command, so a rejected old digest cannot bump revision.
        "apply_card_import_proposal",
    }
    return {
        key: MutationHandlerSpec(
            callback,
            advances_narrative_revision=key[0] not in non_narrative_operations,
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
            claim = command.get("proposal_claim") or {}
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
                "修订提案恢复失败，已隔离且不会继续写入；请重新运行 Agent"
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
