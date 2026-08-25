"""Canonical chapter-state completion inspection.

Chapter summaries are author-facing prose and are not proof that state extraction
was accepted.  This module makes the accepted delta, its source-content digest,
and the prose acceptance state the single read model used by batch generation,
auditing, and the chapter UI.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

from backend.db import collections
from backend.db.mongo import get_database
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.utils import to_object_id
from backend.services.novel.state_fact_accounting import (
    StateFactAccountingError,
    validate_state_fact_accounting,
)
from backend.services.generation.chapter_completion_certificate import (
    ChapterCompletionPolicyError,
    verify_persisted_chapter_completion_certificate,
)


StateCompletionStatus = Literal[
    "missing",
    "current",
    "stale_after_content_edit",
    "degraded_all_character_updates_dropped",
    "degraded_partial_reference_drop",
    "degraded_fact_accounting",
    "unknown_legacy",
]

PROSE_ACCEPTANCE_ELIGIBLE_STATES = frozenset(
    {"ai_complete", "manual_complete"}
)


def chapter_content_digest(content: Any) -> str:
    """Digest only canonical prose bytes, not mutable chapter audit fields."""
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def prose_acceptance_state(chapter: dict[str, Any]) -> str:
    acceptance = chapter.get("prose_acceptance")
    if isinstance(acceptance, dict) and acceptance.get("state"):
        return str(acceptance["state"])
    return "unknown_legacy"


def prose_is_eligible_for_state(
    chapter: dict[str, Any],
    *,
    allow_unverified_legacy: bool = False,
) -> bool:
    content = str(chapter.get("content") or "")
    if not content.strip():
        return False
    acceptance = chapter.get("prose_acceptance")
    if not isinstance(acceptance, dict):
        return allow_unverified_legacy
    state = prose_acceptance_state(chapter)
    current_digest = chapter_content_digest(content)
    if state == "manual_complete":
        return bool(
            acceptance.get("content_origin") in {None, "manual"}
            and acceptance.get("content_digest") == current_digest
        )
    if state != "ai_complete":
        return False
    try:
        verify_persisted_chapter_completion_certificate(chapter)
        return True
    except ChapterCompletionPolicyError:
        # ``legacy_verified_v1`` is a read-only audit classification derived
        # from the source run, outline, word gate, current state, and legacy
        # journals. A label copied into chapter acceptance is not that proof
        # and must never unlock a new state Provider call.
        return bool(
            allow_unverified_legacy
            and acceptance.get("chapter_completion_certificate") is None
            and acceptance.get("content_digest") == current_digest
            and acceptance.get("completion_status") == "complete"
            and acceptance.get("finish_reason") == "stop"
            and str(acceptance.get("source_run_id") or "")
        )


@dataclass(frozen=True)
class StateCompletion:
    chapter_id: str
    status: StateCompletionStatus
    needs_backfill: bool
    requires_pause: bool
    prose_eligible: bool
    prose_acceptance_state: str
    source_content_digest: str | None = None
    current_content_digest: str | None = None
    completion_reason: str | None = None
    reference_resolution: dict[str, Any] | None = None
    fact_accounting: dict[str, Any] | None = None
    delta_revision: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapter_id": self.chapter_id,
            "status": self.status,
            "needs_backfill": self.needs_backfill,
            "requires_pause": self.requires_pause,
            "prose_eligible": self.prose_eligible,
            "prose_acceptance_state": self.prose_acceptance_state,
            "source_content_digest": self.source_content_digest,
            "current_content_digest": self.current_content_digest,
            "completion_reason": self.completion_reason,
            "reference_resolution": dict(self.reference_resolution or {}),
            "fact_accounting": dict(self.fact_accounting or {}),
            "delta_revision": self.delta_revision,
        }


class StateCompletionModule:
    @staticmethod
    def classify(
        chapter: dict[str, Any],
        delta: dict[str, Any] | None,
        *,
        allow_unverified_legacy: bool = False,
    ) -> StateCompletion:
        chapter_id = str(chapter.get("_id") or "")
        acceptance_state = prose_acceptance_state(chapter)
        eligible = prose_is_eligible_for_state(
            chapter,
            allow_unverified_legacy=allow_unverified_legacy,
        )
        current_digest = (
            chapter_content_digest(chapter.get("content"))
            if str(chapter.get("content") or "").strip()
            else None
        )
        if not eligible:
            acceptance = chapter.get("prose_acceptance")
            acceptance_digest = (
                acceptance.get("content_digest")
                if isinstance(acceptance, dict)
                else None
            )
            if (
                acceptance_state in PROSE_ACCEPTANCE_ELIGIBLE_STATES
                and acceptance_digest
                and acceptance_digest != current_digest
            ):
                return StateCompletion(
                    chapter_id=chapter_id,
                    status="stale_after_content_edit",
                    needs_backfill=True,
                    requires_pause=False,
                    prose_eligible=False,
                    prose_acceptance_state=acceptance_state,
                    source_content_digest=str(acceptance_digest),
                    current_content_digest=current_digest,
                )
            # A matching old delta is still not reusable after prose is explicitly
            # marked partial/manual-required. Eligibility is the outer gate; the
            # digest only proves which bytes the old delta described.
            return StateCompletion(
                chapter_id=chapter_id,
                status="missing",
                needs_backfill=True,
                requires_pause=False,
                prose_eligible=False,
                prose_acceptance_state=acceptance_state,
                current_content_digest=current_digest,
            )
        if not delta or delta.get("accepted_delta") is None:
            return StateCompletion(
                chapter_id=chapter_id,
                status="missing",
                needs_backfill=True,
                requires_pause=False,
                prose_eligible=eligible,
                prose_acceptance_state=acceptance_state,
                current_content_digest=current_digest,
            )

        evaluation = delta.get("evaluation") or {}
        evidence = evaluation.get("state_completion") or {}
        source_digest = evidence.get("source_content_digest")
        resolution = dict(evidence.get("reference_resolution") or {})
        raw_fact_accounting = evidence.get("fact_accounting")
        fact_accounting: dict[str, Any] | None = None
        fact_accounting_invalid = False
        if isinstance(raw_fact_accounting, dict):
            try:
                fact_accounting = validate_state_fact_accounting(
                    raw_fact_accounting
                )
            except StateFactAccountingError:
                fact_accounting_invalid = True
        completion_reason = str(evidence.get("completion_reason") or "") or None
        revision = int(delta.get("revision") or 0) or None
        if not source_digest:
            return StateCompletion(
                chapter_id=chapter_id,
                status="unknown_legacy",
                needs_backfill=False,
                requires_pause=False,
                prose_eligible=eligible,
                prose_acceptance_state=acceptance_state,
                current_content_digest=current_digest,
                completion_reason=completion_reason,
                reference_resolution=resolution,
                fact_accounting=fact_accounting,
                delta_revision=revision,
            )
        if source_digest != current_digest:
            return StateCompletion(
                chapter_id=chapter_id,
                status="stale_after_content_edit",
                needs_backfill=True,
                requires_pause=False,
                prose_eligible=eligible,
                prose_acceptance_state=acceptance_state,
                source_content_digest=str(source_digest),
                current_content_digest=current_digest,
                completion_reason=completion_reason,
                reference_resolution=resolution,
                fact_accounting=fact_accounting,
                delta_revision=revision,
            )

        if fact_accounting_invalid or (
            fact_accounting is not None
            and fact_accounting.get("gate_passed") is not True
        ):
            return StateCompletion(
                chapter_id=chapter_id,
                status="degraded_fact_accounting",
                needs_backfill=True,
                requires_pause=True,
                prose_eligible=eligible,
                prose_acceptance_state=acceptance_state,
                source_content_digest=str(source_digest),
                current_content_digest=current_digest,
                completion_reason=completion_reason,
                reference_resolution=resolution,
                fact_accounting=fact_accounting,
                delta_revision=revision,
            )

        proposed = int(resolution.get("proposed_character_update_count") or 0)
        accepted = int(resolution.get("accepted_character_update_count") or 0)
        dropped = int(resolution.get("dropped_character_update_count") or 0)
        if proposed > 0 and accepted == 0 and dropped >= proposed:
            status: StateCompletionStatus = (
                "degraded_all_character_updates_dropped"
            )
            needs_backfill = True
            requires_pause = True
        elif dropped > 0:
            status = "degraded_partial_reference_drop"
            needs_backfill = False
            requires_pause = False
        else:
            status = "current"
            needs_backfill = False
            requires_pause = False
        return StateCompletion(
            chapter_id=chapter_id,
            status=status,
            needs_backfill=needs_backfill,
            requires_pause=requires_pause,
            prose_eligible=eligible,
            prose_acceptance_state=acceptance_state,
            source_content_digest=str(source_digest),
            current_content_digest=current_digest,
            completion_reason=completion_reason,
            reference_resolution=resolution,
            fact_accounting=fact_accounting,
            delta_revision=revision,
        )

    async def inspect(self, chapter_id: str) -> StateCompletion:
        chapter = await chapter_repo.get_chapter_by_id(chapter_id)
        delta = await get_database()[collections.CHAPTER_STATE_DELTAS].find_one(
            {
                "novel_id": chapter["novel_id"],
                "chapter_id": chapter["_id"],
                "is_deleted": False,
            }
        )
        return self.classify(chapter, delta)

    async def inspect_many(
        self,
        chapters: list[dict[str, Any]],
        *,
        allow_unverified_legacy: bool = False,
    ) -> dict[str, StateCompletion]:
        if not chapters:
            return {}
        chapter_ids = [to_object_id(str(chapter["_id"])) for chapter in chapters]
        novel_ids = {
            to_object_id(str(chapter["novel_id"])) for chapter in chapters
        }
        deltas = await get_database()[collections.CHAPTER_STATE_DELTAS].find(
            {
                "novel_id": {"$in": list(novel_ids)},
                "chapter_id": {"$in": chapter_ids},
                "is_deleted": False,
            }
        ).to_list(length=None)
        by_chapter = {str(delta["chapter_id"]): delta for delta in deltas}
        return {
            str(chapter["_id"]): self.classify(
                chapter,
                by_chapter.get(str(chapter["_id"])),
                allow_unverified_legacy=allow_unverified_legacy,
            )
            for chapter in chapters
        }

    async def attach_many(
        self,
        chapters: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        inspected = await self.inspect_many(chapters)
        return [
            {
                **chapter,
                "state_completion": inspected[str(chapter["_id"])].to_dict(),
            }
            for chapter in chapters
        ]

    async def needs_backfill(self, chapter_id: str) -> bool:
        return (await self.inspect(chapter_id)).needs_backfill


state_completion_module = StateCompletionModule()
