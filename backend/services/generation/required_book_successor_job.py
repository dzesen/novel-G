"""Production orchestration shell for the bounded book successor.

The coordinator contract owns every transition.  This Module owns only the
sequence of side-effecting ports: derive the current child, persist it, run it,
append its validated result, and finally invoke the fenced book audit.  Keeping
those ports injected makes the recovery order testable without MongoDB or a
Provider.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from backend.db.utils import get_utc_now
from backend.services.generation.required_book_successor import (
    RequiredBookSuccessorAction,
    RequiredBookSuccessorConflict,
    RequiredBookSuccessorCoordinator,
    RequiredBookSuccessorJournal,
    parse_required_book_successor_journal,
    parse_required_book_successor_recovery_checkpoint,
)


RequiredBookSuccessorRunState = Literal[
    "completed",
    "blocked",
    "deferred",
    "paused",
    "aborted",
]


@dataclass(frozen=True)
class RequiredBookSuccessorJobDeps:
    get_parent: Callable[[], Awaitable[Mapping[str, Any]]]
    initialize: Callable[[], Awaitable[RequiredBookSuccessorJournal | Mapping[str, Any]]]
    get_chapter: Callable[[str], Awaitable[Mapping[str, Any]]]
    read_child: Callable[[str], Awaitable[Mapping[str, Any]]]
    create_child: Callable[
        [RequiredBookSuccessorAction, Mapping[str, Any]],
        Awaitable[Mapping[str, Any]],
    ]
    run_child: Callable[[str], Awaitable[Mapping[str, Any]]]
    advance_child: Callable[[str], Awaitable[RequiredBookSuccessorJournal | Mapping[str, Any]]]
    block: Callable[[str], Awaitable[Any]]
    finalize_audit: Callable[[], Awaitable[bool]]
    pause_parent: Callable[[], Awaitable[Any]]
    abort_parent: Callable[[], Awaitable[Any]]
    pause_requested: Callable[[], bool]
    abort_requested: Callable[[], bool]
    now: Callable[[], datetime] = get_utc_now
    pause_for_recovery_checkpoint: Callable[[str], Awaitable[Any]] | None = None


class RequiredBookSuccessorJobRunner:
    """Run the exact root transition sequence, one persisted child at a time."""

    def __init__(
        self,
        coordinator_job_id: str,
        *,
        deps: RequiredBookSuccessorJobDeps,
    ) -> None:
        self.coordinator_job_id = str(coordinator_job_id)
        self.deps = deps

    async def run(self) -> RequiredBookSuccessorRunState:
        parent = await self.deps.get_parent()
        coordinator = RequiredBookSuccessorCoordinator(
            coordinator_job_id=self.coordinator_job_id,
            readiness=parent.get("readiness") or {},
        )
        initialized = await self.deps.initialize()
        journal = (
            initialized
            if isinstance(initialized, RequiredBookSuccessorJournal)
            else parse_required_book_successor_journal(initialized)
        )
        if (
            coordinator.authorization.recovery_checkpoint
            == "before_first_child"
            and not journal.stages
        ):
            execution_epoch = parent.get("execution_epoch")
            if type(execution_epoch) is not int or execution_epoch < 1:
                raise RequiredBookSuccessorConflict(
                    "required_book_successor_recovery_epoch_invalid"
                )
            raw_checkpoint = parent.get(
                "required_book_successor_recovery_checkpoint"
            )
            if raw_checkpoint is None:
                pause = self.deps.pause_for_recovery_checkpoint
                if pause is None:
                    raise RequiredBookSuccessorConflict(
                        "required_book_successor_recovery_port_missing"
                    )
                paused = await pause(journal.journal_digest)
                if paused is False:
                    raise RequiredBookSuccessorConflict(
                        "required_book_successor_recovery_pause_fence_lost"
                    )
                return "paused"
            checkpoint = parse_required_book_successor_recovery_checkpoint(
                raw_checkpoint
            )
            if (
                checkpoint.coordinator_job_id != self.coordinator_job_id
                or checkpoint.coordinator_readiness_digest
                != journal.coordinator_readiness_digest
                or checkpoint.journal_digest != journal.journal_digest
            ):
                raise RequiredBookSuccessorConflict(
                    "required_book_successor_recovery_checkpoint_changed"
                )
        maximum_transitions = journal.chapter_count * 3 + 1
        for _ in range(maximum_transitions):
            if self.deps.abort_requested():
                await self.deps.abort_parent()
                return "aborted"
            if self.deps.pause_requested():
                await self.deps.pause_parent()
                return "paused"
            action = coordinator.next_action(journal)
            if action is None:
                return "completed" if journal.phase == "completed" else "blocked"
            if self.deps.now() >= coordinator.authorization.deadline_at:
                await self.deps.block("authorization_deadline_expired")
                return "blocked"
            if action.stage == "book_audit":
                completed = await self.deps.finalize_audit()
                return "completed" if completed else "blocked"

            if action.stage == "review":
                chapter = await self.deps.get_chapter(str(action.chapter_id))
                child_readiness = coordinator.derive_review_readiness(
                    journal,
                    chapter,
                )
            elif action.stage == "state":
                reviewed_job = await self.deps.read_child(
                    action.predecessor_job_ids[0]
                )
                child_readiness = coordinator.derive_state_readiness(
                    journal,
                    reviewed_job,
                )
            else:
                reviewed_job = await self.deps.read_child(
                    action.predecessor_job_ids[0]
                )
                state_job = await self.deps.read_child(
                    action.predecessor_job_ids[1]
                )
                child_readiness = coordinator.derive_finalization_readiness(
                    journal,
                    state_job,
                    reviewed_job,
                )

            child = await self.deps.create_child(action, child_readiness)
            child_id = str(child.get("_id") or "")
            child = await self.deps.run_child(child_id)
            if str(child.get("status") or "") == "running":
                return "deferred"
            if child.get("has_uncertain_attempts") is True:
                await self.deps.block("child_provider_attempt_uncertain")
                return "blocked"
            try:
                advanced = await self.deps.advance_child(child_id)
                journal = (
                    advanced
                    if isinstance(advanced, RequiredBookSuccessorJournal)
                    else parse_required_book_successor_journal(advanced)
                )
            except RequiredBookSuccessorConflict:
                await self.deps.block("child_stage_not_ready")
                return "blocked"
        raise RequiredBookSuccessorConflict(
            "required_book_successor_transition_overflow"
        )
