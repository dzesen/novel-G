"""Content-free identity links between generation jobs and prose runs."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


MAX_RELATED_PROSE_RUN_IDS = 200


def _object_id_text(value: Any) -> str | None:
    text = str(value or "")
    if (
        len(text) == 24
        and all(character in "0123456789abcdef" for character in text)
    ):
        return text
    return None


def related_prose_run_ids(job: Mapping[str, Any]) -> tuple[str, ...]:
    """Return bounded run identities explicitly retained by one job.

    Older jobs predate the direct ``generation_job_id`` field on prose runs,
    but their append-only candidate checkpoints still bind the exact run.
    """

    result: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        run_id = _object_id_text(value)
        if run_id is None or run_id in seen:
            return
        if len(result) >= MAX_RELATED_PROSE_RUN_IDS:
            return
        seen.add(run_id)
        result.append(run_id)

    def add_source(value: Any) -> None:
        if isinstance(value, Mapping):
            add(value.get("source_run_id"))

    checkpoints = job.get("candidate_pipeline_checkpoints")
    if isinstance(checkpoints, list):
        for checkpoint in checkpoints[-MAX_RELATED_PROSE_RUN_IDS:]:
            if isinstance(checkpoint, Mapping):
                add_source(checkpoint.get("source"))

    progress = job.get("progress")
    if isinstance(progress, list):
        for entry in progress[-MAX_RELATED_PROSE_RUN_IDS:]:
            if not isinstance(entry, Mapping):
                continue
            add_source(entry.get("source"))
            completion = entry.get("candidate_pipeline_completion")
            if isinstance(completion, Mapping):
                add_source(completion.get("source"))
            incomplete = entry.get("incomplete_prose")
            if isinstance(incomplete, Mapping):
                add(incomplete.get("source_run_id"))
            prose_completion = entry.get("prose_completion")
            if isinstance(prose_completion, Mapping):
                add(prose_completion.get("source_run_id"))

    diagnostics = job.get("diagnostics")
    if isinstance(diagnostics, list):
        for event in diagnostics[-MAX_RELATED_PROSE_RUN_IDS:]:
            if not isinstance(event, Mapping):
                continue
            details = event.get("details")
            if isinstance(details, Mapping):
                add(details.get("prose_run_id"))

    return tuple(result)
