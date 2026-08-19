"""受批量 readiness 授权的正文 + 状态单一正式提交边界。"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from backend.db.mutation import MutationCommand, MutationRecorder, commit_mutation
from backend.db.repositories.generation_job_repository import generation_job_repo
from backend.services.generation.outline_adherence import is_material_deviation
from backend.services.generation.prose_runs import ProseRunModule, prose_run_module
from backend.services.novel.chapter_state_service import ChapterStateService
from backend.services.novel.derived_stats import derived_stats
from backend.services.novel.state_proposal import (
    SelectAllPolicy,
    state_proposal_module,
)


FINALIZATION_AUTHORIZATION_SCHEMA = "chapter_finalization_authorization.v1"
FINALIZATION_CHANGE_CLASSES = ("chapter_prose", "chapter_state")


class ChapterFinalizationDenied(ValueError):
    """候选、闸门或批量作业授权不允许正式提交。"""


class ChapterFinalizationAuthorization(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: str = Field(min_length=1)
    readiness_digest: str = Field(min_length=1)
    authorization_revision: int = Field(ge=1)


class ChapterFinalizationEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    outline_adherence: Mapping[str, Any]
    repair_cycles_used: int = Field(default=0, ge=0)


def _strict_int(
    value: Any,
    *,
    field: str,
    minimum: int = 0,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ChapterFinalizationDenied(f"{field} 不是有效的冻结整数")
    return value


@dataclass(frozen=True)
class ChapterFinalizationDeps:
    job_repo: Any = generation_job_repo
    prose_runs: Any = prose_run_module
    state_proposals: Any = state_proposal_module
    state_service: Any = ChapterStateService


def _serialize_subcommand(command: MutationCommand) -> dict[str, Any]:
    return {
        "operation": command.operation,
        "version": command.version,
        "payload": deepcopy(command.payload),
        "before_image": deepcopy(command.before_image),
        "child_ids": deepcopy(command.child_ids),
    }


class _SubMutationRecorder:
    """让既有深模块在一个 journal 内使用隔离的回执命名空间。"""

    def __init__(
        self,
        root: MutationRecorder,
        *,
        prefix: str,
        command: Mapping[str, Any],
    ) -> None:
        if not prefix or "." in prefix:
            raise ValueError("mutation receipt prefix must be non-empty and dot-free")
        self._root = root
        self._prefix = f"{prefix}__"
        self._command = deepcopy(dict(command))

    @property
    def journal(self) -> dict[str, Any]:
        root_receipts = self._root.journal.get("receipts") or {}
        receipts = {
            key[len(self._prefix):]: deepcopy(value)
            for key, value in root_receipts.items()
            if key.startswith(self._prefix)
        }
        if "narrative_revision" in root_receipts:
            receipts["narrative_revision"] = deepcopy(
                root_receipts["narrative_revision"]
            )
        return {
            **self._root.journal,
            "command": self._command,
            "receipts": receipts,
        }

    def child_id(self, key: str) -> str:
        return str((self._command.get("child_ids") or {})[key])

    def was_received(self, key: str) -> bool:
        return self._root.was_received(f"{self._prefix}{key}")

    async def advance_phase(self, phase: str) -> None:
        await self._root.advance_phase(phase)

    async def receipt(self, key: str, value: Any) -> None:
        await self._root.receipt(f"{self._prefix}{key}", value)


class ChapterFinalizationService:
    """验证所有候选闸门，并以一个可恢复命令提交正式章节。"""

    def __init__(self, deps: ChapterFinalizationDeps | None = None) -> None:
        self._deps = deps or ChapterFinalizationDeps()

    async def commit(
        self,
        *,
        owner_id: str,
        chapter_id: str,
        prose_run_id: str,
        prose_run_revision: int,
        state_proposal_id: str,
        state_acceptance_token: str,
        authorization: ChapterFinalizationAuthorization,
        evidence: ChapterFinalizationEvidence,
    ) -> dict[str, Any]:
        prose_command = await self._deps.prose_runs.prepare_accept_mutation(
            owner_id=owner_id,
            run_id=prose_run_id,
            chapter_id=chapter_id,
            expected_revision=prose_run_revision,
            accept_partial=False,
            partial_acknowledgement=False,
        )
        stored_authorization = await self._verify_authorization(
            authorization,
            novel_id=prose_command.novel_id,
            chapter_id=chapter_id,
        )
        state_payload, state_metadata, proposal_claim = (
            await self._deps.state_proposals.prepare_policy_decision(
                chapter_id=chapter_id,
                proposal_id=state_proposal_id,
                acceptance_token=state_acceptance_token,
                policy=SelectAllPolicy(),
            )
        )
        proposal_claim = {
            **proposal_claim,
            "finalized_prose": {
                "run_id": prose_run_id,
                "run_revision": int(prose_run_revision),
                "content_digest": prose_command.payload["text_digest"],
            },
        }
        state_command = await self._deps.state_service.prepare_chapter_state_mutation(
            chapter_id,
            state_payload,
            acceptance_metadata=state_metadata,
            proposal_claim=proposal_claim,
        )
        if state_command.novel_id != prose_command.novel_id:
            raise ChapterFinalizationDenied(
                "正文候选与状态候选不属于同一小说"
            )

        self._validate_gates(
            prose_command=prose_command,
            state_command=state_command,
            evidence=evidence,
            authorization=stored_authorization,
        )
        expected_revision = int(
            prose_command.payload["captured_narrative_revision"]
        )
        if int(proposal_claim["expected_narrative_revision"]) != expected_revision:
            raise ChapterFinalizationDenied(
                "正文候选与状态候选基于不同的小说版本"
            )

        idempotency_key = (
            f"finalize-chapter-generation:{prose_run_id}:"
            f"{prose_run_revision}:{state_proposal_id}"
        )
        deterministic_child_ids = {
            key: hashlib.sha256(
                f"{idempotency_key}:{key}".encode("utf-8")
            ).hexdigest()[:24]
            for key in state_command.child_ids
        }
        prose_command = replace(
            prose_command,
            payload={**prose_command.payload, "defer_derived_stats": True},
        )
        state_command = replace(
            state_command,
            child_ids=deterministic_child_ids,
        )
        command = MutationCommand(
            novel_id=prose_command.novel_id,
            idempotency_key=idempotency_key,
            operation="finalize_chapter_generation",
            version=1,
            expected_narrative_revision=expected_revision,
            payload={
                "chapter_id": chapter_id,
                "authorization": {
                    **authorization.model_dump(),
                    "snapshot": deepcopy(stored_authorization),
                },
                "evidence": evidence.model_dump(mode="json"),
                "prose_command": _serialize_subcommand(prose_command),
                "state_command": _serialize_subcommand(state_command),
            },
        )
        return await commit_mutation(
            command,
            self._execute_finalize,
            advances_narrative_revision=True,
        )

    async def _verify_authorization(
        self,
        supplied: ChapterFinalizationAuthorization,
        *,
        novel_id: str,
        chapter_id: str,
    ) -> dict[str, Any]:
        job = await self._deps.job_repo.get_job(supplied.job_id)
        if str(job.get("novel_id") or "") != str(novel_id):
            raise ChapterFinalizationDenied("批量作业不属于正文候选所在小说")
        if str(job.get("status") or "") != "running":
            raise ChapterFinalizationDenied("批量作业当前不允许正式提交")
        if str(job.get("current_chapter_id") or "") != str(chapter_id):
            raise ChapterFinalizationDenied("批量作业未授权当前章节")
        if job.get("has_uncertain_attempts"):
            raise ChapterFinalizationDenied("尚有未处置的付费调用，不能正式提交")
        readiness = dict(job.get("readiness") or {})
        if str(readiness.get("digest") or "") != supplied.readiness_digest:
            raise ChapterFinalizationDenied("批量 readiness 摘要已经变化")
        frozen = dict(
            (readiness.get("planning") or {}).get(
                "chapter_finalization_authorization"
            )
            or {}
        )
        if frozen.get("schema_version") != FINALIZATION_AUTHORIZATION_SCHEMA:
            raise ChapterFinalizationDenied("批量作业没有正式提交授权")
        frozen_revision = _strict_int(
            frozen.get("authorization_revision"),
            field="正式提交授权版本",
            minimum=1,
        )
        if frozen_revision != supplied.authorization_revision:
            raise ChapterFinalizationDenied("正式提交授权版本已经变化")
        if tuple(frozen.get("change_classes") or ()) != FINALIZATION_CHANGE_CLASSES:
            raise ChapterFinalizationDenied("正式提交授权范围不完整")
        return frozen

    @staticmethod
    def _validate_gates(
        *,
        prose_command: MutationCommand,
        state_command: MutationCommand,
        evidence: ChapterFinalizationEvidence,
        authorization: Mapping[str, Any],
    ) -> None:
        completion = dict(prose_command.payload.get("completion") or {})
        if (
            completion.get("can_write_formal_prose") is not True
            or str(completion.get("status") or "") != "complete"
        ):
            raise ChapterFinalizationDenied("正文候选未通过完整性闸门")
        adherence = dict(evidence.outline_adherence or {})
        if not adherence or is_material_deviation(adherence):
            raise ChapterFinalizationDenied("正文候选未通过细纲符合度闸门")
        if (
            str(adherence.get("source_prose_run_id") or "")
            != str(prose_command.payload["run_id"])
            or _strict_int(
                adherence.get("source_prose_run_revision"),
                field="细纲符合度正文版本",
            )
            != int(prose_command.payload["expected_revision"])
            or str(adherence.get("source_content_digest") or "")
            != str(prose_command.payload["text_digest"])
        ):
            raise ChapterFinalizationDenied("细纲符合度结果没有绑定当前正文候选")
        max_repairs = _strict_int(
            authorization.get("max_repair_cycles"),
            field="正文修复次数上限",
        )
        if evidence.repair_cycles_used > max_repairs:
            raise ChapterFinalizationDenied("正文修复次数超过已授权上限")

        state_payload = state_command.payload
        metadata = dict(state_payload.get("acceptance_metadata") or {})
        if list(metadata.get("consistency_issues") or []):
            raise ChapterFinalizationDenied("状态候选仍有一致性冲突")
        completion_evidence = dict(metadata.get("state_completion") or {})
        resolution = dict(completion_evidence.get("reference_resolution") or {})
        resolution_fields = (
            "proposed_character_update_count",
            "accepted_character_update_count",
            "dropped_character_update_count",
            "proposed_thread_update_count",
            "accepted_thread_update_count",
            "dropped_thread_update_count",
        )
        if any(field not in resolution for field in resolution_fields):
            raise ChapterFinalizationDenied("状态候选缺少完整的引用校验证据")
        counts = {
            field: _strict_int(
                resolution.get(field),
                field=f"状态引用证据 {field}",
            )
            for field in resolution_fields
        }
        if any(
            counts[field] > 0
            for field in (
                "dropped_character_update_count",
                "dropped_thread_update_count",
            )
        ):
            raise ChapterFinalizationDenied("状态候选仍含无效内部引用")
        proposed_characters = counts["proposed_character_update_count"]
        accepted_characters = counts["accepted_character_update_count"]
        if proposed_characters > 0 and accepted_characters == 0:
            raise ChapterFinalizationDenied("状态候选的角色更新全部丢失")
        if completion_evidence.get("source_prose_acceptance_state") != "ai_complete":
            raise ChapterFinalizationDenied("状态候选未绑定完整正文")
        if (
            str(completion_evidence.get("source_prose_run_id") or "")
            != str(prose_command.payload["run_id"])
            or _strict_int(
                completion_evidence.get("source_prose_run_revision"),
                field="状态候选正文版本",
            )
            != int(prose_command.payload["expected_revision"])
            or str(completion_evidence.get("source_content_digest") or "")
            != str(prose_command.payload["text_digest"])
        ):
            raise ChapterFinalizationDenied("状态候选没有绑定当前正文候选")

    @staticmethod
    async def _execute_finalize(session: Any, mutation: MutationRecorder) -> dict[str, Any]:
        payload = mutation.journal["command"]["payload"]
        prose = _SubMutationRecorder(
            mutation,
            prefix="prose",
            command=payload["prose_command"],
        )
        state = _SubMutationRecorder(
            mutation,
            prefix="state",
            command=payload["state_command"],
        )
        prose_result = await ProseRunModule._execute_accept(session, prose)
        state_result = await ChapterStateService._execute_accept_chapter_state(
            session, state
        )
        await mutation.advance_phase("derived_data")
        if mutation.was_received("final_derived_stats"):
            stats = mutation.journal["receipts"]["final_derived_stats"]
        else:
            stats = await derived_stats.refresh(
                str(mutation.journal["novel_id"]), session=session
            )
            await mutation.receipt("final_derived_stats", stats)
        return {
            "chapter_id": str(payload["chapter_id"]),
            "prose": prose_result,
            "state": state_result,
            "derived_stats": stats,
        }


chapter_finalization_service = ChapterFinalizationService()
