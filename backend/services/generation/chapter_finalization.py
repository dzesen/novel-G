"""受批量 readiness 授权的正文 + 状态单一正式提交边界。"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from backend.db.mutation import MutationCommand, MutationRecorder, commit_mutation
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.generation_job_repository import generation_job_repo
from backend.services.generation.candidate_repair_contracts import (
    MAX_CHAPTER_CANDIDATE_REPAIR_CYCLES,
)
from backend.services.generation.outline_adherence import (
    OutlineAdherenceValidationError,
    validate_complete_outline_adherence,
)
from backend.services.generation.prose_runs import ProseRunModule, prose_run_module
from backend.services.generation.prose_completion_contract import (
    completion_allows_formal_write,
)
from backend.services.novel.chapter_state_service import ChapterStateService
from backend.services.novel.derived_stats import derived_stats
from backend.services.novel.state_proposal import (
    FactAccountingPolicy,
    StaleStatePreview,
    state_proposal_module,
)
from backend.services.novel.state_fact_accounting import (
    StateFactAccountingError,
    validate_state_fact_accounting,
)


FINALIZATION_AUTHORIZATION_SCHEMA = "chapter_finalization_authorization.v1"
FINALIZATION_CHANGE_CLASSES = ("chapter_prose", "chapter_state")
DEFAULT_FINALIZATION_REPAIR_CYCLES = 2
MAX_FINALIZATION_REPAIR_CYCLES = MAX_CHAPTER_CANDIDATE_REPAIR_CYCLES


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


def chapter_finalization_idempotency_key(
    *,
    prose_run_id: str,
    prose_run_revision: int,
    state_proposal_id: str,
) -> str:
    if not str(prose_run_id or "") or not str(state_proposal_id or ""):
        raise ValueError("chapter finalization identity is incomplete")
    if (
        isinstance(prose_run_revision, bool)
        or not isinstance(prose_run_revision, int)
        or prose_run_revision < 0
    ):
        raise ValueError("chapter finalization prose revision is invalid")
    return (
        f"finalize-chapter-generation:{prose_run_id}:"
        f"{prose_run_revision}:{state_proposal_id}"
    )


def _strict_int(
    value: Any,
    *,
    field: str,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise ChapterFinalizationDenied(f"{field} 不是有效的冻结整数")
    return value


def build_chapter_finalization_authorization(
    *,
    authorization_revision: int,
    max_repair_cycles: int = DEFAULT_FINALIZATION_REPAIR_CYCLES,
) -> dict[str, Any]:
    """Build the closed authority later consumed by the atomic finalizer."""
    if (
        isinstance(authorization_revision, bool)
        or not isinstance(authorization_revision, int)
        or authorization_revision < 1
    ):
        raise ValueError("authorization revision must be a positive integer")
    if (
        isinstance(max_repair_cycles, bool)
        or not isinstance(max_repair_cycles, int)
        or max_repair_cycles < 0
        or max_repair_cycles > MAX_FINALIZATION_REPAIR_CYCLES
    ):
        raise ValueError(
            "repair cycles must be an integer between 0 and "
            f"{MAX_FINALIZATION_REPAIR_CYCLES}"
        )
    return {
        "schema_version": FINALIZATION_AUTHORIZATION_SCHEMA,
        "change_classes": list(FINALIZATION_CHANGE_CLASSES),
        "authorization_revision": authorization_revision,
        "max_repair_cycles": max_repair_cycles,
    }


def parse_chapter_finalization_authorization(
    value: Any,
) -> dict[str, Any]:
    """Revalidate one persisted finalization authority without coercion."""

    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "change_classes",
        "authorization_revision",
        "max_repair_cycles",
    }:
        raise ChapterFinalizationDenied("批量作业没有有效的正式提交授权")
    if value.get("schema_version") != FINALIZATION_AUTHORIZATION_SCHEMA:
        raise ChapterFinalizationDenied("批量作业没有正式提交授权")
    change_classes = value.get("change_classes")
    if (
        not isinstance(change_classes, list)
        or change_classes != list(FINALIZATION_CHANGE_CLASSES)
    ):
        raise ChapterFinalizationDenied("正式提交授权范围不完整")
    authorization_revision = _strict_int(
        value.get("authorization_revision"),
        field="正式提交授权版本",
        minimum=1,
    )
    max_repair_cycles = _strict_int(
        value.get("max_repair_cycles"),
        field="正文修复次数上限",
        maximum=MAX_FINALIZATION_REPAIR_CYCLES,
    )
    return build_chapter_finalization_authorization(
        authorization_revision=authorization_revision,
        max_repair_cycles=max_repair_cycles,
    )


@dataclass(frozen=True)
class ChapterFinalizationDeps:
    job_repo: Any = generation_job_repo
    chapter_repo: Any = chapter_repo
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
        try:
            candidate_text = (
                await self._deps.prose_runs.load_candidate_text_for_validation(
                    owner_id=owner_id,
                    run_id=prose_run_id,
                    chapter_id=chapter_id,
                    expected_revision=prose_run_revision,
                    expected_digest=str(prose_command.payload["text_digest"]),
                )
            )
        except ValueError as exc:
            raise ChapterFinalizationDenied(str(exc)) from exc
        stored_authorization = await self._verify_authorization(
            authorization,
            novel_id=prose_command.novel_id,
            chapter_id=chapter_id,
        )
        chapter = await self._deps.chapter_repo.get_chapter_by_id(chapter_id)
        if str(chapter.get("novel_id") or "") != str(prose_command.novel_id):
            raise ChapterFinalizationDenied(
                "正式提交章节不属于正文候选所在小说"
            )
        try:
            state_payload, state_metadata, proposal_claim = (
                await self._deps.state_proposals.prepare_policy_decision(
                    chapter_id=chapter_id,
                    proposal_id=state_proposal_id,
                    acceptance_token=state_acceptance_token,
                    policy=FactAccountingPolicy(),
                )
            )
        except StaleStatePreview as exc:
            raise ChapterFinalizationDenied(str(exc)) from exc
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

        adherence_metadata = self._validate_gates(
            prose_command=prose_command,
            state_command=state_command,
            evidence=evidence,
            authorization=stored_authorization,
            chapter=chapter,
            prose_text=candidate_text,
        )
        expected_revision = int(
            prose_command.payload["captured_narrative_revision"]
        )
        if int(proposal_claim["expected_narrative_revision"]) != expected_revision:
            raise ChapterFinalizationDenied(
                "正文候选与状态候选基于不同的小说版本"
            )

        idempotency_key = chapter_finalization_idempotency_key(
            prose_run_id=prose_run_id,
            prose_run_revision=prose_run_revision,
            state_proposal_id=state_proposal_id,
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
                "evidence": {
                    "outline_adherence": adherence_metadata,
                    "repair_cycles_used": evidence.repair_cycles_used,
                },
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
        planning = readiness.get("planning")
        if not isinstance(planning, Mapping):
            raise ChapterFinalizationDenied("批量 readiness 规划无效")
        frozen = parse_chapter_finalization_authorization(
            planning.get("chapter_finalization_authorization")
        )
        frozen_revision = frozen["authorization_revision"]
        if frozen_revision != supplied.authorization_revision:
            raise ChapterFinalizationDenied("正式提交授权版本已经变化")
        return frozen

    @staticmethod
    def _validate_gates(
        *,
        prose_command: MutationCommand,
        state_command: MutationCommand,
        evidence: ChapterFinalizationEvidence,
        authorization: Mapping[str, Any],
        chapter: Mapping[str, Any],
        prose_text: str,
    ) -> dict[str, Any]:
        completion = dict(prose_command.payload.get("completion") or {})
        if not completion_allows_formal_write(
            status=completion.get("status"),
            can_write_formal_prose=completion.get("can_write_formal_prose"),
            finish_reason=completion.get("finish_reason"),
        ):
            raise ChapterFinalizationDenied("正文候选未通过完整性闸门")
        adherence = dict(evidence.outline_adherence or {})
        outline = chapter.get("outline")
        if not isinstance(outline, Mapping):
            raise ChapterFinalizationDenied("正式提交章节缺少有效章纲")
        try:
            adherence_metadata = validate_complete_outline_adherence(
                adherence,
                outline=outline,
                prose=prose_text,
            )
        except OutlineAdherenceValidationError as exc:
            raise ChapterFinalizationDenied(str(exc)) from exc
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
            maximum=MAX_FINALIZATION_REPAIR_CYCLES,
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
        raw_fact_accounting = completion_evidence.get("fact_accounting")
        if not isinstance(raw_fact_accounting, Mapping):
            raise ChapterFinalizationDenied("状态候选缺少正式事实核算证据")
        try:
            fact_accounting = validate_state_fact_accounting(
                raw_fact_accounting
            )
        except StateFactAccountingError as exc:
            raise ChapterFinalizationDenied(str(exc)) from exc
        if not fact_accounting["gate_passed"]:
            raise ChapterFinalizationDenied(
                "状态候选仍有未核算正式事实或非法内部引用"
            )
        fact_source = fact_accounting["source_binding"]
        if (
            str(fact_source.get("chapter_id") or "")
            != str(chapter.get("_id") or "")
            or str(fact_source.get("source_prose_run_id") or "")
            != str(prose_command.payload["run_id"])
            or _strict_int(
                fact_source.get("source_prose_run_revision"),
                field="正式事实核算正文版本",
            )
            != int(prose_command.payload["expected_revision"])
            or str(fact_source.get("source_content_digest") or "")
            != str(prose_command.payload["text_digest"])
        ):
            raise ChapterFinalizationDenied(
                "正式事实核算没有绑定当前正文候选"
            )
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
        return adherence_metadata

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
