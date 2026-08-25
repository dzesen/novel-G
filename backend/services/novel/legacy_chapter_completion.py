"""Frozen, deterministic proof for pre-V2 AI chapter prose.

This verifier never grants authority to rewrite prose.  It only proves that
unchanged legacy prose is safe to use as the source of a new state proposal.
Book completion adds the stricter requirement that the accepted state is
already current.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from bson import ObjectId

from backend.db import collections
from backend.db.mongo import get_database
from backend.db.repositories.chapter_repository import chapter_repo
from backend.db.utils import to_object_id
from backend.services.novel.chapter_service import count_chapter_words
from backend.services.novel.state_completion import chapter_content_digest


@dataclass(frozen=True)
class LegacyChapterCompletionProof:
    source_prose_run_id: str
    source_prose_run_revision: int | None
    source_content_digest: str


def legacy_semantic_override(
    *,
    source_prose_run_id: str,
    source_content_digest: str,
    finalization_journals: list[Mapping[str, Any]],
    job_reviews: list[Mapping[str, Any]],
) -> bool:
    """Return whether old semantic evidence explicitly denied completion."""

    for journal in finalization_journals:
        command = journal.get("command")
        if not isinstance(command, Mapping) or command.get("version") != 1:
            continue
        payload = command.get("payload")
        raw_evidence = (
            payload.get("evidence")
            if isinstance(payload, Mapping)
            else None
        )
        adherence = (
            raw_evidence.get("outline_adherence")
            if isinstance(raw_evidence, Mapping)
            else None
        )
        if isinstance(adherence, Mapping):
            result = adherence.get(
                "decision"
                if adherence.get("decision") is not None
                else "verdict"
            )
            if result is not None and result != "pass":
                return True
        if (
            isinstance(payload, Mapping)
            and payload.get("outline_deviation_policy")
            == "accept_and_continue"
        ):
            return True

    for item in job_reviews:
        review = item.get("outline_adherence")
        if not isinstance(review, Mapping):
            continue
        if (
            str(review.get("source_prose_run_id") or "")
            != source_prose_run_id
            or str(review.get("source_content_digest") or "")
            != source_content_digest
        ):
            continue
        result = review.get(
            "decision"
            if review.get("decision") is not None
            else "verdict"
        )
        if result is not None and result != "pass":
            return True
        if item.get("outline_deviation_policy") == "accept_and_continue":
            return True
    return False


async def verify_legacy_chapter_completion_for_state(
    *,
    novel_id: str,
    chapter_id: str,
    chapter: Mapping[str, Any] | None = None,
) -> LegacyChapterCompletionProof | None:
    """Prove unchanged legacy AI prose without dispatching a Provider call."""

    current = (
        dict(chapter)
        if isinstance(chapter, Mapping)
        else await chapter_repo.get_chapter_by_id(chapter_id)
    )
    if str(current.get("novel_id") or "") != str(novel_id):
        return None
    acceptance = current.get("prose_acceptance")
    content = str(current.get("content") or "")
    content_digest = chapter_content_digest(content)
    if (
        not content.strip()
        or not isinstance(acceptance, Mapping)
        or acceptance.get("state") != "ai_complete"
        or acceptance.get("chapter_completion_certificate") is not None
        or acceptance.get("content_digest") != content_digest
        or acceptance.get("completion_status") != "complete"
        or acceptance.get("finish_reason") != "stop"
    ):
        return None
    source_run_id = str(acceptance.get("source_run_id") or "")
    if not ObjectId.is_valid(source_run_id):
        return None

    outline = current.get("outline")
    scenes = outline.get("scenes") if isinstance(outline, Mapping) else None
    if not isinstance(scenes, list) or not scenes:
        return None
    target_word_count = (
        outline.get("target_word_count")
        if isinstance(outline, Mapping)
        else None
    )
    if (
        target_word_count is not None
        and (
            isinstance(target_word_count, bool)
            or not isinstance(target_word_count, int)
            or target_word_count < 1
        )
    ):
        return None
    if (
        isinstance(target_word_count, int)
        and count_chapter_words(content)
        < math.ceil(target_word_count * 0.8)
    ):
        return None

    database = get_database()
    source_run = await database[collections.PROSE_RUNS].find_one({
        "_id": to_object_id(source_run_id),
        "novel_id": to_object_id(novel_id),
        "chapter_id": to_object_id(chapter_id),
        "is_deleted": False,
    })
    completion = (
        source_run.get("completion")
        if isinstance(source_run, Mapping)
        else None
    )
    if (
        not isinstance(source_run, Mapping)
        or source_run.get("status") != "accepted"
        or source_run.get("acceptance_state") != "ai_complete"
        or source_run.get("accepted_text_digest") != content_digest
        or not isinstance(completion, Mapping)
        or completion.get("status") != "complete"
        or completion.get("can_write_formal_prose") is not True
        or completion.get("finish_reason") != "stop"
        or completion.get("scene_count") != len(scenes)
        or completion.get("completed_scene_count") != len(scenes)
    ):
        return None

    journals = await database[collections.MUTATION_JOURNALS].find(
        {
            "novel_id": to_object_id(novel_id),
            "operation": "finalize_chapter_generation",
            "command.payload.chapter_id": str(chapter_id),
            "is_deleted": False,
        },
        projection={"command": 1},
    ).to_list(length=None)
    jobs = await database[collections.GENERATION_JOBS].find(
        {
            "novel_id": to_object_id(novel_id),
            "is_deleted": False,
            "progress.outline_adherence": {"$exists": True},
        },
        projection={"progress": 1, "outline_deviation_policy": 1},
    ).to_list(length=None)
    job_reviews: list[Mapping[str, Any]] = []
    for job in jobs:
        for entry in job.get("progress") or []:
            if (
                isinstance(entry, Mapping)
                and str(entry.get("chapter_id") or "") == str(chapter_id)
                and isinstance(entry.get("outline_adherence"), Mapping)
            ):
                job_reviews.append({
                    "outline_adherence": entry["outline_adherence"],
                    "outline_deviation_policy": job.get(
                        "outline_deviation_policy"
                    ),
                })
    if legacy_semantic_override(
        source_prose_run_id=source_run_id,
        source_content_digest=content_digest,
        finalization_journals=journals,
        job_reviews=job_reviews,
    ):
        return None
    raw_revision = source_run.get("revision")
    revision = (
        raw_revision
        if type(raw_revision) is int and raw_revision >= 1
        else None
    )
    return LegacyChapterCompletionProof(
        source_prose_run_id=source_run_id,
        source_prose_run_revision=revision,
        source_content_digest=content_digest,
    )
