"""Deterministic, read-only audit for the book-completion contract.

The report is the single public read model for deciding whether a novel is a
completed book.  It deliberately does not call an LLM and does not treat a
GenerationJob terminal status as completion evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Literal

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.db import collections
from backend.db.errors import NotFoundError
from backend.db.mongo import get_database
from backend.db.narrative_revision import (
    NarrativeRevisionConflict,
    narrative_revision_store,
)
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.repositories.novel_repository import novel_repo
from backend.db.repositories.plot_thread_repository import ACTIVE_THREAD_STATUSES
from backend.db.repositories.volume_repository import volume_repo
from backend.db.utils import get_utc_now, to_object_id
from backend.services.generation.job_planner import order_book_chapters
from backend.services.generation.failure_diagnostics import (
    ActiveFailureEventState,
    ActiveFailureKind,
    resolve_active_failure_event,
)
from backend.services.novel.chapter_service import count_chapter_words
from backend.services.novel.emergent_reference_card_candidates import (
    REVIEWABLE_STATUSES,
)
from backend.services.novel.state_completion import (
    chapter_content_digest,
    state_completion_module,
)


BOOK_COMPLETION_AUDIT_SCHEMA_VERSION: Literal["book_completion_audit.v1"] = (
    "book_completion_audit.v1"
)
_HEX_64_PATTERN = r"^[0-9a-f]{64}$"
_WORLD_BASELINE_STATES = frozenset({
    "required",
    "blocked_pending_decisions",
    "current",
    "stale",
    "not_required_legacy",
})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BookCompletionIssue(_StrictModel):
    code: str
    category: Literal[
        "structure",
        "prose",
        "state",
        "reference",
        "thread",
        "word_count",
        "semantic",
        "runtime",
    ]
    level: Literal["blocking", "advisory"] = "blocking"
    volume_id: str | None = None
    chapter_id: str | None = None
    job_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class BookCompletionBlueprintSnapshot(_StrictModel):
    source: Literal["current_active_structure"] = "current_active_structure"
    current_structure_digest: str = Field(pattern=_HEX_64_PATTERN)
    frozen_job_id: str | None = None
    frozen_worklist_digest: str | None = Field(
        default=None,
        pattern=_HEX_64_PATTERN,
    )
    matches_frozen_worklist: bool | None = None
    world_baseline_state: Literal[
        "required",
        "blocked_pending_decisions",
        "current",
        "stale",
        "not_required_legacy",
        "invalid",
    ] = "invalid"
    world_baseline_confirmed_at: str | None = None


class BookCompletionSummary(_StrictModel):
    volume_count: int
    chapter_count: int
    complete_chapter_count: int = 0
    current_state_count: int = 0
    blocking_reference_candidate_count: int = 0
    unresolved_thread_count: int = 0
    blocking_issue_count: int
    advisory_issue_count: int


class BookCompletionChapterAudit(_StrictModel):
    chapter_id: str
    volume_id: str
    volume_order: int
    chapter_order: int
    outline_complete: bool
    prose_status: str
    state_status: str
    content_digest: str = Field(pattern=_HEX_64_PATTERN)
    actual_word_count: int
    target_word_count: int | None


class _BookCompletionReportProjection(_StrictModel):
    schema_version: Literal["book_completion_audit.v1"]
    novel_id: str
    narrative_revision: int = Field(ge=0)
    status: Literal["complete", "incomplete"]
    complete: bool
    read_only: Literal[True] = True
    blueprint: BookCompletionBlueprintSnapshot
    summary: BookCompletionSummary
    chapters: list[BookCompletionChapterAudit]
    issues: list[BookCompletionIssue]
    excluded_optional_subsystems: tuple[
        Literal["illustrations"],
        Literal["exports"],
        Literal["optional_agent_reports"],
    ] = ("illustrations", "exports", "optional_agent_reports")


class BookCompletionReport(_BookCompletionReportProjection):
    audit_digest: str = Field(pattern=_HEX_64_PATTERN)

    @classmethod
    def from_projection(
        cls,
        projection: Mapping[str, Any],
    ) -> "BookCompletionReport":
        canonical = _BookCompletionReportProjection.model_validate(projection)
        payload = canonical.model_dump(mode="json")
        return cls(**payload, audit_digest=_digest(payload))

    @model_validator(mode="after")
    def validate_completion_projection(self) -> "BookCompletionReport":
        blocking_issue_count = sum(
            issue.level == "blocking" for issue in self.issues
        )
        advisory_issue_count = sum(
            issue.level == "advisory" for issue in self.issues
        )
        if (
            self.summary.blocking_issue_count != blocking_issue_count
            or self.summary.advisory_issue_count != advisory_issue_count
        ):
            raise ValueError("book completion issue count projection diverged")
        if self.complete != (self.status == "complete"):
            raise ValueError("book completion status projection diverged")
        if self.complete != (self.summary.blocking_issue_count == 0):
            raise ValueError("book completion issue count projection diverged")
        projection = self.model_dump(
            mode="json",
            exclude={"audit_digest"},
        )
        if self.audit_digest != _digest(projection):
            raise ValueError("book completion audit digest does not match")
        return self


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _outline_is_complete(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    scenes = value.get("scenes")
    return bool(
        isinstance(scenes, list)
        and scenes
        and all(isinstance(scene, dict) for scene in scenes)
        and str(value.get("core_conflict") or "").strip()
        and str(value.get("ending_hook") or "").strip()
        and (_positive_int(value.get("target_word_count")) or 0) >= 100
    )


def _reference_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _frozen_worklist(job: dict[str, Any]) -> list[dict[str, Any]] | None:
    readiness = job.get("readiness")
    work = readiness.get("work") if isinstance(readiness, dict) else None
    raw_chapters = work.get("chapters") if isinstance(work, dict) else None
    if not isinstance(raw_chapters, list):
        return None
    frozen: list[dict[str, Any]] = []
    for item in raw_chapters:
        if not isinstance(item, dict):
            return None
        chapter_id = str(item.get("chapter_id") or "")
        volume_id = str(item.get("volume_id") or "")
        order_index = item.get("order_index")
        if (
            not chapter_id
            or not volume_id
            or isinstance(order_index, bool)
            or not isinstance(order_index, int)
            or order_index < 1
        ):
            return None
        frozen.append(
            {
                "chapter_id": chapter_id,
                "volume_id": volume_id,
                "order_index": order_index,
            }
        )
    return frozen


def _prose_status(
    chapter: dict[str, Any],
    *,
    content_digest: str,
    actual_word_count: int,
    target_word_count: int | None,
    expected_scene_count: int | None,
    source_run: Mapping[str, Any] | None,
) -> tuple[str, list[BookCompletionIssue]]:
    chapter_id = str(chapter.get("_id") or "")
    volume_id = str(chapter.get("volume_id") or "")
    content = str(chapter.get("content") or "")
    acceptance = chapter.get("prose_acceptance")
    state = (
        str(acceptance.get("state") or "")
        if isinstance(acceptance, dict)
        else "unknown_legacy"
    )
    issues: list[BookCompletionIssue] = []
    if not content.strip():
        issues.append(
            BookCompletionIssue(
                code="chapter_prose_missing",
                category="prose",
                volume_id=volume_id,
                chapter_id=chapter_id,
            )
        )
        return "missing", issues
    if state not in {"ai_complete", "manual_complete"}:
        issues.append(
            BookCompletionIssue(
                code=(
                    "chapter_prose_partial"
                    if state == "partial_manual_required"
                    else "chapter_prose_completion_unproven"
                ),
                category="prose",
                volume_id=volume_id,
                chapter_id=chapter_id,
                details={"prose_acceptance_state": state},
            )
        )
        return state or "unknown_legacy", issues

    assert isinstance(acceptance, dict)
    if str(acceptance.get("content_digest") or "") != content_digest:
        issues.append(
            BookCompletionIssue(
                code="chapter_prose_acceptance_stale",
                category="prose",
                volume_id=volume_id,
                chapter_id=chapter_id,
            )
        )
    if state == "ai_complete":
        completion_status = str(acceptance.get("completion_status") or "")
        finish_reason = str(acceptance.get("finish_reason") or "")
        if completion_status != "complete" or finish_reason != "stop":
            issues.append(
                BookCompletionIssue(
                    code="ai_prose_completion_gate_unproven",
                    category="prose",
                    volume_id=volume_id,
                    chapter_id=chapter_id,
                    details={
                        "completion_status": completion_status,
                        "finish_reason": finish_reason,
                    },
                )
            )
        source_run_id = str(acceptance.get("source_run_id") or "")
        completion = (
            source_run.get("completion")
            if isinstance(source_run, Mapping)
            else None
        )
        scene_count = (
            completion.get("scene_count")
            if isinstance(completion, Mapping)
            else None
        )
        completed_scene_count = (
            completion.get("completed_scene_count")
            if isinstance(completion, Mapping)
            else None
        )
        source_is_bound = bool(
            isinstance(source_run, Mapping)
            and str(source_run.get("_id") or "") == source_run_id
            and str(source_run.get("chapter_id") or "") == chapter_id
            and source_run.get("status") == "accepted"
            and source_run.get("acceptance_state") == "ai_complete"
            and source_run.get("accepted_text_digest") == content_digest
        )
        scene_gate_passed = bool(
            source_is_bound
            and isinstance(completion, Mapping)
            and completion.get("status") == "complete"
            and completion.get("can_write_formal_prose") is True
            and completion.get("finish_reason") == "stop"
            and not isinstance(scene_count, bool)
            and isinstance(scene_count, int)
            and scene_count >= 1
            and not isinstance(completed_scene_count, bool)
            and isinstance(completed_scene_count, int)
            and completed_scene_count == scene_count
            and expected_scene_count == scene_count
        )
        if not scene_gate_passed:
            issues.append(
                BookCompletionIssue(
                    code="ai_prose_scene_gate_unproven",
                    category="prose",
                    volume_id=volume_id,
                    chapter_id=chapter_id,
                    details={
                        "source_run_id": source_run_id or None,
                        "source_is_bound": source_is_bound,
                        "expected_scene_count": expected_scene_count,
                        "scene_count": scene_count,
                        "completed_scene_count": completed_scene_count,
                    },
                )
            )
    elif str(chapter.get("status") or "") != "completed":
        issues.append(
            BookCompletionIssue(
                code="manual_prose_completion_gate_unproven",
                category="prose",
                volume_id=volume_id,
                chapter_id=chapter_id,
                details={"chapter_status": chapter.get("status")},
            )
        )
    if (
        target_word_count is not None
        and actual_word_count < math.ceil(target_word_count * 0.8)
    ):
        issues.append(
            BookCompletionIssue(
                code="chapter_prose_below_word_gate",
                category="word_count",
                volume_id=volume_id,
                chapter_id=chapter_id,
                details={
                    "actual_word_count": actual_word_count,
                    "minimum_word_count": math.ceil(target_word_count * 0.8),
                },
            )
        )
    return state, issues


def _finish_report(
    *,
    novel_id: str,
    narrative_revision: int,
    blueprint: BookCompletionBlueprintSnapshot,
    summary: BookCompletionSummary,
    chapters: list[BookCompletionChapterAudit],
    issues: list[BookCompletionIssue],
) -> BookCompletionReport:
    complete = summary.blocking_issue_count == 0
    projection = {
        "schema_version": BOOK_COMPLETION_AUDIT_SCHEMA_VERSION,
        "novel_id": str(novel_id),
        "narrative_revision": narrative_revision,
        "status": "complete" if complete else "incomplete",
        "complete": complete,
        "read_only": True,
        "blueprint": blueprint.model_dump(mode="json"),
        "summary": summary.model_dump(mode="json"),
        "chapters": [chapter.model_dump(mode="json") for chapter in chapters],
        "issues": [issue.model_dump(mode="json") for issue in issues],
        "excluded_optional_subsystems": [
            "illustrations",
            "exports",
            "optional_agent_reports",
        ],
    }
    return BookCompletionReport.from_projection(projection)


@dataclass(frozen=True)
class BookCompletionPublicationFence:
    novel_id: str
    job_id: str
    narrative_revision: int
    fence_token: str


class BookCompletionAudit:
    """Inspect the canonical persisted book through one read-only interface."""

    @asynccontextmanager
    async def publication_fence(
        self,
        novel_id: str,
        job_id: str,
        *,
        expected_narrative_revision: int | None,
        fence_token: str,
    ) -> AsyncIterator[BookCompletionPublicationFence]:
        """Fence narrative writers until one audited Job transition is published."""

        current_revision = await narrative_revision_store.current_for_audit(
            novel_id
        )
        if (
            expected_narrative_revision is not None
            and current_revision != expected_narrative_revision
        ):
            raise NarrativeRevisionConflict(
                "Narrative revision changed before the book completion audit"
            )
        fence = BookCompletionPublicationFence(
            novel_id=str(novel_id),
            job_id=str(job_id),
            narrative_revision=current_revision,
            fence_token=str(fence_token),
        )
        await narrative_revision_store.acquire_book_completion_fence(
            novel_id,
            expected_revision=current_revision,
            fence_token=fence.fence_token,
            job_id=fence.job_id,
        )
        try:
            yield fence
        finally:
            await narrative_revision_store.release_book_completion_fence(
                novel_id,
                fence_token=fence.fence_token,
                job_id=fence.job_id,
            )

    async def inspect(
        self,
        novel_id: str,
        *,
        job_id: str | None = None,
        expected_narrative_revision: int | None = None,
        publication_fence_token: str | None = None,
    ) -> BookCompletionReport:
        await novel_repo.get_novel_by_id(novel_id)
        narrative_revision = await narrative_revision_store.current_for_audit(
            novel_id,
            allowed_book_completion_token=publication_fence_token,
        )
        if (
            expected_narrative_revision is not None
            and narrative_revision != expected_narrative_revision
        ):
            raise NarrativeRevisionConflict(
                "Narrative revision changed before the book completion audit"
            )
        volumes = await volume_repo.get_volumes_by_novel(novel_id)
        volume_order = {
            str(volume.get("_id") or ""): int(
                volume.get("order_index") or 0
            )
            for volume in volumes
        }
        chapters = await chapter_repo.get_chapters_by_novel(
            novel_id,
            include_content=True,
        )
        chapters = order_book_chapters(chapters, volume_order)
        completions = await state_completion_module.inspect_many(chapters)
        database = get_database()
        novel_object_id = to_object_id(novel_id)
        active_character_ids = {
            str(item["_id"])
            for item in await database[collections.CHARACTERS].find(
                {"novel_id": novel_object_id, "is_deleted": False},
                projection={"_id": 1},
            ).to_list(length=None)
        }
        active_worldbook_ids = {
            str(item["_id"])
            for item in await database[collections.WORLDBOOK].find(
                {"novel_id": novel_object_id, "is_deleted": False},
                projection={"_id": 1},
            ).to_list(length=None)
        }
        all_thread_ids = {
            str(item["_id"])
            for item in await database[collections.PLOT_THREADS].find(
                {"novel_id": novel_object_id, "is_deleted": False},
                projection={"_id": 1},
            ).to_list(length=None)
        }
        chapters_by_volume: dict[str, list[dict[str, Any]]] = {
            volume_id: [] for volume_id in volume_order
        }
        orphan_chapters: list[dict[str, Any]] = []
        for chapter in chapters:
            volume_id = str(chapter.get("volume_id") or "")
            if volume_id not in chapters_by_volume:
                orphan_chapters.append(chapter)
            else:
                chapters_by_volume[volume_id].append(chapter)

        structure = [
            {
                "volume_id": str(volume.get("_id") or ""),
                "order_index": int(volume.get("order_index") or 0),
                "chapters": [
                    {
                        "chapter_id": str(chapter.get("_id") or ""),
                        "order_index": int(
                            chapter.get("order_index") or 0
                        ),
                    }
                    for chapter in chapters_by_volume.get(
                        str(volume.get("_id") or ""),
                        [],
                    )
                ],
            }
            for volume in volumes
        ]
        issues: list[BookCompletionIssue] = []
        # A confirmed world baseline is part of a modern book blueprint.  Old
        # books without a baseline requirement remain explicitly compatible,
        # but a required/stale/decision-blocked baseline cannot certify a book.
        from backend.services.novel.world_baseline import WorldBaselineService

        world_baseline = await WorldBaselineService.inspect(novel_id)
        raw_world_baseline_state = world_baseline.get("state")
        world_baseline_state = (
            raw_world_baseline_state
            if isinstance(raw_world_baseline_state, str)
            and raw_world_baseline_state in _WORLD_BASELINE_STATES
            else "invalid"
        )
        raw_stale_reasons = world_baseline.get("stale_reasons")
        world_baseline_stale_reasons = (
            [
                str(reason)
                for reason in raw_stale_reasons
                if isinstance(reason, str) and reason
            ]
            if isinstance(raw_stale_reasons, list)
            else []
        )
        if world_baseline_state == "invalid":
            world_baseline_stale_reasons.append("state_missing_or_invalid")
        if world_baseline_state not in {"current", "not_required_legacy"}:
            issues.append(
                BookCompletionIssue(
                    code="world_baseline_not_current",
                    category="reference",
                    details={
                        "state": world_baseline_state,
                        "stale_reasons": world_baseline_stale_reasons,
                    },
                )
            )
        if not volumes:
            issues.append(
                BookCompletionIssue(
                    code="book_has_no_volumes",
                    category="structure",
                )
            )
        actual_volume_orders = [
            int(volume.get("order_index") or 0) for volume in volumes
        ]
        expected_volume_orders = list(range(1, len(volumes) + 1))
        if actual_volume_orders != expected_volume_orders:
            issues.append(
                BookCompletionIssue(
                    code="volume_order_gap",
                    category="structure",
                    details={
                        "actual": actual_volume_orders,
                        "expected": expected_volume_orders,
                    },
                )
            )

        for volume in volumes:
            volume_id = str(volume.get("_id") or "")
            scoped = chapters_by_volume[volume_id]
            if not scoped:
                issues.append(
                    BookCompletionIssue(
                        code="volume_has_no_chapters",
                        category="structure",
                        volume_id=volume_id,
                    )
                )
                continue
            actual_orders = [
                int(chapter.get("order_index") or 0) for chapter in scoped
            ]
            expected_orders = list(range(1, len(scoped) + 1))
            if actual_orders != expected_orders:
                issues.append(
                    BookCompletionIssue(
                        code="chapter_order_gap",
                        category="structure",
                        volume_id=volume_id,
                        details={
                            "actual": actual_orders,
                            "expected": expected_orders,
                        },
                    )
                )
        for chapter in orphan_chapters:
            issues.append(
                BookCompletionIssue(
                    code="chapter_has_inactive_volume",
                    category="structure",
                    volume_id=str(chapter.get("volume_id") or ""),
                    chapter_id=str(chapter.get("_id") or ""),
                )
            )

        source_run_object_ids = {
            ObjectId(source_run_id)
            for chapter in chapters
            if isinstance(chapter.get("prose_acceptance"), dict)
            and chapter["prose_acceptance"].get("state") == "ai_complete"
            and (
                source_run_id := str(
                    chapter["prose_acceptance"].get("source_run_id") or ""
                )
            )
            and ObjectId.is_valid(source_run_id)
        }
        source_runs = (
            await database[collections.PROSE_RUNS].find(
                {
                    "_id": {"$in": list(source_run_object_ids)},
                    "novel_id": novel_object_id,
                    "is_deleted": False,
                },
                projection={
                    "_id": 1,
                    "chapter_id": 1,
                    "status": 1,
                    "acceptance_state": 1,
                    "accepted_text_digest": 1,
                    "completion": 1,
                },
            ).to_list(length=None)
            if source_run_object_ids
            else []
        )
        source_runs_by_id = {
            str(source_run["_id"]): source_run for source_run in source_runs
        }
        chapter_audits: list[BookCompletionChapterAudit] = []
        formal_prose_evidence: dict[str, dict[str, str]] = {}
        current_state_count = 0
        for chapter in chapters:
            chapter_id = str(chapter.get("_id") or "")
            volume_id = str(chapter.get("volume_id") or "")
            outline = chapter.get("outline")
            outline_complete = _outline_is_complete(outline)
            target_word_count = (
                _positive_int(outline.get("target_word_count"))
                if isinstance(outline, dict)
                else None
            )
            content = str(chapter.get("content") or "")
            content_digest = chapter_content_digest(content)
            acceptance = chapter.get("prose_acceptance")
            formal_prose_evidence[chapter_id] = {
                "content_digest": content_digest,
                "source_prose_run_id": (
                    str(acceptance.get("source_run_id") or "")
                    if isinstance(acceptance, Mapping)
                    else ""
                ),
            }
            actual_word_count = count_chapter_words(content)
            if not outline_complete:
                issues.append(
                    BookCompletionIssue(
                        code="chapter_outline_incomplete",
                        category="structure",
                        volume_id=volume_id,
                        chapter_id=chapter_id,
                    )
                )
            prose_status, prose_issues = _prose_status(
                chapter,
                content_digest=content_digest,
                actual_word_count=actual_word_count,
                target_word_count=target_word_count,
                expected_scene_count=(
                    len(outline.get("scenes") or [])
                    if isinstance(outline, dict)
                    and isinstance(outline.get("scenes"), list)
                    else None
                ),
                source_run=source_runs_by_id.get(
                    str(
                        (chapter.get("prose_acceptance") or {}).get(
                            "source_run_id"
                        )
                        or ""
                    )
                ),
            )
            issues.extend(prose_issues)
            stored_word_count = chapter.get("word_count")
            if (
                isinstance(stored_word_count, bool)
                or not isinstance(stored_word_count, int)
                or stored_word_count != actual_word_count
            ):
                issues.append(
                    BookCompletionIssue(
                        code="chapter_word_count_stale",
                        category="word_count",
                        volume_id=volume_id,
                        chapter_id=chapter_id,
                        details={
                            "stored_word_count": stored_word_count,
                            "actual_word_count": actual_word_count,
                        },
                    )
                )
            state_status = completions[chapter_id].status
            if state_status != "current":
                issues.append(
                    BookCompletionIssue(
                        code="chapter_state_not_current",
                        category="state",
                        volume_id=volume_id,
                        chapter_id=chapter_id,
                        details={"state_completion_status": state_status},
                    )
                )
            else:
                current_state_count += 1
            if isinstance(outline, dict):
                character_ids = _reference_ids(
                    outline.get("present_character_card_ids")
                ) + _reference_ids(
                    outline.get("mentioned_character_card_ids")
                )
                pov_id = str(outline.get("pov_character_card_id") or "")
                if pov_id:
                    character_ids.append(pov_id)
                worldbook_ids = _reference_ids(
                    outline.get("referenced_worldbook_card_ids")
                )
                thread_ids = _reference_ids(
                    outline.get("threads_planted")
                ) + _reference_ids(outline.get("threads_resolved"))
                missing_characters = sorted(
                    set(character_ids) - active_character_ids
                )
                missing_worldbook = sorted(
                    set(worldbook_ids) - active_worldbook_ids
                )
                missing_threads = sorted(set(thread_ids) - all_thread_ids)
                if missing_characters or missing_worldbook or missing_threads:
                    issues.append(
                        BookCompletionIssue(
                            code="chapter_reference_unresolved",
                            category="reference",
                            volume_id=volume_id,
                            chapter_id=chapter_id,
                            details={
                                "missing_character_card_ids": (
                                    missing_characters
                                ),
                                "missing_worldbook_card_ids": (
                                    missing_worldbook
                                ),
                                "missing_plot_thread_ids": missing_threads,
                            },
                        )
                    )
            chapter_audits.append(
                BookCompletionChapterAudit(
                    chapter_id=chapter_id,
                    volume_id=volume_id,
                    volume_order=volume_order.get(volume_id, 0),
                    chapter_order=int(chapter.get("order_index") or 0),
                    outline_complete=outline_complete,
                    prose_status=prose_status,
                    state_status=state_status,
                    content_digest=content_digest,
                    actual_word_count=actual_word_count,
                    target_word_count=target_word_count,
                )
            )

        blocking_candidates = await database[
            collections.EMERGENT_REFERENCE_CARD_CANDIDATES
        ].find(
            {
                "novel_id": novel_object_id,
                "is_deleted": False,
                "status": {"$in": sorted(REVIEWABLE_STATUSES)},
                "requires_review_before_next_chapter": True,
            },
            projection={"_id": 1},
        ).sort("_id", 1).to_list(length=None)
        if blocking_candidates:
            issues.append(
                BookCompletionIssue(
                    code="blocking_reference_candidates",
                    category="reference",
                    details={
                        "candidate_ids": [
                            str(candidate["_id"])
                            for candidate in blocking_candidates
                        ]
                    },
                )
            )

        now = get_utc_now()
        active_reference_proposals = await database[
            collections.REFERENCE_CARD_PROPOSALS
        ].find(
            {
                "novel_id": novel_object_id,
                "is_deleted": False,
                "$or": [
                    {
                        "status": {
                            "$in": [
                                "claimed",
                                "generating",
                                "generation_uncertain",
                            ]
                        }
                    },
                    {
                        "status": "proposed",
                        "expires_at": {"$gt": now},
                    },
                ],
            },
            projection={"_id": 1},
        ).sort("_id", 1).to_list(length=None)
        if active_reference_proposals:
            issues.append(
                BookCompletionIssue(
                    code="blocking_reference_card_proposal",
                    category="reference",
                    details={
                        "proposal_ids": [
                            str(proposal["_id"])
                            for proposal in active_reference_proposals
                        ]
                    },
                )
            )

        active_import_proposals = await database[
            collections.CARD_IMPORT_PROPOSALS
        ].find(
            {
                "novel_id": novel_object_id,
                "is_deleted": False,
                "status": {"$in": ["applying", "pending_review"]},
            },
            projection={"_id": 1},
        ).sort("_id", 1).to_list(length=None)
        if active_import_proposals:
            issues.append(
                BookCompletionIssue(
                    code="blocking_card_import_proposal",
                    category="reference",
                    details={
                        "proposal_ids": [
                            str(proposal["_id"])
                            for proposal in active_import_proposals
                        ]
                    },
                )
            )

        for receipt_collection in (
            collections.PROSE_REMEDIATION_RECEIPTS,
            collections.STATE_CANDIDATE_REPAIR_RECEIPTS,
            collections.REFERENCE_CARD_REPAIR_RECEIPTS,
        ):
            receipts = await database[receipt_collection].find(
                {
                    "novel_id": novel_object_id,
                    "is_deleted": False,
                    "state": {"$ne": "completed"},
                },
                projection={"_id": 1, "state": 1},
            ).sort("_id", 1).to_list(length=None)
            if receipts:
                issues.append(
                    BookCompletionIssue(
                        code="repair_receipt_unresolved",
                        category="runtime",
                        details={
                            "receipt_collection": receipt_collection,
                            "receipt_ids": [
                                str(receipt["_id"])
                                for receipt in receipts
                            ],
                            "states": sorted(
                                {
                                    str(receipt.get("state") or "unknown")
                                    for receipt in receipts
                                }
                            ),
                        },
                    )
                )

        unresolved_threads = await database[collections.PLOT_THREADS].find(
            {
                "novel_id": novel_object_id,
                "is_deleted": False,
                "status": {"$in": sorted(ACTIVE_THREAD_STATUSES)},
            },
            projection={"_id": 1, "name": 1, "status": 1},
        ).sort([("created_at", 1), ("_id", 1)]).to_list(length=None)
        for thread in unresolved_threads:
            issues.append(
                BookCompletionIssue(
                    code="plot_thread_unresolved",
                    category="thread",
                    details={
                        "thread_id": str(thread["_id"]),
                        "name": str(thread.get("name") or ""),
                        "status": str(thread.get("status") or ""),
                    },
                )
            )

        if job_id is not None:
            latest_job = await database[collections.GENERATION_JOBS].find_one(
                {
                    "_id": to_object_id(job_id),
                    "novel_id": novel_object_id,
                    "scope": "book",
                    "is_deleted": False,
                }
            )
            if latest_job is None:
                raise NotFoundError(
                    f"Book generation job {job_id} not found for novel {novel_id}"
                )
        else:
            latest_jobs = await database[collections.GENERATION_JOBS].find(
                {
                    "novel_id": novel_object_id,
                    "scope": "book",
                    "is_deleted": False,
                }
            ).sort([("created_at", -1), ("_id", -1)]).limit(1).to_list(
                length=1
            )
            latest_job = latest_jobs[0] if latest_jobs else None
        frozen_job_id: str | None = None
        frozen_worklist_digest: str | None = None
        matches_frozen_worklist: bool | None = None
        if latest_job is not None:
            frozen_job_id = str(latest_job.get("_id") or "")
            current_worklist = [
                {
                    "chapter_id": str(chapter.get("_id") or ""),
                    "volume_id": str(chapter.get("volume_id") or ""),
                    "order_index": int(chapter.get("order_index") or 0),
                }
                for chapter in chapters
            ]
            frozen = _frozen_worklist(latest_job)
            if frozen is None:
                matches_frozen_worklist = False
                issues.append(
                    BookCompletionIssue(
                        code="frozen_worklist_invalid",
                        category="structure",
                        job_id=frozen_job_id,
                    )
                )
            else:
                frozen_worklist_digest = _digest(frozen)
                matches_frozen_worklist = frozen == current_worklist
                if not matches_frozen_worklist:
                    issues.append(
                        BookCompletionIssue(
                            code="frozen_worklist_drift",
                            category="structure",
                            job_id=frozen_job_id,
                            details={
                                "frozen_chapter_ids": [
                                    item["chapter_id"] for item in frozen
                                ],
                                "current_chapter_ids": [
                                    item["chapter_id"]
                                    for item in current_worklist
                                ],
                            },
                        )
                    )

            progress = latest_job.get("progress") or []
            latest_reviews: dict[str, tuple[int, dict[str, Any]]] = {}
            latest_chapter_progress: dict[
                str, tuple[int, dict[str, Any]]
            ] = {}
            for index, entry in enumerate(progress):
                if not isinstance(entry, dict):
                    continue
                chapter_id = str(entry.get("chapter_id") or "")
                if not chapter_id:
                    continue
                latest_chapter_progress[chapter_id] = (index, entry)
                review = entry.get("outline_adherence")
                if (
                    isinstance(review, dict)
                    and review.get("verdict") in {"pass", "warn", "fail"}
                ):
                    latest_reviews[chapter_id] = (index, review)

            stale_semantics: list[dict[str, Any]] = []
            current_semantic_reviews: dict[
                str, tuple[int, dict[str, Any]]
            ] = {}
            for chapter_id, (index, review) in latest_reviews.items():
                current = formal_prose_evidence.get(chapter_id)
                review_digest = str(review.get("source_content_digest") or "")
                review_run_id = str(review.get("source_prose_run_id") or "")
                current_digest = str((current or {}).get("content_digest") or "")
                current_run_id = str(
                    (current or {}).get("source_prose_run_id") or ""
                )
                source_is_bound = bool(
                    current
                    and current_run_id
                    and review_run_id
                    and review_digest == current_digest
                    and review_run_id == current_run_id
                )
                if source_is_bound:
                    current_semantic_reviews[chapter_id] = (index, review)
                    continue
                stale_semantics.append({
                    "progress_index": index,
                    "chapter_id": chapter_id,
                    "source_prose_run_id": review_run_id or None,
                    "current_source_prose_run_id": current_run_id or None,
                    "source_content_digest": review_digest or None,
                    "current_content_digest": current_digest or None,
                })
            for semantic in sorted(
                stale_semantics,
                key=lambda item: item["progress_index"],
            ):
                issues.append(
                    BookCompletionIssue(
                        code="semantic_review_stale",
                        category="semantic",
                        chapter_id=semantic["chapter_id"] or None,
                        job_id=frozen_job_id,
                        details={
                            key: value
                            for key, value in semantic.items()
                            if key != "chapter_id"
                        },
                    )
                )

            unresolved_semantics = [
                {
                    "progress_index": index,
                    "chapter_id": chapter_id,
                }
                for chapter_id, (
                    index,
                    review,
                ) in current_semantic_reviews.items()
                if review.get("verdict") == "fail"
                and str(
                    latest_job.get("outline_deviation_policy") or ""
                )
                != "accept_and_continue"
            ]
            for semantic in sorted(
                unresolved_semantics,
                key=lambda item: item["progress_index"],
            ):
                issues.append(
                    BookCompletionIssue(
                        code="semantic_review_unresolved",
                        category="semantic",
                        chapter_id=semantic["chapter_id"] or None,
                        job_id=frozen_job_id,
                        details={
                            "progress_index": semantic["progress_index"]
                        },
                    )
                )

            accepted_progress_count = latest_job.get(
                "last_checkpoint_index",
                0,
            )
            if (
                isinstance(accepted_progress_count, bool)
                or not isinstance(accepted_progress_count, int)
                or accepted_progress_count < 0
            ):
                accepted_progress_count = 0
            for chapter_id, (
                progress_index,
                entry,
            ) in sorted(
                latest_chapter_progress.items(),
                key=lambda item: item[1][0],
            ):
                raw_conflicts = entry.get("consistency_issues")
                conflict_count = (
                    len(raw_conflicts)
                    if isinstance(raw_conflicts, list)
                    else _positive_int(
                        entry.get("consistency_issue_count")
                    )
                    or 0
                )
                if (
                    conflict_count > 0
                    and progress_index >= accepted_progress_count
                ):
                    issues.append(
                        BookCompletionIssue(
                            code="semantic_conflict_unresolved",
                            category="semantic",
                            chapter_id=chapter_id,
                            job_id=frozen_job_id,
                            details={
                                "progress_index": progress_index,
                                "conflict_count": conflict_count,
                            },
                        )
                    )

            attempt_slots = latest_job.get("attempt_slots") or []
            uncertain = bool(latest_job.get("has_uncertain_attempts")) or any(
                isinstance(slot, dict)
                and str(slot.get("state") or "")
                in {"claimed", "uncertain"}
                for slot in attempt_slots
            )
            uncertain = uncertain or latest_job.get(
                "state_dispatch_resolution"
            ) is not None
            if uncertain:
                issues.append(
                    BookCompletionIssue(
                        code="uncertain_provider_attempt",
                        category="runtime",
                        job_id=frozen_job_id,
                    )
                )
            pause_reason = str(latest_job.get("pause_reason") or "")
            failure = resolve_active_failure_event(latest_job)
            if failure.state in {
                ActiveFailureEventState.MISSING,
                ActiveFailureEventState.INVALID,
            }:
                issues.append(
                    BookCompletionIssue(
                        code=(
                            "current_failure_event_missing"
                            if failure.state == ActiveFailureEventState.MISSING
                            else "current_failure_event_invalid"
                        ),
                        category="runtime",
                        job_id=frozen_job_id,
                        details={
                            "pause_reason": pause_reason,
                            **(
                                {"event_id": failure.event_id}
                                if failure.event_id
                                else {}
                            ),
                        },
                    )
                )
            elif (
                failure.state == ActiveFailureEventState.RESOLVED
                and failure.event is not None
            ):
                issue_code = (
                    "generation_source_changed"
                    if failure.kind == ActiveFailureKind.SOURCE_CHANGED
                    else "reference_card_repair_exhausted"
                    if failure.kind == ActiveFailureKind.REPAIR_EXHAUSTED
                    else "generation_failure_active"
                )
                event_chapter_id = str(
                    failure.event.get("chapter_id") or ""
                )
                issues.append(
                    BookCompletionIssue(
                        code=issue_code,
                        category="runtime",
                        chapter_id=event_chapter_id or None,
                        job_id=frozen_job_id,
                        details={
                            "event_id": failure.event_id,
                            "pause_reason": pause_reason,
                            **(
                                {"event_code": failure.event.get("code")}
                                if failure.event.get("code") is not None
                                else {}
                            ),
                        },
                    )
                )
            elif pause_reason in {
                "source_changed",
                "reference_card_repair_exhausted",
            }:
                # These reasons are active failures and therefore must have
                # been resolved above. This branch is defensive against an
                # unknown future status projection.
                issues.append(
                    BookCompletionIssue(
                        code="current_failure_event_missing",
                        category="runtime",
                        job_id=frozen_job_id,
                        details={"pause_reason": pause_reason},
                    )
                )
            if (
                latest_job.get("candidate_pipeline_checkpoints")
                or latest_job.get("job_mutation_recovery") is not None
            ):
                issues.append(
                    BookCompletionIssue(
                        code="repair_checkpoint_unresolved",
                        category="runtime",
                        job_id=frozen_job_id,
                    )
                )
        blocking_chapter_ids = {
            issue.chapter_id
            for issue in issues
            if issue.level == "blocking" and issue.chapter_id is not None
        }
        complete_chapter_count = sum(
            chapter.chapter_id not in blocking_chapter_ids
            for chapter in chapter_audits
        )
        blocking = sum(issue.level == "blocking" for issue in issues)
        advisory = len(issues) - blocking
        current_narrative_revision = (
            await narrative_revision_store.current_for_audit(
                novel_id,
                allowed_book_completion_token=publication_fence_token,
            )
        )
        if current_narrative_revision != narrative_revision:
            raise NarrativeRevisionConflict(
                "Narrative revision changed during the book completion audit"
            )
        return _finish_report(
            novel_id=novel_id,
            narrative_revision=narrative_revision,
            blueprint=BookCompletionBlueprintSnapshot(
                current_structure_digest=_digest(structure),
                frozen_job_id=frozen_job_id,
                frozen_worklist_digest=frozen_worklist_digest,
                matches_frozen_worklist=matches_frozen_worklist,
                world_baseline_state=world_baseline_state,
                world_baseline_confirmed_at=(
                    str(world_baseline.get("confirmed_at"))
                    if world_baseline.get("confirmed_at") is not None
                    else None
                ),
            ),
            summary=BookCompletionSummary(
                volume_count=len(volumes),
                chapter_count=len(chapters),
                complete_chapter_count=complete_chapter_count,
                current_state_count=current_state_count,
                blocking_reference_candidate_count=len(
                    blocking_candidates
                ),
                unresolved_thread_count=len(unresolved_threads),
                blocking_issue_count=blocking,
                advisory_issue_count=advisory,
            ),
            chapters=chapter_audits,
            issues=issues,
        )


book_completion_audit = BookCompletionAudit()
